#!/usr/bin/env python3
"""
实验室安全事前干预监测系统 - 统一主入口
"""
import sys
import os

# 1. 自动环境引导：确保即使直接使用 python 运行也能自动扫描识别工程内与 Conda/venv 依赖
try:
    import src.bootstrap
except Exception:
    pass

# 2. 移除可能导致 Qt 崩溃的 OpenCV 残留环境变量
if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ and "cv2" in os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH", ""):
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

lsm_dir = os.path.join(project_root, "LabSafetyMonitor")
if lsm_dir not in sys.path:
    sys.path.insert(0, lsm_dir)

src_dir = os.path.join(project_root, "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

from src.ui.main_window import MainWindow, load_pixel_font

if __name__ == "__main__":
    app = QApplication(sys.argv)
    load_pixel_font()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
