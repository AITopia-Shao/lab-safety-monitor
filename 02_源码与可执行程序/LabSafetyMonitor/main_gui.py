"""
实验室安全监控系统可视化主入口 (已升级至工业新粗野主义遥测与多模态边缘状态机)
LabSafetyMonitor/main_gui.py
"""
import sys
import os

# 将项目根目录与当前目录加入 sys.path，保证跨目录调用与直接启动均正常
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# 从 src.ui.main_window 导入全面重构升级的主窗口与组件
from src.ui.main_window import (
    MainWindow,
    TelemetryCard,
    WaveBackgroundWidget,
    InferenceThread,
    ReviewDialog,
    load_pixel_font,
    get_qss,
    apply_hard_shadow,
)

# 保持对原有 MainGUI 类名的向后兼容别名
MainGUI = MainWindow

if __name__ == '__main__':
    # 优先使用 PySide6，回退至 PyQt6
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    load_pixel_font()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
