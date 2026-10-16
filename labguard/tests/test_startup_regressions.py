"""启动脚本与画面输出回归：不打开摄像头或串口。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "LabSafetyMonitor"):
    sys.path.insert(0, str(path))


class LauncherTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "bash 启动器用于 macOS / Linux")
    def test_local_venv_and_arguments_from_another_directory(self):
        with tempfile.TemporaryDirectory(prefix="labguard launcher ") as tmp:
            base = Path(tmp)
            app = base / "project with spaces"
            python = app / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
            python.chmod(0o755)
            shutil.copyfile(ROOT / "start_linux.sh", app / "start_linux.sh")
            env = dict(os.environ, HOME=str(base / "no conda"))
            result = subprocess.run(
                ["bash", str(app / "start_linux.sh"), "--mode", "headless", "--windowed"],
                cwd=base, env=env, text=True, capture_output=True, check=True,
            )
            self.assertEqual(result.stdout.splitlines(),
                             [str(app), "run_app.py", "--mode", "headless", "--windowed"])


class JpegTests(unittest.TestCase):
    def test_cache_respects_width_and_quality(self):
        import cv2
        import numpy as np
        from src.core.engine import SafetyEngine

        frame = np.random.default_rng(42).integers(0, 256, (120, 240, 3), dtype=np.uint8)
        engine = SimpleNamespace(_lock=threading.Lock(), _frames=(frame, frame),
                                 _frame_seq=1, _jpeg_cache={})
        first = SafetyEngine.latest_jpeg(engine, width=160, quality=80)
        smaller = SafetyEngine.latest_jpeg(engine, width=80, quality=80)
        low_quality = SafetyEngine.latest_jpeg(engine, width=80, quality=20)
        image = cv2.imdecode(np.frombuffer(smaller, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape[:2], (40, 80))
        self.assertNotEqual(first, smaller)
        self.assertNotEqual(smaller, low_quality)
        self.assertIs(low_quality, SafetyEngine.latest_jpeg(engine, width=80, quality=20))


if __name__ == "__main__":
    unittest.main()
