"""串口、演示数据和环境控制回归测试；不需要真实硬件。"""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "LabSafetyMonitor"):
    sys.path.insert(0, str(path))

import serial_comm
from serial_comm import SerialComm
from src.core import demo, env_control
from src.core.telemetry import TelemetryStore
from src.hardware import sensor_worker


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class SerialRegressionTests(unittest.TestCase):
    def setUp(self):
        self.shared = SerialComm._shared
        SerialComm._shared = None
        self.link = SerialComm()

    def tearDown(self):
        SerialComm._shared = self.shared

    def test_wrong_command_ack_cannot_confirm_power_cut(self):
        self.link._pending[7] = {"cmd": serial_comm.CMD_POWER_OFF}
        self.link.power_off_state = "pending"
        self.link._handle(1, serial_comm.MSG_ACK, bytes([7, serial_comm.CMD_SET_ENV, 0]))
        self.assertEqual(self.link.power_off_state, "pending")
        self.assertIn(7, self.link._pending)
        self.link._handle(2, serial_comm.MSG_ACK, bytes([7, serial_comm.CMD_POWER_OFF, 0]))
        self.assertEqual(self.link.power_off_state, "acked")
        self.assertNotIn(7, self.link._pending)

    def test_reconnect_waits_for_new_telemetry_and_actuator_status(self):
        self.link.serial_conn = SimpleNamespace(is_open=True, close=lambda: None)
        self.link._handle(1, serial_comm.MSG_TELEMETRY,
                          struct.pack("<hHhHBB", 250, 550, 300, 400, 0x3F, 0))
        self.link._handle(2, serial_comm.MSG_ENV_STATUS, bytes([70, 1, 1]))
        self.assertTrue(self.link.is_alive)
        self.link._close("测试断开")
        self.link.serial_conn = SimpleNamespace(is_open=True, close=lambda: None)
        self.assertFalse(self.link.is_alive)
        self.assertIsNone(self.link.get_sensor_data()["fan_pwm"])


class IPCRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="labsafety_ipc_test_")
        self.path = str(Path(self.tmp.name) / "telemetry.json")
        self.path_patch = patch.object(sensor_worker.SensorWorker, "IPC_TELEMETRY_PATH", self.path)
        self.path_patch.start()
        self.cfg_patch = patch.object(sensor_worker, "cfg", return_value=True)
        self.cfg_patch.start()
        self.worker = sensor_worker.SensorWorker()

    def tearDown(self):
        self.cfg_patch.stop()
        self.path_patch.stop()
        self.tmp.cleanup()

    def write(self, value):
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump(value, stream)

    def test_bad_ipc_values_are_rejected_before_worker_conversion(self):
        for field, value in (("temp", "broken"), ("humi", None),
                             ("obj_temp", []), ("smoke", float("inf")),
                             ("temp", float("nan"))):
            with self.subTest(field=field, value=value):
                payload = {"temp": 25, "humi": 55, "obj_temp": None, "smoke": None}
                payload[field] = value
                self.write(payload)
                self.assertIsNone(self.worker._read_ipc())

    def test_numeric_ipc_is_normalized(self):
        self.write({"temp": "26.5", "humi": 55, "obj_temp": "99", "smoke": "300"})
        payload = self.worker._read_ipc()
        self.assertEqual(payload["temp"], 26.5)
        self.assertEqual(payload["obj_temp"], 99.0)
        self.assertEqual(payload["smoke"], 300)

    def test_open_hardware_port_never_reads_demo_file(self):
        link = SimpleNamespace(is_alive=False, is_open=True, status_text=lambda: "等待遥测")
        with patch.object(sensor_worker, "_serial_link", return_value=link), \
                patch.object(sensor_worker, "telemetry", TelemetryStore()), \
                patch.object(self.worker, "_read_ipc", side_effect=AssertionError("不得混入演示")), \
                patch.object(self.worker, "_sleep", side_effect=lambda _ms: setattr(self.worker, "_running", False)):
            self.worker.run()


class DemoRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="labsafety_demo_test_")
        self.path = str(Path(self.tmp.name) / "telemetry.json")
        self.path_patch = patch.object(sensor_worker.SensorWorker, "IPC_TELEMETRY_PATH", self.path)
        self.path_patch.start()
        self.cfg_patch = patch.object(demo, "cfg", return_value=True)
        self.cfg_patch.start()
        self.link = SimpleNamespace(is_open=False, is_alive=False)
        self.injector = demo.DemoInjector(SimpleNamespace(serial=self.link))

    def tearDown(self):
        self.injector.stop()
        self.cfg_patch.stop()
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_open_hardware_port_blocks_demo_before_first_telemetry(self):
        self.link.is_open = True
        with self.assertRaises(PermissionError):
            self.injector.start("smoke")
        self.assertFalse(os.path.exists(self.path))

    def test_bad_custom_values_are_rejected_synchronously(self):
        for value in (None, [], "broken", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.injector.start("custom", {"temp": value})
        self.assertIsNone(self.injector._thread)

    def test_stop_waits_for_inflight_write_and_restart_works(self):
        entered, release = threading.Event(), threading.Event()
        real_replace = os.replace

        def blocked_replace(source, destination):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("测试没有释放写入")
            real_replace(source, destination)

        stopper = None
        with patch.object(demo.os, "replace", side_effect=blocked_replace):
            try:
                self.injector.start("heating")
                self.assertTrue(entered.wait(2))
                stopper = threading.Thread(target=self.injector.stop)
                stopper.start()
                stopper.join(0.03)
                self.assertTrue(stopper.is_alive())
            finally:
                release.set()
                if stopper is not None:
                    stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(os.path.exists(self.path))
        self.injector.start("smoke")
        self.assertTrue(wait_until(lambda: os.path.exists(self.path)))
        with open(self.path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["smoke"], 2300)

    def test_hardware_arrival_stops_demo_and_removes_ipc(self):
        self.injector.start("heating")
        self.assertTrue(wait_until(lambda: os.path.exists(self.path)))
        self.link.is_open = True
        self.assertTrue(wait_until(lambda: self.injector.status()["scenario"] is None))
        self.assertFalse(os.path.exists(self.path))


class EnvironmentRegressionTests(unittest.TestCase):
    def test_lost_humidity_does_not_crash_when_humidifier_was_on(self):
        fan, humid, reason = env_control.control_law(None, None, None, 26, 40, 70, 1200,
                                                   prev_humid=True)
        self.assertEqual(fan, 0)
        self.assertTrue(humid)
        self.assertIn("湿度数据暂缺", reason)

    def test_smoke_overrides_manual_simulation_as_on_firmware(self):
        store = TelemetryStore()
        store.update("ipc", temp=25, humi=30, obj_temp=None, smoke=2000)
        controller = env_control.EnvController()
        with patch.object(env_control, "telemetry", store):
            controller.set_manual(20, True)
            status = controller.status()
            self.assertEqual((status["fan_pwm"], status["humid_on"]), (100, False))
            store.update("ipc", smoke=300)
            controller.tick()
            status = controller.status()
            self.assertEqual((status["fan_pwm"], status["humid_on"]), (20, True))

    def test_failed_setpoint_enqueue_is_retried(self):
        attempts = []
        link = SimpleNamespace(is_alive=True, get_sensor_data=lambda: {},
                               set_env=lambda *args: attempts.append(args) or False)
        controller = env_control.EnvController(link)
        controller.tick()
        controller.tick()
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
