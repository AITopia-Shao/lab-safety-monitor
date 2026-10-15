#!/usr/bin/env python3
"""
实验室安全事前干预监测系统 - 统一主入口

运行模式：
  python run_app.py                 # desktop：PC 大窗口（中控）+ Web 总后台，PC 单机演示 / 兜底
  python run_app.py --mode edge     # edge：香橙派小触屏 Kiosk + Web 总后台（正式部署）
  python run_app.py --mode headless # headless：无屏幕，只跑安全引擎 + Web 总后台
可选：--no-web 不启动总后台；--windowed 小屏以窗口方式运行（PC 上调试小屏界面）
"""
import argparse
import os
import sys

# 1. 自动环境引导：确保即使直接使用 python 运行也能自动扫描识别工程内与 Conda/venv 依赖
try:
    import src.bootstrap  # noqa: F401
except Exception:
    pass

# 2. 移除可能导致 Qt 崩溃的 OpenCV 残留环境变量
if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ and "cv2" in os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH", ""):
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

project_root = os.path.dirname(os.path.abspath(__file__))
for p in (project_root, os.path.join(project_root, "LabSafetyMonitor"), os.path.join(project_root, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)


def main():
    ap = argparse.ArgumentParser(description="实验室安全事前干预监测系统")
    ap.add_argument("--mode", choices=["desktop", "edge", "headless"], default="desktop")
    ap.add_argument("--no-web", action="store_true", help="不启动 Web 总后台")
    ap.add_argument("--windowed", action="store_true", help="小屏界面以窗口方式运行（调试用）")
    args = ap.parse_args()

    if args.mode == "headless":
        try:
            from PySide6.QtCore import QCoreApplication as App
        except ImportError:
            from PyQt6.QtCore import QCoreApplication as App
    else:
        try:
            from PySide6.QtWidgets import QApplication as App
        except ImportError:
            from PyQt6.QtWidgets import QApplication as App
    app = App(sys.argv)

    from src.core.engine import SafetyEngine, attach_extensions
    engine = attach_extensions(SafetyEngine())

    if not args.no_web:
        from src.web.server import start_server
        start_server(engine)

    window = None
    if args.mode == "desktop":
        from src.ui.main_window import MainWindow, load_pixel_font
        load_pixel_font()
        window = MainWindow(engine=engine)
        window.show()
    elif args.mode == "edge":
        from src.ui.main_window import load_pixel_font
        from src.ui.kiosk import KioskWindow
        load_pixel_font()
        window = KioskWindow(engine, fullscreen=not args.windowed)

    engine.start()
    code = app.exec()
    engine.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
