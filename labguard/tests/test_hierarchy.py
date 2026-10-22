"""分层规则、证据隔离、联动路由、持久化与执行安全回归。无需摄像头。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.hierarchy.config import default_config, validate_config, make_lab
from src.hierarchy.coordinator import BuildingCoordinator, Observation, Telemetry


class Clock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class HierarchyTests(unittest.TestCase):
    def setUp(self):
        self.config = default_config()
        self.clock = Clock()
        self.core = BuildingCoordinator(self.config, clock=self.clock)
        self.feed_all()

    def feed_all(self):
        for lab in self.config['labs']:
            self.feed(lab['macro']['id'], ['Lab Coat'])
            for bench in lab['benches']:
                self.feed(bench['camera']['id'], ['Beaker'])
                self.core.update_telemetry(lab['id'], bench['id'], Telemetry(self.clock(), 25, 55))
        self.core.tick()

    def feed(self, node, labels, status='SIMULATION'):
        self.core.ingest(Observation(node, self.clock(), labels, status))

    def fire(self, lab_index=0):
        self.clock.advance(0.1)
        self.feed(self.config['labs'][lab_index]['macro']['id'], ['fire'])
        self.core.tick()

    def test_default_topology_and_health(self):
        self.assertEqual(len(self.core.node_labs), 5)
        self.assertEqual(self.core.snapshot()['level'], 'NORMAL')
        self.assertEqual(len(self.core.snapshot()['labs'][0]['benches']), 2)

    def test_minimal_asymmetric_pair_is_valid(self):
        cfg = deepcopy(self.config)
        cfg['labs'] = cfg['labs'][:1]
        cfg['labs'][0]['benches'] = cfg['labs'][0]['benches'][:1]
        self.assertEqual(len(BuildingCoordinator(validate_config(cfg)).node_labs), 2)

    def test_fire_fanout_and_idempotency(self):
        self.fire()
        self.assertEqual(len(self.core.events), 1)
        self.assertEqual(len(self.core.commands), 4)
        self.assertEqual({c['target_lab'] for c in self.core.commands.values()}, {'lab-101', 'lab-102'})
        for _ in range(20): self.core.tick()
        self.assertEqual(len(self.core.commands), 4)
        self.assertTrue(all(c['status'] == 'SIMULATED' for c in self.core.commands.values()))

    def test_scope_can_limit_other_lab_to_alarm(self):
        cfg = deepcopy(self.config)
        cfg['labs'].append(make_lab('lab-103', '实验室 103'))
        cfg['fire_linkage'] = dict(targets=['lab-102'], actions=['alarm'])
        self.core = BuildingCoordinator(cfg, clock=self.clock)
        self.fire()
        self.assertEqual(len(self.core.commands), 3)
        self.assertFalse(any(c['target_lab'] == 'lab-103' for c in self.core.commands.values()))
        self.assertFalse(any(c['target_lab'] == 'lab-102' and c['action'] == 'power_off' for c in self.core.commands.values()))

    def prepare_dry(self):
        lab = self.config['labs'][0]
        self.clock.advance(0.1)
        self.feed(lab['macro']['id'], [])
        self.clock.advance(5.1)
        self.feed(lab['macro']['id'], [])
        self.feed(lab['benches'][1]['camera']['id'], ['heater'])
        self.core.update_telemetry(lab['id'], lab['benches'][1]['id'], Telemetry(self.clock(), 60, 55))
        return lab

    def test_one_macro_to_multiple_micro_and_local_dry_action(self):
        lab = self.prepare_dry()
        self.core.tick()
        event = list(self.core.events.values())[0]
        self.assertEqual(event['node_id'], lab['benches'][1]['id'])
        self.assertEqual(event['event_type'], 'DRY_BURN')
        self.assertEqual(len(self.core.commands), 2)
        self.assertEqual({c['target_lab'] for c in self.core.commands.values()}, {'lab-101'})

    def test_no_cross_bench_evidence_mix(self):
        lab = self.prepare_dry()
        self.clock.advance(0.1)
        self.feed(lab['benches'][1]['camera']['id'], ['Beaker'])
        self.feed(lab['benches'][0]['camera']['id'], ['heater'])
        self.core.tick()
        self.assertEqual(len(self.core.events), 0, '低温台的加热证据不能与另一个台的高温拼接')

    def test_container_is_not_heating(self):
        lab = self.prepare_dry()
        self.clock.advance(0.1)
        self.feed(lab['benches'][1]['camera']['id'], ['Beaker', 'Conical Flask'])
        self.core.tick()
        self.assertFalse(self.core.events)

    def test_macro_offline_is_not_unattended(self):
        lab = self.prepare_dry()
        self.clock.advance(0.1)
        self.feed(lab['macro']['id'], [], 'ERROR')
        self.core.tick()
        self.assertFalse(self.core.events)
        self.assertIsNone(self.core.snapshot()['labs'][0]['unattended'])

    def test_stale_macro_is_unknown(self):
        lab = self.prepare_dry()
        self.clock.advance(9)
        self.feed(lab['benches'][1]['camera']['id'], ['heater'])
        self.core.update_telemetry(lab['id'], lab['benches'][1]['id'], Telemetry(self.clock(), 60, 55))
        self.core.tick()
        self.assertFalse(self.core.events)
        self.assertIsNone(self.core.snapshot()['labs'][0]['unattended'])

    def test_absence_timer_restarts_after_gap(self):
        lab = self.config['labs'][0]
        self.clock.advance(0.1)
        self.feed(lab['macro']['id'], [])
        self.clock.advance(10)
        self.feed(lab['macro']['id'], [])
        self.assertFalse(self.core.snapshot()['labs'][0]['unattended'])

    def test_stale_sensor_cannot_trigger_dry(self):
        lab = self.prepare_dry()
        self.clock.advance(9)
        self.feed(lab['macro']['id'], [])
        self.feed(lab['benches'][1]['camera']['id'], ['heater'])
        self.core.tick()
        self.assertFalse(self.core.events)

    def test_out_of_order_does_not_override_macro(self):
        lab = self.config['labs'][0]
        self.clock.advance(1)
        self.feed(lab['macro']['id'], ['Lab Coat'])
        self.core.ingest(Observation(lab['macro']['id'], 99, ['fire']))
        self.core.tick()
        self.assertFalse(self.core.events)
        self.assertEqual(self.core.labs[lab['id']].macro.labels, ['Lab Coat'])

    def test_fire_stays_latched_until_manual_ack_and_no_power_restore(self):
        self.fire()
        with self.assertRaises(ValueError): self.core.acknowledge()
        self.clock.advance(0.1)
        self.feed(self.config['labs'][0]['macro']['id'], ['Lab Coat'])
        self.core.tick()
        self.assertEqual(self.core.snapshot()['level'], 'CRITICAL')
        self.core.acknowledge()
        self.assertEqual(self.core.snapshot()['labs'][0]['power'], 'SIMULATED')
        self.assertFalse(any(c['action'] == 'power_on' for c in self.core.commands.values()))
        self.core.reset_simulated_power()
        self.assertFalse(self.core.power_latches)

    def test_unknown_evidence_blocks_ack(self):
        self.fire()
        self.clock.advance(9)
        self.core.tick()
        with self.assertRaises(ValueError): self.core.acknowledge()

    def test_manual_report_and_release(self):
        self.core.manual_report('lab-102', True)
        self.core.tick()
        self.assertEqual(len(self.core.commands), 4)
        self.core.manual_report('lab-102', False)
        self.core.tick()
        self.core.acknowledge()

    def test_manual_fire_report_survives_restart_until_explicit_release(self):
        with tempfile.TemporaryDirectory() as folder:
            self.core = BuildingCoordinator(self.config, clock=self.clock, state_dir=folder)
            self.core.manual_report('lab-101', True)
            restored = BuildingCoordinator(self.config, clock=self.clock, state_dir=folder)
            restored.tick()
            self.assertTrue(restored.labs['lab-101'].manual_fire)
            with self.assertRaises(ValueError): restored.acknowledge()
            restored.manual_report('lab-101', False)
            restored.acknowledge()

    def test_restart_does_not_replay_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            self.core = BuildingCoordinator(self.config, clock=self.clock, state_dir=folder)
            self.fire()
            dispatched = []
            restored = BuildingCoordinator(self.config, clock=self.clock, state_dir=folder,
                                            dispatch=lambda c, cb: dispatched.append(c))
            restored.ingest(Observation(self.config['labs'][0]['macro']['id'], self.clock(), ['fire'], 'SIMULATION'))
            restored.tick()
            self.assertFalse(dispatched)
            self.assertEqual(len(restored.commands), 4)
            self.assertTrue(Path(folder, 'events.jsonl').exists())
            self.assertEqual(restored.snapshot()['level'], 'CRITICAL')

    def test_pending_restart_is_unknown_not_success_or_resend(self):
        with tempfile.TemporaryDirectory() as folder:
            self.core = BuildingCoordinator(self.config, clock=self.clock, state_dir=folder, dispatch=lambda c, cb: None)
            self.fire()
            restored = BuildingCoordinator(self.config, clock=self.clock, state_dir=folder)
            self.assertTrue(all(c['status'] == 'UNKNOWN' for c in restored.commands.values()))
            self.assertEqual(restored.power_latches['lab-101'], 'UNKNOWN')

    def test_restart_rejects_removal_of_unacknowledged_source(self):
        with tempfile.TemporaryDirectory() as folder:
            self.core = BuildingCoordinator(self.config, clock=self.clock, state_dir=folder)
            self.fire()
            cfg = deepcopy(self.config)
            cfg['labs'] = cfg['labs'][1:]
            with self.assertRaises(ValueError): BuildingCoordinator(cfg, state_dir=folder)

    def test_simulated_receipt_does_not_hide_real_send(self):
        self.core.commands['real'] = dict(id='real', target_lab='lab-101', action='power_off', status='SENT')
        self.fire()
        self.assertEqual(self.core.power_latches['lab-101'], 'SENT')

    def test_physical_state_cannot_be_reset_as_simulation(self):
        self.core.power_latches['lab-101'] = 'SENT'
        with self.assertRaises(ValueError): self.core.reset_simulated_power()

    def test_config_rejects_duplicate_camera_id(self):
        cfg = deepcopy(self.config)
        cfg['labs'][0]['benches'][1]['camera']['id'] = cfg['labs'][0]['macro']['id']
        with self.assertRaises(ValueError): validate_config(cfg)

    def test_config_rejects_shared_usb_wrong_model_missing_scope(self):
        for variant in ('usb', 'model', 'scope', 'empty', 'nan'):
            cfg = deepcopy(self.config)
            if variant == 'usb':
                for node in (cfg['labs'][0]['macro'], cfg['labs'][0]['benches'][0]['camera']):
                    node.update(source_kind='camera', source=0)
            if variant == 'model': cfg['labs'][0]['macro']['model'] = 'missing.pt'
            if variant == 'scope': cfg['fire_linkage']['targets'] = ['unknown']
            if variant == 'empty': cfg['labs'][0]['benches'] = []
            if variant == 'nan': cfg['rules']['high_temperature'] = float('nan')
            with self.subTest(variant=variant), self.assertRaises(ValueError): validate_config(cfg)

    def test_per_micro_weights_are_independent(self):
        cfg = deepcopy(self.config)
        cfg['labs'][0]['benches'][1]['camera']['model'] = 'models/ppe_best.pt'
        validated = validate_config(cfg)
        self.assertNotEqual(validated['labs'][0]['benches'][0]['camera']['model'], validated['labs'][0]['benches'][1]['camera']['model'])


class RuntimeTests(unittest.TestCase):
    def test_exclusive_state_directory(self):
        from src.hierarchy.runtime import HierarchyRuntime
        with tempfile.TemporaryDirectory() as folder:
            first = HierarchyRuntime(default_config(), folder)
            with self.assertRaises(RuntimeError): HierarchyRuntime(default_config(), folder)
            first.stop()
            second = HierarchyRuntime(default_config(), folder)
            second.stop()

    def test_simulation_never_touches_real_serial(self):
        from src.hierarchy.runtime import HierarchyRuntime
        cfg = default_config()
        cfg['labs'][0]['actuator'].update(mode='serial', port='/dev/fake')
        with tempfile.TemporaryDirectory() as folder:
            runtime = HierarchyRuntime(cfg, folder)
            complete = threading.Event()
            received = []
            def callback(cid, status, detail):
                received.append(status)
                complete.set()
            with patch('serial.Serial', side_effect=AssertionError('模拟事件禁止访问串口')) as serial:
                runtime.dispatch(dict(id='test', target_lab='lab-101', action='power_off', origin='simulation'), callback)
                self.assertTrue(complete.wait(3))
                serial.assert_not_called()
            runtime.stop()
            self.assertEqual(received, ['SIMULATED'])

    def test_serial_requires_ack_and_failure_is_visible(self):
        from src.hierarchy.runtime import HierarchyRuntime
        cfg = default_config()
        cfg['labs'][0]['actuator'].update(mode='serial', port='/dev/fake')
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as folder:
                runtime = HierarchyRuntime(cfg, folder)
                complete, received = threading.Event(), []
                def callback(cid, status, detail):
                    received.append(status)
                    complete.set()
                with patch('LabSafetyMonitor.serial_comm.SerialComm') as serial:
                    link = serial.return_value
                    link.connect.return_value = True
                    link.send_power_off.return_value = True
                    link.power_off_state = 'failed_no_ack' if fail else 'acked'
                    runtime.dispatch(dict(id='test', target_lab='lab-101', action='power_off', origin='manual'), callback)
                    self.assertTrue(complete.wait(3))
                runtime.stop()
                self.assertEqual(received, ['FAILED' if fail else 'ACKNOWLEDGED'])

    def test_parser(self):
        from src.hierarchy.runtime import parse_telemetry
        for value in ['TEMP:27.6,HUMI:69.2', '27.6,69.2', '{"temp":27.6,"humi":69.2}']:
            self.assertEqual(parse_telemetry(value), (27.6, 69.2))
        for value in ['garbage', 'NaN,55', '{"temp":200,"humi":50}', '1,2,3']:
            self.assertIsNone(parse_telemetry(value))


if __name__ == '__main__':
    unittest.main()
