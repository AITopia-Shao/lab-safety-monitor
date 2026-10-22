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

# 3. Windows 控制台默认 GBK：遇到打不出来的字符时替换掉，不要因为一句 print 闪退
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

# 4. 闪退记录：Python 报错和底层崩溃（C 扩展段错误等）都写进 runtime/crash.log，方便事后排查
CRASH_LOG = os.path.join(project_root, "runtime", "crash.log")


def _install_crash_log():
    import faulthandler
    import time
    import traceback
    try:
        os.makedirs(os.path.dirname(CRASH_LOG), exist_ok=True)
        f = open(CRASH_LOG, "a", encoding="utf-8", buffering=1)
        f.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')}  Python {sys.version.split()[0]} =====\n")
        faulthandler.enable(file=f, all_threads=True)
    except OSError:
        faulthandler.enable()
        return

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        f.write(text)
        sys.__stderr__.write(text)
        sys.__stderr__.write(f"\n[启动失败] 错误详情已保存到 {CRASH_LOG}\n")

    sys.excepthook = hook


def _check_qt_version():
    """PySide6 6.12 在 Python 3.10 / 3.11 上有引用计数错误，界面画几秒就会闪退（Python 3.12 起不受影响）"""
    try:
        import PySide6
    except ImportError:
        return
    ver = tuple(int(x) for x in PySide6.__version__.split(".")[:2])
    if ver >= (6, 12) and sys.version_info < (3, 12):
        print(f"[启动失败] 当前 PySide6 {PySide6.__version__} + Python {sys.version.split()[0]} 会让界面闪退。\n"
              f"  请执行：pip install \"PySide6>=6.5,<6.12\"\n"
              f"  或者改用 Python 3.12（推荐用 conda：conda env create -f environment.yml）")
        sys.exit(2)


def main():
    _install_crash_log()
    _check_qt_version()
    ap = argparse.ArgumentParser(description="实验室安全事前干预监测系统")
    ap.add_argument("--mode", choices=["desktop", "edge", "headless"], default="desktop")
    ap.add_argument("--no-web", action="store_true", help="不启动 Web 总后台")
    ap.add_argument("--windowed", action="store_true", help="小屏界面以窗口方式运行（调试用）")
    ap.add_argument("--hierarchy", action="store_true", help="打开实验楼分层母系统（独立桌面模式）")
    ap.add_argument("--hierarchy-config", help="指定分层拓扑 JSON")
    ap.add_argument("--hierarchy-state-dir", help="指定分层事件运行目录")
    args = ap.parse_args()
    if args.hierarchy and args.mode != "desktop":
        ap.error("--hierarchy 当前仅支持桌面模式；原 edge/headless 模式保持独立")

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

    if args.hierarchy:
        from src.config import cfg, resolve_path
        from src.core.events import events
        from src.ui.hierarchy_window import HierarchyWindow

        def audit(record):
            events.log(title="分层系统：" + record.get("title", record["kind"]),
                       message=record.get("detail", record.get("message", "")),
                       level="INFO", kind="hierarchy_" + record["kind"].lower(), details=record)

        path = args.hierarchy_config or resolve_path(cfg("hierarchy.topology_file"))
        state = args.hierarchy_state_dir or resolve_path(cfg("hierarchy.state_dir"))
        window = HierarchyWindow(path, state, event_sink=audit)
        window.show()
        sys.exit(app.exec())

    from src.core.engine import SafetyEngine, attach_extensions
    engine = attach_extensions(SafetyEngine())

    if not args.no_web:
        try:
            from src.web.server import start_server
        except ImportError as e:   # 缺 fastapi 等网页依赖时只关掉网页，安全核心和界面照常运行
            print(f"[Web] 网页依赖没装全（{e}），总后台未启动。请执行：pip install fastapi uvicorn")
        else:
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
