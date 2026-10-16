"""安全规则与温度预测回归测试，无需摄像头或下位机。

运行：python tests/test_core_regressions.py
"""
import copy
import os
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config as config_mod
from src.core import state_machine as state_machine_mod
from src.core.state_machine import SafetyStateMachine
from src.core.telemetry import TelemetryStore
from src.core.world_model import ThermalWorldModel


class CoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(config_mod.DEFAULTS)
        self.config["rules"]["sim_latch_release_s"] = 1.0
        config_patch = patch.object(config_mod, "_config", self.config)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.telemetry = TelemetryStore()
        tel_patch = patch.object(state_machine_mod, "telemetry", self.telemetry)
        tel_patch.start()
        self.addCleanup(tel_patch.stop)
        # 只测试规则，不启动事件的数据库、语音或串口后台任务。
        self.events = Mock()
        self.events.update.return_value = None
        event_patch = patch.object(state_machine_mod, "events", self.events)
        event_patch.start()
        self.addCleanup(event_patch.stop)

    def evaluate_at(self, sm, now, *, source="ipc", obj_temp=None,
                    ambient=25.0, unattended=False, items=None):
        with patch.object(state_machine_mod.time, "time", return_value=now):
            self.telemetry.update(source, obj_temp=obj_temp, smoke=None)
            return sm.evaluate(items or [], unattended, ambient, 55.0)

    @staticmethod
    def hardware():
        link = Mock(is_alive=True)
        link.get_sensor_data.return_value = {"latched": False, "cut_reason": 0}
        link.send_power_off.return_value = True
        return link

    def test_missing_ambient_sensor_does_not_crash_fallback(self):
        sm = SafetyStateMachine()
        state, _ = self.evaluate_at(sm, 1000, ambient=None,
                                    unattended=True, items=["Beaker"])
        self.assertEqual(state, "NORMAL")
        self.assertFalse(sm.latched)

    def test_hardware_disconnect_keeps_latch_until_field_reset(self):
        link = self.hardware()
        sm = SafetyStateMachine(link)
        self.evaluate_at(sm, 1000, source="hardware", obj_temp=130.0)
        self.assertTrue(sm.latched)
        # 失联后传感器线程会切换来源；这不能被当成纯演示而自动解锁。
        link.is_alive = False
        self.evaluate_at(sm, 1001, obj_temp=25.0)
        state, _ = self.evaluate_at(sm, 1005, obj_temp=25.0)
        self.assertEqual(state, "CRITICAL")
        self.assertTrue(sm.latched)
        self.assertEqual(link.send_power_off.call_count, 1)
        link.is_alive = True
        sm._on_serial_event("mcu_event", {"code": 5})
        state, _ = self.evaluate_at(sm, 1006, source="hardware", obj_temp=25.0)
        self.assertEqual(state, "NORMAL")
        self.assertFalse(sm.latched)

    def test_hardware_telemetry_keeps_latch_without_live_link(self):
        sm = SafetyStateMachine()
        self.evaluate_at(sm, 1000, source="hardware", obj_temp=130.0)
        self.evaluate_at(sm, 1001, obj_temp=25.0)
        self.evaluate_at(sm, 1005, obj_temp=25.0)
        self.assertTrue(sm.latched)

    def test_manual_hardware_cut_keeps_latch_after_disconnect(self):
        link = self.hardware()
        sm = SafetyStateMachine(link)
        sm.manual_power_off("测试")
        link.is_alive = False
        self.evaluate_at(sm, 1001, obj_temp=25.0)
        self.evaluate_at(sm, 1005, obj_temp=25.0)
        self.assertTrue(sm.latched)

    def test_disconnected_hardware_cut_reports_failure(self):
        link = self.hardware()
        sm = SafetyStateMachine(link)
        self.evaluate_at(sm, 1000, source="hardware", obj_temp=25.0)
        link.is_alive = False
        link.is_open = False
        link.send_power_off.return_value = False
        self.evaluate_at(sm, 1001, obj_temp=25.0)
        message = sm.manual_power_off("网页")
        self.assertIn("断电指令无法下发", message)
        self.assertIn("人工切断", message)
        self.assertNotIn("演示模式", message)

    def test_manual_cut_waits_for_active_evaluation(self):
        sm = SafetyStateMachine()
        evaluating, release, cutting, cut_done = (threading.Event() for _ in range(4))
        original_snapshot = self.telemetry.snapshot

        def blocked_snapshot():
            evaluating.set()
            if not release.wait(3.0):
                raise RuntimeError("评估未按时恢复")
            return original_snapshot()

        def cut():
            cutting.set()
            sm.manual_power_off("网页")
            cut_done.set()

        self.telemetry.update("ipc", obj_temp=None, smoke=None)
        with patch.object(self.telemetry, "snapshot", side_effect=blocked_snapshot):
            evaluation = threading.Thread(target=sm.evaluate, args=([], False, 25.0, 55.0))
            manual = threading.Thread(target=cut)
            evaluation.start()
            try:
                self.assertTrue(evaluating.wait(1.0))
                manual.start()
                self.assertTrue(cutting.wait(1.0))
                self.assertFalse(cut_done.wait(0.1))
            finally:
                release.set()
                evaluation.join(2.0)
                if manual.ident is not None:
                    manual.join(2.0)
        self.assertFalse(evaluation.is_alive())
        self.assertFalse(manual.is_alive())
        self.assertTrue(cut_done.is_set())
        self.assertEqual(sm.snapshot()["state"], "CRITICAL")
        self.assertTrue(sm.snapshot()["latched"])

    def test_demo_latch_still_releases_after_hazard_clears(self):
        sm = SafetyStateMachine()
        self.evaluate_at(sm, 1000, obj_temp=130.0)
        self.evaluate_at(sm, 1001, obj_temp=25.0)
        state, _ = self.evaluate_at(sm, 1005, obj_temp=25.0)
        self.assertEqual(state, "NORMAL")
        self.assertFalse(sm.latched)

    def test_prediction_warning_cannot_cut_power(self):
        link = self.hardware()
        sm = SafetyStateMachine(link)
        for second in range(16):
            state, _ = self.evaluate_at(sm, 1000 + second, source="hardware",
                                        obj_temp=104.5 + 0.5 * second)
        self.assertEqual(state, "WARNING")
        self.assertFalse(sm.latched)
        self.assertGreater(sm.world_model.snapshot()["eta_s"], 0)
        link.send_power_off.assert_not_called()

    def test_overheat_is_visible_before_enough_forecast_samples(self):
        result = ThermalWorldModel().update(130.0, now=1000.0)
        self.assertEqual(result["phase"], "overheat")
        self.assertEqual(result["risk"], 1.0)
        self.assertEqual(result["eta_s"], 0.0)
        self.assertTrue(result["will_dry_burn"])

    def test_cooling_ends_boiling_plateau(self):
        wm = ThermalWorldModel()
        for second in range(40):
            result = wm.update(102.0, now=1000 + second)
        self.assertEqual(result["phase"], "boiling")
        for second in range(14):
            result = wm.update(102.0 - 0.5 * second, now=1040 + second)
        self.assertEqual(result["phase"], "cooling")
        self.assertEqual(result["plateau_s"], 0.0)
        self.assertEqual(result["risk"], 0.0)

    def test_long_telemetry_gap_does_not_count_as_continuous_boiling(self):
        wm = ThermalWorldModel()
        for second in range(40):
            wm.update(99.0, now=1000 + second)
        for second in range(10):
            result = wm.update(99.0, now=2000 + second)
        self.assertEqual(result["phase"], "boiling")
        self.assertLessEqual(result["plateau_s"], 1.0)
        self.assertLess(result["risk"], 0.01)


if __name__ == "__main__":
    unittest.main(verbosity=2)
