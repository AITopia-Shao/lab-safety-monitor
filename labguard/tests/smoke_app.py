"""三种启动模式的集成检查：使用生成的图片，不打开真实摄像头或串口。

运行：python tests/smoke_app.py（需要本机回环网络和 Qt offscreen 插件）
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def child(mode):
    import copy
    import json
    import socket
    import urllib.error
    import urllib.request

    import cv2
    import numpy as np

    sys.path.insert(0, str(ROOT))
    import run_app
    from src import config
    from src.core.engine import SafetyEngine
    from src.web import server
    try:
        from PySide6.QtCore import QCoreApplication, QTimer
    except ImportError:
        from PyQt6.QtCore import QCoreApplication, QTimer

    with tempfile.TemporaryDirectory(prefix="labguard_smoke_") as tmp:
        base = Path(tmp)
        image = base / "input.png"
        assert cv2.imwrite(str(image), np.full((120, 160, 3), 128, dtype=np.uint8))
        # 占住首选端口，同时验证上游的端口自动顺延功能。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            values = copy.deepcopy(config.DEFAULTS)
            values["camera"].update(pano=str(image), desk=str(image))
            values["serial"]["port"] = str(base / "no-hardware-port")
            values["events"].update(db=str(base / "events.db"), snapshot_dir=str(base / "snaps"))
            values["web"].update(host="127.0.0.1", port=port, token="LocalSmokeToken")
            values["agent"].update(route="off", auto_review=False)
            values["ui"]["wave_fps"] = 0
            config._config = values
            config._override_path = lambda: str(base / "override.yaml")
            run_app.CRASH_LOG = str(base / "crash.log")
            resolve = server.resolve_path
            server.resolve_path = lambda p: str(base / Path(p).name) if p.startswith("runtime") else resolve(p)
            errors = []
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            original_start = SafetyEngine.start

            def start(engine):
                original_start(engine)

                def verify():
                    try:
                        assert not engine.dual_system.vision_errors, engine.dual_system.vision_errors
                        assert engine.frame_seq > 0, "未收到推理画面"
                        assert engine.latest_jpeg("pano"), "未生成 JPEG"
                        assert server.web_port() > port, "未避开被占用端口"
                        url = f"http://127.0.0.1:{server.web_port()}"
                        for path in ("/", "/static/app.js", "/api/ping", "/api/snapshot", "/api/frame/desk.jpg"):
                            req = urllib.request.Request(url + path, headers={"X-Token": "LocalSmokeToken"})
                            with opener.open(req, timeout=3) as response:
                                assert response.status == 200, path
                                data = response.read()
                                if path == "/api/snapshot":
                                    snap = json.loads(data)
                                    assert snap["vision"]["ok"] and "env" in snap
                        try:
                            opener.open(url + "/api/snapshot", timeout=3)
                        except urllib.error.HTTPError as exc:
                            assert exc.code == 401
                        else:
                            raise AssertionError("缺少口令仍可读取快照")
                        if mode != "headless":
                            assert any(w.isVisible() for w in QCoreApplication.instance().topLevelWidgets())
                    except Exception as exc:
                        errors.append(repr(exc))
                    finally:
                        QCoreApplication.instance().quit()

                QTimer.singleShot(4000, verify)

            SafetyEngine.start = start
            sys.argv = [str(ROOT / "run_app.py"), "--mode", mode, "--windowed"]
            try:
                run_app.main()
            except SystemExit as exc:
                assert exc.code == 0, exc.code
            assert not errors, errors
            print(f"{mode}: 启动、模型推理、网页鉴权、JPEG 和端口顺延通过")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        with tempfile.TemporaryDirectory(prefix="labguard_smoke_cache_") as cache:
            env.update(YOLO_CONFIG_DIR=cache, MPLCONFIGDIR=cache)
            for mode in ("desktop", "edge", "headless"):
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", mode],
                                        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
                if result.returncode:
                    print(result.stdout, result.stderr)
                    sys.exit(result.returncode)
                print(result.stdout.splitlines()[-1])
