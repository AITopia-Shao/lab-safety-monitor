"""离屏验证真实 Qt 界面、动态拓扑和真实模型多路推理。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LAB_SAFETY_ISOLATED_ENV', '1')
from pathlib import Path
import tempfile
import time
import unittest
import cv2
import numpy as np
from src.hierarchy.config import default_config, save_config
from src.ui.hierarchy_window import QApplication, HierarchyWindow


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.config_path = self.path / 'topology.json'
        self.config = default_config()
        # 独立模拟数据，避免读取用户当前正在运行的旧仿真器。
        self.config['labs'][0]['benches'][0]['telemetry']['kind'] = 'simulation'
        save_config(self.config, self.config_path)
        self.window = HierarchyWindow(self.config_path, self.path / 'state')
        self.window.show()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.folder.cleanup()

    def wait_for(self, condition, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition(): return
            time.sleep(0.03)
        self.fail('等待界面/运行时条件超时：' + self.window.runtime.error)

    def test_fire_lifecycle_and_dynamic_topology(self):
        self.wait_for(lambda: self.window.runtime.coordinator.snapshot()['level'] == 'NORMAL')
        self.window.simulate_fire()
        self.wait_for(lambda: len(self.window.runtime.coordinator.commands) == 4
                      and all(c['status'] == 'SIMULATED' for c in self.window.runtime.coordinator.commands.values()))
        self.window.refresh()
        self.assertEqual(self.window.commands_table.rowCount(), 4)
        self.assertTrue(all(l['level'] == 'CRITICAL' for l in self.window.runtime.coordinator.snapshot()['labs']))
        self.window.clear_simulation()
        self.wait_for(lambda: all(e['condition_active'] is False for e in self.window.runtime.coordinator.events.values()))
        self.window.runtime.coordinator.acknowledge()
        self.window.runtime.coordinator.reset_simulated_power()
        self.window.add_bench()
        self.assertEqual(len(self.window.config['labs'][0]['benches']), 3)
        self.assertEqual(len(self.window.runtime.workers), 6)
        self.window.add_lab()
        self.assertEqual(len(self.window.config['labs']), 3)
        self.assertEqual(len(self.window.runtime.workers), 8)
        self.wait_for(lambda: self.window.runtime.coordinator.snapshot()['level'] == 'NORMAL')
        self.window.tree.setCurrentItem(self.window.lab_items[self.window.config['labs'][1]['id']])
        self.assertEqual(self.window.selected_lab, self.window.config['labs'][1]['id'])

    def test_real_models_one_macro_two_micro(self):
        fixture = self.path / '空白实验台 fixture.png'
        cv2.imwrite(str(fixture), np.zeros((240, 320, 3), np.uint8))
        cfg = default_config()
        cfg['labs'][0]['benches'][0]['telemetry']['kind'] = 'simulation'
        lab = cfg['labs'][0]
        nodes = [lab['macro'], *[b['camera'] for b in lab['benches']]]
        for camera in nodes:
            camera.update(source_kind='image', source=str(fixture), fps=1)
        self.window.apply_config(cfg)
        def live():
            agent = self.window.runtime.coordinator.labs[lab['id']]
            return agent.macro and agent.macro.status == 'LIVE' and all(
                b.observation and b.observation.status == 'LIVE' for b in agent.benches.values())
        self.wait_for(live, timeout=45)
        self.assertEqual(len(self.window.runtime.pool.models), 2)
        for camera in nodes:
            self.assertIsNotNone(self.window.runtime.coordinator.frame(camera['id']))
        self.assertFalse(self.window.runtime.error)


if __name__ == '__main__':
    unittest.main()
