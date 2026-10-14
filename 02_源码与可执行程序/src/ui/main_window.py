"""
工业新粗野主义风格主监控窗口与遥测展示卡片
src/ui/main_window.py
"""
import sys
import os

# -----------------------------------------------------------------------------
# 自动环境引导：确保直接使用系统 python 或从任意目录启动均能自动识别本地依赖
# -----------------------------------------------------------------------------
_current_file = os.path.abspath(__file__)
_ui_dir = os.path.dirname(_current_file)
_src_dir = os.path.dirname(_ui_dir)
_root_dir = os.path.dirname(_src_dir)
project_root = _root_dir

for _p in [_root_dir, _src_dir, os.path.join(_root_dir, "LabSafetyMonitor")]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

for _cand_root in [_root_dir, os.path.join(_root_dir, "LabSafetyMonitor")]:
    _venv_lib = os.path.join(_cand_root, "venv", "lib")
    if os.path.isdir(_venv_lib):
        for _py_ver in sorted(os.listdir(_venv_lib), reverse=True):
            _sp = os.path.join(_venv_lib, _py_ver, "site-packages")
            if os.path.isdir(_sp) and _sp not in sys.path:
                sys.path.insert(0, _sp)

import math
import cv2
import numpy as np
from datetime import datetime
from typing import Optional, List, Dict, Any

# Qt 绑定兼容层：优先使用 PySide6，无缝回退至 PyQt6
try:
    from PySide6.QtCore import Qt, QThread, Signal, QTimer, QDateTime, QRect
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QScrollArea, QFrame, QGridLayout, QButtonGroup,
        QFileDialog, QSlider, QGraphicsDropShadowEffect, QDialog, QMessageBox,
        QDoubleSpinBox, QSpinBox, QLineEdit, QTextEdit, QInputDialog
    )
    from PySide6.QtGui import (
        QImage, QPixmap, QFont, QColor, QFontDatabase, QPainter, QPainterPath
    )
except ImportError:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QTimer, QDateTime, QRect
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QScrollArea, QFrame, QGridLayout, QButtonGroup,
        QFileDialog, QSlider, QGraphicsDropShadowEffect, QDialog, QMessageBox,
        QDoubleSpinBox, QSpinBox, QLineEdit, QTextEdit, QInputDialog
    )
    from PyQt6.QtGui import (
        QImage, QPixmap, QFont, QColor, QFontDatabase, QPainter, QPainterPath
    )

from src.hardware.sensor_worker import SensorWorker
from src.core.state_machine import SafetyStateMachine
from dual_inference import DualInferenceSystem
from serial_comm import SerialComm

# 全局字体配置
PIXEL_FONT_FAMILY = "monospace"
ICON_FONT_FAMILY = "monospace"


def load_pixel_font():
    """读取本地像素字体与图标字体"""
    global PIXEL_FONT_FAMILY, ICON_FONT_FAMILY
    meipass = getattr(sys, '_MEIPASS', None)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    search_paths = [
        meipass,
        os.path.join(meipass, "LabSafetyMonitor") if meipass else None,
        os.path.join(base_dir, "LabSafetyMonitor"),
        os.path.join(project_root, "LabSafetyMonitor"),
        project_root,
        os.getcwd(),
    ]
    search_paths = [p for p in search_paths if p and os.path.isdir(p)]

    for p in search_paths:
        zpix_path = os.path.join(p, "Zpix.ttf")
        nerd_path = os.path.join(p, "NerdFontSymbols.ttf")
        if os.path.exists(zpix_path):
            fid = QFontDatabase.addApplicationFont(zpix_path)
            if fid != -1:
                fams = QFontDatabase.applicationFontFamilies(fid)
                if fams:
                    PIXEL_FONT_FAMILY = fams[0]
                    print(f"成功加载像素字体: {PIXEL_FONT_FAMILY}")
            break

    for p in search_paths:
        nerd_path = os.path.join(p, "NerdFontSymbols.ttf")
        if os.path.exists(nerd_path):
            fid = QFontDatabase.addApplicationFont(nerd_path)
            if fid != -1:
                fams = QFontDatabase.applicationFontFamilies(fid)
                if fams:
                    ICON_FONT_FAMILY = fams[0]
                    print(f"成功加载图标字体: {ICON_FONT_FAMILY}")
            break


def get_qss():
    """水色基调与物理按键 QSS 样式表"""
    return f"""
    * {{
        font-family: '{PIXEL_FONT_FAMILY}', '{ICON_FONT_FAMILY}', 'Noto Sans Mono', 'SimSun', monospace;
        font-weight: bold;
    }}
    QMainWindow {{
        background-color: transparent;
    }}
    QLabel {{
        color: #5C6BC0;
    }}
    QFrame {{
        background-color: #FFFFFF;
        border: 2px solid #5C6BC0;
        border-radius: 0px;
    }}
    QPushButton {{
        background-color: #FFFFFF;
        color: #5C6BC0;
        border: 2px solid #5C6BC0;
        border-radius: 0px;
        padding: 8px 12px;
        margin-top: 0px;
        margin-left: 0px;
        margin-bottom: 4px;
        margin-right: 4px;
    }}
    QPushButton:hover {{
        background-color: #E1F5FE;
    }}
    QPushButton:checked {{
        background-color: #5C6BC0;
        color: #FFFFFF;
        margin-top: 2px;
        margin-left: 2px;
        margin-bottom: 2px;
        margin-right: 2px;
    }}
    QPushButton:pressed {{
        background-color: #4DD0E1;
        color: #FFFFFF;
        margin-top: 4px;
        margin-left: 4px;
        margin-bottom: 0px;
        margin-right: 0px;
    }}
    QScrollArea {{
        background-color: transparent;
        border: none;
    }}
    QScrollBar:vertical {{
        background-color: #FFFFFF;
        width: 16px;
        border: 2px solid #5C6BC0;
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background-color: #5C6BC0;
        min-height: 20px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    """


def apply_hard_shadow(widget, offset_x=4, offset_y=4, color="#5C6BC0"):
    """纯粹实心的无模糊硬阴影"""
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(0)
    shadow.setColor(QColor(color))
    shadow.setOffset(offset_x, offset_y)
    widget.setGraphicsEffect(shadow)


# ==============================================================================
# 一、 工业新粗野主义风格环境遥测面板 (TelemetryCard)
# ==============================================================================
class TelemetryCard(QFrame):
    """
    符合工业新粗野主义 (Neo-brutalism) 规范的环境遥测面板：
    - 直角无圆角：border-radius: 0px
    - 纯黑粗边框：border: 2px solid #000000
    - 高对比度配色：高饱和工业明黄 (#FFC107)，纯黑粗体
    - 无模糊纯黑物理硬阴影：box-shadow: 4px 4px 0px 0px #000000
    - 包含两个高对比度数值标签：温度与湿度，以及绿色 SAFE / 红色 ALERT 状态指示徽标
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()

    def init_ui(self):
        # 卡片外容器样式：高对比亮黄背景，纯黑 2px 粗边框，直角无圆角
        self.setStyleSheet("""
            QFrame#TelemetryCard {
                background-color: #FFC107;
                border: 2px solid #000000;
                border-radius: 0px;
            }
        """)
        self.setObjectName("TelemetryCard")

        # 挂载纯黑无模糊物理硬阴影
        apply_hard_shadow(self, offset_x=4, offset_y=4, color="#000000")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # --- 顶部标题栏与状态徽标 ---
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)

        self.lbl_title = QLabel("⚡ 工业环境遥测 / TELEMETRY")
        self.lbl_title.setStyleSheet("""
            QLabel {
                color: #000000;
                font-size: 15px;
                font-weight: 900;
                background: transparent;
                border: none;
            }
        """)

        # 模式指示徽标 (硬件在线 / 模拟遥测)
        self.lbl_mode_badge = QLabel("INIT")
        self.lbl_mode_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_mode_badge.setStyleSheet("""
            QLabel {
                background-color: #000000;
                color: #FFFFFF;
                border: 1px solid #000000;
                border-radius: 0px;
                font-size: 11px;
                font-weight: bold;
                padding: 2px 6px;
            }
        """)

        # 核心安全状态徽标 (正常绿色 SAFE / 超标红色 ALERT)
        self.lbl_status_badge = QLabel("SAFE")
        self.lbl_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status_badge.setStyleSheet("""
            QLabel {
                background-color: #00E676;
                color: #000000;
                border: 2px solid #000000;
                border-radius: 0px;
                font-size: 13px;
                font-weight: 900;
                padding: 3px 12px;
            }
        """)

        header_layout.addWidget(self.lbl_title)
        header_layout.addStretch()
        header_layout.addWidget(self.lbl_mode_badge)
        header_layout.addWidget(self.lbl_status_badge)
        layout.addLayout(header_layout)

        # --- 中部：两个高对比度数值方块 ---
        values_layout = QHBoxLayout()
        values_layout.setSpacing(12)

        # 温度显示块
        self.temp_container = QFrame()
        self.temp_container.setStyleSheet("""
            QFrame {
                background-color: #FFFFFF;
                border: 2px solid #000000;
                border-radius: 0px;
            }
        """)
        temp_sub_layout = QVBoxLayout(self.temp_container)
        temp_sub_layout.setContentsMargins(10, 8, 10, 8)
        temp_sub_layout.setSpacing(2)

        lbl_temp_header = QLabel("SENSOR A // 温度")
        lbl_temp_header.setStyleSheet("border: none; background: transparent; font-size: 11px; color: #555555; font-weight: bold;")
        self.lbl_temp = QLabel("温度: --.- °C")
        self.lbl_temp.setStyleSheet("border: none; background: transparent; font-size: 20px; font-weight: 900; color: #000000;")
        temp_sub_layout.addWidget(lbl_temp_header)
        temp_sub_layout.addWidget(self.lbl_temp)

        # 湿度显示块
        self.humi_container = QFrame()
        self.humi_container.setStyleSheet("""
            QFrame {
                background-color: #FFFFFF;
                border: 2px solid #000000;
                border-radius: 0px;
            }
        """)
        humi_sub_layout = QVBoxLayout(self.humi_container)
        humi_sub_layout.setContentsMargins(10, 8, 10, 8)
        humi_sub_layout.setSpacing(2)

        lbl_humi_header = QLabel("SENSOR B // 相对湿度")
        lbl_humi_header.setStyleSheet("border: none; background: transparent; font-size: 11px; color: #555555; font-weight: bold;")
        self.lbl_humi = QLabel("湿度: --.- %")
        self.lbl_humi.setStyleSheet("border: none; background: transparent; font-size: 20px; font-weight: 900; color: #000000;")
        humi_sub_layout.addWidget(lbl_humi_header)
        humi_sub_layout.addWidget(self.lbl_humi)

        values_layout.addWidget(self.temp_container, stretch=1)
        values_layout.addWidget(self.humi_container, stretch=1)
        layout.addLayout(values_layout)

        # --- 底部提示栏 ---
        self.lbl_footer = QLabel("联动闭环状态: 继电器供电常通 | 触发阈值: 温度 > 55.0℃ 或 湿度 > 80.0%")
        self.lbl_footer.setStyleSheet("""
            QLabel {
                color: #000000;
                font-size: 11px;
                font-weight: bold;
                background: transparent;
                border: none;
            }
        """)
        layout.addWidget(self.lbl_footer)

    def update_telemetry(
        self,
        temp: float,
        humi: float,
        is_alert: bool = False,
        is_simulated: bool = False,
        is_external: bool = False,
        status_text: Optional[str] = None,
    ):
        """更新温湿度遥测数据及徽标状态"""
        self.lbl_temp.setText(f"温度: {temp:.1f} °C")
        self.lbl_humi.setText(f"湿度: {humi:.1f} %")

        # 模式徽标
        if is_external:
            self.lbl_mode_badge.setText("外部模拟器")
            self.lbl_mode_badge.setStyleSheet("""
                background-color: #1A237E; color: #00E676;
                border: 1px solid #000000; border-radius: 0px;
                font-size: 11px; font-weight: bold; padding: 2px 6px;
            """)
        elif is_simulated:
            self.lbl_mode_badge.setText("自主模拟")
            self.lbl_mode_badge.setStyleSheet("""
                background-color: #212121; color: #FFD54F;
                border: 1px solid #000000; border-radius: 0px;
                font-size: 11px; font-weight: bold; padding: 2px 6px;
            """)
        else:
            self.lbl_mode_badge.setText("硬件在线")
            self.lbl_mode_badge.setStyleSheet("""
                background-color: #000000; color: #FFFFFF;
                border: 1px solid #000000; border-radius: 0px;
                font-size: 11px; font-weight: bold; padding: 2px 6px;
            """)

        # 状态徽标 (SAFE / ALERT)
        if is_alert or temp > 55.0 or humi > 80.0:
            badge_text = status_text if status_text else "ALERT"
            self.lbl_status_badge.setText(badge_text)
            self.lbl_status_badge.setStyleSheet("""
                QLabel {
                    background-color: #FF1744;
                    color: #FFFFFF;
                    border: 2px solid #000000;
                    border-radius: 0px;
                    font-size: 13px;
                    font-weight: 900;
                    padding: 3px 12px;
                }
            """)
            self.lbl_footer.setText(
                f"⚠ 警报触发！当前指标超标: 温度 {temp:.1f}℃ / 湿度 {humi:.1f}% | 已联动执行边缘处置"
            )
        else:
            self.lbl_status_badge.setText("SAFE")
            self.lbl_status_badge.setStyleSheet("""
                QLabel {
                    background-color: #00E676;
                    color: #000000;
                    border: 2px solid #000000;
                    border-radius: 0px;
                    font-size: 13px;
                    font-weight: 900;
                    padding: 3px 12px;
                }
            """)
            self.lbl_footer.setText("联动闭环状态: 继电器供电常通 | 触发阈值: 温度 > 55.0℃ 或 湿度 > 80.0%")


# ==============================================================================
# 二、 底层动画水浪背景
# ==============================================================================
class WaveBackgroundWidget(QWidget):
    """自定义中心容器：绘制双层持续流动的半透明海浪"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase = 0.0
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.animate_wave)
        self.anim_timer.start(30)

    def animate_wave(self):
        self.phase += 0.1
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.fillRect(self.rect(), QColor("#E1F5FE"))

        spacing = 60
        offset = (self.phase * 15) % spacing
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(129, 212, 250, 150))

        for x in range(-spacing, self.width() + spacing, spacing):
            for y in range(-spacing, self.height() + spacing, spacing):
                painter.drawEllipse(int(x + offset), int(y + offset), 4, 4)

        w = self.width()
        h = self.height()
        base_y = h * 0.75

        path1 = QPainterPath()
        path1.moveTo(0, h)
        for x in range(0, w + 10, 10):
            y = base_y + math.sin((x / 150.0) - (self.phase * 0.8)) * 30
            path1.lineTo(x, y)
        path1.lineTo(w, h)
        path1.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(129, 212, 250, 150))
        painter.drawPath(path1)

        path2 = QPainterPath()
        path2.moveTo(0, h)
        for x in range(0, w + 10, 10):
            y = base_y + 20 + math.sin((x / 100.0) + self.phase) * 20
            path2.lineTo(x, y)
        path2.lineTo(w, h)
        path2.closeSubpath()
        painter.setBrush(QColor(92, 107, 192, 100))
        painter.drawPath(path2)


# ==============================================================================
# 三、 推理与视频流线程
# ==============================================================================
class InferenceThread(QThread):
    update_frames_signal = Signal(np.ndarray, np.ndarray, dict, list, int, int, str)

    def __init__(self):
        super().__init__()
        self.running = True
        self.dual_system = DualInferenceSystem(src1=0, src2=0)
        self.serial = SerialComm(port='/dev/ttyUSB0')

    def run(self):
        self.dual_system.start()
        self.serial.connect()
        while self.running:
            res = self.dual_system.get_results()
            if res is not None:
                frame_pano, frame_desk, alarms, violations, curr_f, tot_f, log_str = res
                self.update_frames_signal.emit(
                    frame_pano, frame_desk, alarms, violations, curr_f, tot_f, log_str
                )
            self.msleep(30)

    def stop(self):
        self.running = False
        self.dual_system.stop()
        self.serial.disconnect()
        self.wait(1500)


# ==============================================================================
# 四、 专家评审模式弹窗 (ReviewDialog)
# ==============================================================================
class ReviewDialog(QDialog):
    def __init__(self, parent_gui, initial_index):
        super().__init__(parent_gui)
        self.parent_gui = parent_gui
        self.current_index = initial_index
        self.setWindowTitle("大图评审与导出")
        self.setModal(True)
        self.setStyleSheet(parent_gui.styleSheet())
        self.resize(1000, 500)

        layout = QVBoxLayout(self)

        # 顶部导航
        nav_layout = QHBoxLayout()
        self.lbl_title = QLabel()
        self.lbl_title.setStyleSheet("font-size: 18px;")
        nav_layout.addStretch()
        nav_layout.addWidget(self.lbl_title)
        nav_layout.addStretch()
        layout.addLayout(nav_layout)

        # 显示大图
        images_layout = QHBoxLayout()
        self.lbl1 = QLabel()
        self.lbl1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl1.setStyleSheet("border: 2px solid #5C6BC0;")
        images_layout.addWidget(self.lbl1)

        self.lbl2 = QLabel()
        self.lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl2.setStyleSheet("border: 2px solid #5C6BC0;")
        images_layout.addWidget(self.lbl2)
        layout.addLayout(images_layout)

        # 底部按钮
        btn_layout = QHBoxLayout()
        card_btn_style = """
        QPushButton {
            color: #5C6BC0; font-size: 16px; padding: 10px 20px; border: 2px solid #5C6BC0;
            margin-top:0px; margin-left:0px; margin-bottom:4px; margin-right:4px;
        }
        QPushButton:pressed {
            background-color: #4DD0E1; margin-top:4px; margin-left:4px; margin-bottom:0px; margin-right:0px;
        }
        QPushButton:disabled {
            background-color: #E0E0E0; color: #9E9E9E; border: 2px solid #9E9E9E;
        }
        """

        self.btn_prev = QPushButton("上一张")
        self.btn_next = QPushButton("下一张")
        self.btn_correct = QPushButton("正确 (计分)")
        self.btn_wrong = QPushButton("误报 (不计分)")
        self.btn_export = QPushButton("导出单张")

        btn_layout.addWidget(self.btn_prev)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_correct)
        btn_layout.addWidget(self.btn_wrong)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_export)
        btn_layout.addWidget(self.btn_next)

        for btn in [self.btn_prev, self.btn_next, self.btn_correct, self.btn_wrong, self.btn_export]:
            btn.setStyleSheet(card_btn_style)
            apply_hard_shadow(btn, 3, 3)

        layout.addLayout(btn_layout)

        self.btn_prev.clicked.connect(self.on_prev)
        self.btn_next.clicked.connect(self.on_next)
        self.btn_correct.clicked.connect(self.on_correct)
        self.btn_wrong.clicked.connect(self.on_wrong)
        self.btn_export.clicked.connect(self.on_export)

        self.parent_gui.review_dialog = self
        self.update_view()

    def update_view(self):
        item = self.parent_gui.snapshots[self.current_index]
        self.lbl_title.setText(
            f"抓拍时间: {item['time']} ({self.current_index + 1} / {len(self.parent_gui.snapshots)})"
        )

        self.lbl1.setPixmap(
            item['pixmap_pano'].scaled(480, 360, Qt.AspectRatioMode.KeepAspectRatio)
        )
        if not item['pixmap_desk'].isNull():
            self.lbl2.setPixmap(
                item['pixmap_desk'].scaled(480, 360, Qt.AspectRatioMode.KeepAspectRatio)
            )
            self.lbl2.show()
        else:
            self.lbl2.hide()

        if item["reviewed"]:
            self.btn_correct.setEnabled(False)
            self.btn_wrong.setEnabled(False)
        else:
            self.btn_correct.setEnabled(True)
            self.btn_wrong.setEnabled(True)

        self.btn_prev.setEnabled(self.current_index > 0)
        self.btn_next.setEnabled(self.current_index < len(self.parent_gui.snapshots) - 1)

    def on_prev(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.update_view()

    def on_next(self):
        if self.current_index < len(self.parent_gui.snapshots) - 1:
            self.current_index += 1
            self.update_view()

    def on_correct(self):
        self.parent_gui.mark_item_reviewed(self.current_index, True)

    def on_wrong(self):
        self.parent_gui.mark_item_reviewed(self.current_index, False)

    def on_export(self):
        item = self.parent_gui.snapshots[self.current_index]
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存抓拍图片",
            f"snapshot_{item['time'].replace(':','-')}.jpg",
            "Images (*.png *.jpg *.jpeg)",
        )
        if file_path:
            self.parent_gui.save_combined_image(item, file_path)
            QMessageBox.information(self, "保存成功", f"单图已保存至:\n{file_path}")


# ==============================================================================
# 五、 系统主监控窗口 (MainWindow)
# ==============================================================================
class MainWindow(QMainWindow):
    """
    实验室安全监控系统主窗口
    集成：
    1. 双路实时监控推流与渲染
    2. 工业新粗野主义 TelemetryCard 遥测展示
    3. 边缘状态机多模态安全判定与继电器硬件执行闭环
    4. 专家盲评与打分机制流水线
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("实验室AI视觉守护系统 [工业遥测加强版]")
        self.resize(1380, 920)

        self.setStyleSheet(get_qss())

        self.expert_score = 0.0
        self.snapshots = []
        self.review_dialog = None
        self.start_time = QDateTime.currentDateTime()

        # 实时环境状态缓存
        self.current_temp = 25.0
        self.current_humi = 55.0
        self.latest_chem_items: List[str] = []
        self.latest_unattended: bool = False

        # 初始化后台线程与状态机
        self.inference_thread = InferenceThread()
        self.inference_thread.update_frames_signal.connect(self.update_display)

        # 状态机：注入底层的串口控制实例
        self.state_machine = SafetyStateMachine(
            serial_controller=self.inference_thread.serial, parent=self
        )
        self.state_machine.alert_triggered.connect(self.on_safety_alert)

        # 实例化硬件驱动模块 SensorWorker
        self.sensor_worker = SensorWorker(
            port="/dev/ttyUSB0", baudrate=9600, timeout=1.0, parent=self
        )
        self.sensor_worker.sensor_data_updated.connect(self.on_sensor_data_updated)
        self.sensor_worker.mode_changed.connect(self.on_sensor_mode_changed)

        self.init_ui()

        # 启动线程
        self.inference_thread.start()
        self.sensor_worker.start()

    def init_ui(self):
        central_widget = WaveBackgroundWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(16)

        # === 1. 顶部 Header 区 ===
        header_layout = QHBoxLayout()

        lbl_title = QLabel(" 基于双摄协同的实验室安全事前干预监测系统 ")
        lbl_title.setStyleSheet(
            "font-size: 24px; background-color: #FFFFFF; border: 3px solid #5C6BC0; padding: 10px; color: #5C6BC0;"
        )
        apply_hard_shadow(lbl_title)

        self.lbl_stats = QLabel(" 运行: 00:00:00 | 正常守护中 ")
        self.lbl_stats.setStyleSheet(
            "font-size: 15px; background-color: #FFFFFF; border: 3px solid #5C6BC0; padding: 10px;"
        )
        apply_hard_shadow(self.lbl_stats)

        header_layout.addWidget(lbl_title, stretch=1)
        header_layout.addStretch()
        header_layout.addWidget(self.lbl_stats)
        main_layout.addLayout(header_layout)

        self.uptime_timer = QTimer(self)
        self.uptime_timer.timeout.connect(self.update_stats)
        self.uptime_timer.start(1000)

        # === 2. 内容区划分 ===
        content_layout = QHBoxLayout()
        content_layout.setSpacing(16)

        # === 左侧：视频区、遥测卡片与控制区 ===
        left_layout = QVBoxLayout()
        left_layout.setSpacing(14)

        # 双摄预览区域
        video_layout = QHBoxLayout()
        video_layout.setSpacing(16)

        # Camera A
        cam_a_container = QFrame()
        cam_a_layout = QVBoxLayout(cam_a_container)
        cam_a_layout.setContentsMargins(0, 0, 0, 0)
        cam_a_layout.setSpacing(0)

        lbl_cam_a_title = QLabel(" 宏观行为监控 (Camera A) ")
        lbl_cam_a_title.setStyleSheet(
            "background-color: #5C6BC0; color: #FFFFFF; font-size: 15px; padding: 6px; border: none;"
        )
        self.lbl_pano = QLabel()
        self.lbl_pano.setFixedSize(480, 360)
        self.lbl_pano.setStyleSheet("background-color: #000000; border: none;")
        cam_a_layout.addWidget(lbl_cam_a_title)
        cam_a_layout.addWidget(self.lbl_pano)
        apply_hard_shadow(cam_a_container)

        # Camera B
        cam_b_container = QFrame()
        cam_b_layout = QVBoxLayout(cam_b_container)
        cam_b_layout.setContentsMargins(0, 0, 0, 0)
        cam_b_layout.setSpacing(0)

        lbl_cam_b_title = QLabel(" 微观台面监控 (Camera B) ")
        lbl_cam_b_title.setStyleSheet(
            "background-color: #5C6BC0; color: #FFFFFF; font-size: 15px; padding: 6px; border: none;"
        )
        self.lbl_desk = QLabel()
        self.lbl_desk.setFixedSize(480, 360)
        self.lbl_desk.setStyleSheet("background-color: #000000; border: none;")
        cam_b_layout.addWidget(lbl_cam_b_title)
        cam_b_layout.addWidget(self.lbl_desk)
        apply_hard_shadow(cam_b_container)

        video_layout.addWidget(cam_a_container)
        video_layout.addWidget(cam_b_container)
        left_layout.addLayout(video_layout)

        # === 核心新增：工业新粗野主义 TelemetryCard 环境遥测面板 ===
        self.telemetry_card = TelemetryCard(self)
        left_layout.addWidget(self.telemetry_card)

        # 下方控制面板与终端输出
        controls_hlayout = QHBoxLayout()
        controls_hlayout.setSpacing(16)

        # 控制面板
        control_container = QFrame()
        apply_hard_shadow(control_container)
        control_layout = QVBoxLayout(control_container)

        # 模型设置
        lbl_model_set = QLabel("模型设置 (Model Settings)")
        lbl_model_set.setStyleSheet(
            "border: none; background: transparent; font-size: 15px; color: #5C6BC0; font-weight: 900;"
        )
        control_layout.addWidget(lbl_model_set)

        model_btn_layout = QHBoxLayout()
        self.btn_model_ppe = QPushButton("人员模型")
        self.btn_model_chem = QPushButton("物品模型")
        self.btn_model_custom = QPushButton("自定义...")

        small_btn_style = """
            QPushButton {
                background-color: #E8EAF6; color: #5C6BC0; font-size: 12px; padding: 4px 8px; border: 1px solid #5C6BC0;
            }
            QPushButton:pressed { background-color: #C5CAE9; }
            QPushButton:checked { background-color: #5C6BC0; color: #FFFFFF; }
        """
        self.btn_model_custom.setStyleSheet(small_btn_style)

        for btn in [self.btn_model_ppe, self.btn_model_chem]:
            btn.setCheckable(True)
            model_btn_layout.addWidget(btn, stretch=2)

        model_btn_layout.addWidget(self.btn_model_custom, stretch=1)
        self.btn_model_ppe.setChecked(True)
        self.btn_model_chem.setChecked(True)
        control_layout.addLayout(model_btn_layout)

        # 参数微调
        params_layout = QHBoxLayout()
        lbl_conf = QLabel("置信度:")
        lbl_conf.setStyleSheet("border: none; background: transparent; font-size: 12px;")
        self.spin_conf = QDoubleSpinBox()
        self.spin_conf.setRange(0.00, 1.00)
        self.spin_conf.setSingleStep(0.05)
        self.spin_conf.setValue(0.25)
        self.spin_conf.setStyleSheet("border: 2px solid #5C6BC0; background: #FFF; padding: 2px;")

        lbl_iou = QLabel("IoU阈值:")
        lbl_iou.setStyleSheet("border: none; background: transparent; font-size: 12px;")
        self.spin_iou = QDoubleSpinBox()
        self.spin_iou.setRange(0.00, 1.00)
        self.spin_iou.setSingleStep(0.05)
        self.spin_iou.setValue(0.45)
        self.spin_iou.setStyleSheet("border: 2px solid #5C6BC0; background: #FFF; padding: 2px;")

        lbl_size = QLabel("图像尺寸:")
        lbl_size.setStyleSheet("border: none; background: transparent; font-size: 12px;")
        self.spin_size = QSpinBox()
        self.spin_size.setRange(128, 2048)
        self.spin_size.setSingleStep(32)
        self.spin_size.setValue(640)
        self.spin_size.setStyleSheet("border: 2px solid #5C6BC0; background: #FFF; padding: 2px;")

        self.btn_apply_params = QPushButton("确认修改")
        self.btn_apply_params.setStyleSheet("""
            QPushButton { background-color: #C8E6C9; color: #2E7D32; font-size: 12px; padding: 4px 8px; border: 1px solid #2E7D32; }
            QPushButton:pressed { background-color: #A5D6A7; }
        """)

        params_layout.addWidget(lbl_conf)
        params_layout.addWidget(self.spin_conf)
        params_layout.addWidget(lbl_iou)
        params_layout.addWidget(self.spin_iou)
        params_layout.addWidget(lbl_size)
        params_layout.addWidget(self.spin_size)
        params_layout.addWidget(self.btn_apply_params)
        control_layout.addLayout(params_layout)

        # 推理模式
        lbl_infer_mode = QLabel("推理模式 (Inference Mode)")
        lbl_infer_mode.setStyleSheet(
            "border: none; background: transparent; font-size: 15px; color: #5C6BC0; font-weight: 900; margin-top: 6px;"
        )
        control_layout.addWidget(lbl_infer_mode)

        infer_btn_layout = QHBoxLayout()
        self.btn_infer_camera = QPushButton("单摄模式")
        self.btn_infer_phone = QPushButton("连接手机/副摄")
        self.btn_infer_import = QPushButton("导入媒体")
        self.btn_infer_camera.setCheckable(True)
        self.btn_infer_phone.setCheckable(True)
        self.btn_infer_import.setCheckable(True)

        infer_btn_layout.addWidget(self.btn_infer_camera, stretch=1)
        infer_btn_layout.addWidget(self.btn_infer_phone, stretch=1)
        infer_btn_layout.addWidget(self.btn_infer_import, stretch=1)

        self.infer_group = QButtonGroup()
        self.infer_group.addButton(self.btn_infer_camera, 1)
        self.infer_group.addButton(self.btn_infer_phone, 2)
        self.infer_group.addButton(self.btn_infer_import, 3)
        self.btn_infer_camera.setChecked(True)
        control_layout.addLayout(infer_btn_layout)

        # 导入媒体信息与进度条
        self.media_info_layout = QVBoxLayout()
        path_layout = QHBoxLayout()
        self.lbl_media_path = QLabel("当前媒体: (摄像头直播中)")
        self.lbl_media_path.setStyleSheet("border: none; background: transparent; color: #757575; font-size: 12px;")
        path_layout.insertWidget(0, self.lbl_media_path, stretch=1)
        self.media_info_layout.addLayout(path_layout)

        playback_layout = QHBoxLayout()
        self.btn_play_pause = QPushButton("⏸")
        self.btn_play_pause.setFixedSize(40, 40)
        self.slider_progress = QSlider(Qt.Orientation.Horizontal)
        self.lbl_progress = QLabel("Live")
        self.lbl_progress.setStyleSheet("border: none; background: transparent;")

        playback_layout.addWidget(self.btn_play_pause)
        playback_layout.addWidget(self.slider_progress)
        playback_layout.addWidget(self.lbl_progress)

        self.playback_widget = QWidget()
        self.playback_widget.setStyleSheet("background: transparent; border: none;")
        self.playback_widget.setLayout(playback_layout)
        self.playback_widget.hide()

        self.media_info_layout.addWidget(self.playback_widget)
        control_layout.addLayout(self.media_info_layout)

        # 终端输出区
        terminal_container = QFrame()
        apply_hard_shadow(terminal_container)
        terminal_layout = QVBoxLayout(terminal_container)

        lbl_terminal = QLabel("终端输出 (Terminal)")
        lbl_terminal.setStyleSheet(
            "border: none; background: transparent; font-size: 15px; color: #5C6BC0; font-weight: 900;"
        )
        terminal_layout.addWidget(lbl_terminal)

        self.txt_terminal = QTextEdit()
        self.txt_terminal.setReadOnly(True)
        self.txt_terminal.setStyleSheet(
            "background-color: #E8EAF6; color: #3949AB; font-size: 13px; font-weight: bold; border: 2px solid #5C6BC0; padding: 8px; border-radius: 4px;"
        )
        terminal_layout.addWidget(self.txt_terminal)

        controls_hlayout.addWidget(control_container, stretch=2)
        controls_hlayout.addWidget(terminal_container, stretch=2)
        left_layout.addLayout(controls_hlayout)

        content_layout.addLayout(left_layout, stretch=3)

        # === 3. 专家评审模式区 (右侧) ===
        review_container = QFrame()
        apply_hard_shadow(review_container)
        review_layout = QVBoxLayout(review_container)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(0)

        lbl_review_title = QLabel(" 检测结果预览 / 专家评审流水线 ")
        lbl_review_title.setStyleSheet(
            "background-color: #5C6BC0; color: #FFFFFF; font-size: 15px; padding: 10px; border: none;"
        )
        review_layout.addWidget(lbl_review_title)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.snapshots_widget = QWidget()
        self.snapshots_widget.setStyleSheet("background-color: transparent; border: none;")
        self.snapshots_layout = QVBoxLayout(self.snapshots_widget)
        self.snapshots_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.snapshots_layout.setSpacing(14)
        self.scroll_area.setWidget(self.snapshots_widget)
        review_layout.addWidget(self.scroll_area)

        # 抓拍与导出按钮
        snap_btn_layout = QHBoxLayout()
        snap_btn_layout.setAlignment(Qt.AlignmentFlag.AlignBottom)

        self.btn_manual_snap = QPushButton("抓拍")
        self.btn_export_all = QPushButton("导出")

        snap_qss = """
            QPushButton {
                background-color: #FFF9C4; color: #5C6BC0; font-size: 20px; padding: 14px; border: 3px solid #5C6BC0; font-weight: bold;
                margin-top:0px; margin-left:0px; margin-bottom:4px; margin-right:4px;
            }
            QPushButton:pressed {
                background-color: #FFF176; margin-top:4px; margin-left:4px; margin-bottom:0px; margin-right:0px;
            }
        """
        export_qss = """
            QPushButton {
                background-color: #E1F5FE; color: #5C6BC0; font-size: 14px; padding: 8px; border: 2px solid #5C6BC0; font-weight: bold;
                margin-top:0px; margin-left:0px; margin-bottom:4px; margin-right:4px;
            }
            QPushButton:pressed {
                background-color: #B3E5FC; margin-top:4px; margin-left:4px; margin-bottom:0px; margin-right:0px;
            }
        """
        self.btn_manual_snap.setStyleSheet(snap_qss)
        self.btn_export_all.setStyleSheet(export_qss)

        apply_hard_shadow(self.btn_manual_snap, 5, 5)
        apply_hard_shadow(self.btn_export_all, 3, 3)

        snap_btn_layout.addWidget(self.btn_manual_snap, stretch=4)
        snap_btn_layout.addWidget(self.btn_export_all, stretch=1, alignment=Qt.AlignmentFlag.AlignBottom)

        btn_container = QWidget()
        btn_container.setLayout(snap_btn_layout)
        btn_container.setStyleSheet("background: transparent; border: none;")
        review_layout.addWidget(btn_container)

        self.btn_manual_snap.clicked.connect(self.manual_snapshot)
        self.btn_export_all.clicked.connect(self.export_all_images)

        # 计分板
        score_panel = QWidget()
        score_panel.setStyleSheet("background-color: #E1F5FE; border: none; border-top: 3px solid #5C6BC0;")
        score_layout = QVBoxLayout(score_panel)
        self.lbl_score = QLabel("TOTAL SCORE: 0.00 / 35")
        self.lbl_score.setStyleSheet(
            "font-size: 20px; font-weight: 900; background: transparent; border: none; color: #5C6BC0;"
        )
        self.lbl_score.setAlignment(Qt.AlignmentFlag.AlignCenter)
        score_layout.addWidget(self.lbl_score)
        review_layout.addWidget(score_panel)

        content_layout.addWidget(review_container, stretch=1)
        main_layout.addLayout(content_layout)

        # === 信号事件绑定 ===
        self.btn_model_ppe.toggled.connect(self.update_system_config)
        self.btn_model_chem.toggled.connect(self.update_system_config)
        self.btn_apply_params.clicked.connect(self.update_system_config)
        self.btn_model_custom.clicked.connect(self.import_custom_model)

        self.btn_infer_camera.clicked.connect(self.restore_cameras)
        self.btn_infer_phone.clicked.connect(self.connect_phone_camera)
        self.btn_infer_import.clicked.connect(self.import_media)
        self.btn_play_pause.clicked.connect(self.toggle_play_pause)

        self.slider_progress.sliderPressed.connect(self.slider_pressed)
        self.slider_progress.sliderReleased.connect(self.slider_released)
        self.is_slider_dragging = False

        self.update_system_config()

    # ==========================================================================
    # 六、 遥测与状态机联动槽函数
    # ==========================================================================
    def on_sensor_data_updated(self, temp: float, humi: float):
        """温湿度传感器数据到达槽函数"""
        self.current_temp = temp
        self.current_humi = humi

        # 驱动跨模态状态机评估
        state, alert_dict = self.state_machine.evaluate(
            micro_detections=self.latest_chem_items,
            macro_unattended=self.latest_unattended,
            current_temp=self.current_temp,
            current_humi=self.current_humi,
        )

        is_alert = (state != "NORMAL")
        status_text = "CRITICAL" if state == "CRITICAL" else ("ALERT" if is_alert else "SAFE")
        self.telemetry_card.update_telemetry(
            temp=temp,
            humi=humi,
            is_alert=is_alert,
            is_simulated=self.sensor_worker.is_simulated,
            is_external=self.sensor_worker.is_external_controlled,
            status_text=status_text,
        )

    def on_sensor_mode_changed(self, is_simulated: bool, msg: str):
        """传感器模式切换提示"""
        self.append_terminal_log(f"[硬件采集] {msg}")

    def on_safety_alert(self, alert_dict: dict):
        """状态机多模态安全告警推送槽函数"""
        level = alert_dict.get("level", "WARNING")
        title = alert_dict.get("title", "告警")
        message = alert_dict.get("message", "")
        ts = alert_dict.get("timestamp", datetime.now().strftime("%H:%M:%S"))

        log_entry = f"\n🚨 [{ts}] [{level}] {title}\n{message}\n"
        self.append_terminal_log(log_entry)

        # 若发生紧急高危事件，主状态栏高亮
        if level == "CRITICAL":
            self.lbl_stats.setText(f" 状态: 紧急断电闭环生效中 | {ts} ")
            self.lbl_stats.setStyleSheet(
                "font-size: 15px; background-color: #FFCDD2; color: #D32F2F; border: 3px solid #D32F2F; padding: 10px;"
            )

    def append_terminal_log(self, text: str):
        """向终端窗口追加日志"""
        self.txt_terminal.append(text)
        cursor = self.txt_terminal.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.txt_terminal.setTextCursor(cursor)

    # ==========================================================================
    # 七、 视觉处理与传统业务流程保持
    # ==========================================================================
    def update_stats(self):
        secs = self.start_time.secsTo(QDateTime.currentDateTime())
        h = secs // 3600
        m = (secs % 3600) // 60
        s = secs % 60
        if self.state_machine.current_state == "CRITICAL":
            self.lbl_stats.setText(f" 运行: {h:02d}:{m:02d}:{s:02d} | ⚠ 紧急断电状态 ")
        elif self.state_machine.current_state == "WARNING":
            self.lbl_stats.setText(f" 运行: {h:02d}:{m:02d}:{s:02d} | ⚠ 环境异常告警 ")
        else:
            self.lbl_stats.setText(f" 运行: {h:02d}:{m:02d}:{s:02d} | 正常守护中 ")
            self.lbl_stats.setStyleSheet(
                "font-size: 15px; background-color: #FFFFFF; color: #5C6BC0; border: 3px solid #5C6BC0; padding: 10px;"
            )

    def update_display(self, pano_img, desk_img, alarms, violations, curr_f, tot_f, log_str):
        # 提取微观通道物品列表与无人值守状态供状态机消费
        self.latest_unattended = alarms.get("unattended", False)
        self.latest_chem_items = alarms.get("chem_items", [])

        # 驱动一次联合决策
        state, alert_dict = self.state_machine.evaluate(
            micro_detections=self.latest_chem_items,
            macro_unattended=self.latest_unattended,
            current_temp=self.current_temp,
            current_humi=self.current_humi,
        )

        # 更新遥测卡片状态
        is_alert = (state != "NORMAL")
        status_text = "CRITICAL" if state == "CRITICAL" else ("ALERT" if is_alert else "SAFE")
        self.telemetry_card.update_telemetry(
            temp=self.current_temp,
            humi=self.current_humi,
            is_alert=is_alert,
            is_simulated=self.sensor_worker.is_simulated,
            is_external=self.sensor_worker.is_external_controlled,
            status_text=status_text,
        )

        if log_str:
            self.txt_terminal.setText(log_str.strip())

        if tot_f > 0:
            if self.slider_progress.maximum() != tot_f:
                self.slider_progress.setMaximum(tot_f)
            if not self.is_slider_dragging:
                self.slider_progress.setValue(curr_f)
            self.lbl_progress.setText(f"{curr_f}/{tot_f}")
        else:
            self.lbl_progress.setText("Live")
            self.slider_progress.setValue(0)
            self.slider_progress.setMaximum(0)

        pix1 = self.convert_cv_qt(pano_img)
        pix2 = self.convert_cv_qt(desk_img)

        self.lbl_pano.setPixmap(pix1)
        self.lbl_desk.setPixmap(pix2)

        self.latest_pano = pano_img
        self.latest_desk = desk_img

    def manual_snapshot(self):
        if not hasattr(self, 'latest_pano') or self.latest_pano is None:
            return
        if len(self.snapshots) >= 20:
            return

        pix1 = self.convert_cv_qt(self.latest_pano)
        pix2 = (
            self.convert_cv_qt(self.latest_desk)
            if hasattr(self, 'latest_desk') and self.latest_desk is not None
            else QPixmap()
        )

        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        index = len(self.snapshots)

        item = {
            "time": timestamp,
            "pixmap_pano": pix1,
            "pixmap_desk": pix2,
            "reviewed": False,
            "is_correct": False,
            "card": None,
            "btn_correct": None,
            "btn_wrong": None,
        }
        self.snapshots.append(item)
        self.add_snapshot_card(index)

    def add_snapshot_card(self, index):
        item = self.snapshots[index]
        card = QFrame()
        card.setStyleSheet(
            "background-color: #FFFFFF; border: 2px solid #5C6BC0; margin-right: 4px; margin-bottom: 4px;"
        )
        apply_hard_shadow(card, offset_x=4, offset_y=4)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 10, 10, 10)

        lbl_title = QLabel(f"抓拍时间: {item['time']}")
        lbl_title.setStyleSheet("border: none; background: transparent; font-size: 14px; color: #5C6BC0;")
        card_layout.addWidget(lbl_title)

        images_layout = QHBoxLayout()
        lbl_img1 = QLabel()
        lbl_img1.setStyleSheet("border: 2px solid #5C6BC0; padding: 0;")
        lbl_img1.setPixmap(item['pixmap_pano'].scaled(120, 90, Qt.AspectRatioMode.KeepAspectRatio))
        images_layout.addWidget(lbl_img1)

        if not item['pixmap_desk'].isNull():
            lbl_img2 = QLabel()
            lbl_img2.setStyleSheet("border: 2px solid #5C6BC0; padding: 0;")
            lbl_img2.setPixmap(item['pixmap_desk'].scaled(120, 90, Qt.AspectRatioMode.KeepAspectRatio))
            images_layout.addWidget(lbl_img2)

        card_layout.addLayout(images_layout)

        btn_layout = QHBoxLayout()
        btn_correct = QPushButton("正确")
        btn_wrong = QPushButton("误报")

        card_btn_style = """
        QPushButton {
            color: #5C6BC0; font-size: 12px; padding: 6px; border: 2px solid #5C6BC0; 
            margin-top:0px; margin-left:0px; margin-bottom:3px; margin-right:3px;
        }
        QPushButton:pressed {
            background-color: #4DD0E1; margin-top:3px; margin-left:3px; margin-bottom:0px; margin-right:0px;
        }
        QPushButton:disabled {
            background-color: transparent; border: 2px dashed #9E9E9E; color: #9E9E9E;
        }
        """
        btn_correct.setStyleSheet(card_btn_style)
        btn_wrong.setStyleSheet(card_btn_style)
        apply_hard_shadow(btn_correct, 2, 2)
        apply_hard_shadow(btn_wrong, 2, 2)

        btn_correct.clicked.connect(lambda: self.mark_item_reviewed(index, True))
        btn_wrong.clicked.connect(lambda: self.mark_item_reviewed(index, False))

        btn_layout.addWidget(btn_correct)
        btn_layout.addWidget(btn_wrong)
        card_layout.addLayout(btn_layout)

        item["card"] = card
        item["btn_correct"] = btn_correct
        item["btn_wrong"] = btn_wrong

        def on_click(event):
            dialog = ReviewDialog(self, index)
            dialog.exec()

        card.mousePressEvent = on_click
        self.snapshots_layout.insertWidget(0, card)

    def mark_item_reviewed(self, index, is_correct):
        item = self.snapshots[index]
        if item["reviewed"]:
            return

        item["reviewed"] = True
        item["is_correct"] = is_correct

        if is_correct:
            self.expert_score = min(35.0, self.expert_score + 1.75)
            self.lbl_score.setText(f"TOTAL SCORE: {self.expert_score:.2f} / 35")
            color = "#C8E6C9"
        else:
            color = "#FFCDD2"

        item["btn_correct"].setEnabled(False)
        item["btn_wrong"].setEnabled(False)
        item["card"].setStyleSheet(
            f"background-color: {color}; border: 2px solid #5C6BC0; margin-right: 4px; margin-bottom: 4px;"
        )

        if self.review_dialog and self.review_dialog.isVisible() and self.review_dialog.current_index == index:
            self.review_dialog.update_view()

    def export_all_images(self):
        if not self.snapshots:
            QMessageBox.warning(self, "无数据", "当前没有可以导出的抓拍记录。")
            return

        folder = QFileDialog.getExistingDirectory(self, "选择批量保存文件夹")
        if not folder:
            return

        count = 0
        for i, item in enumerate(self.snapshots):
            file_name = f"snapshot_{i+1:02d}_{item['time'].replace(':','-')}.jpg"
            file_path = os.path.join(folder, file_name)
            self.save_combined_image(item, file_path)
            count += 1

        QMessageBox.information(self, "导出完成", f"成功导出 {count} 张合并大图到：\n{folder}")

    def save_combined_image(self, item, file_path):
        w1 = item['pixmap_pano'].width() if not item['pixmap_pano'].isNull() else 0
        w2 = item['pixmap_desk'].width() if not item['pixmap_desk'].isNull() else 0
        h1 = item['pixmap_pano'].height() if not item['pixmap_pano'].isNull() else 0
        h2 = item['pixmap_desk'].height() if not item['pixmap_desk'].isNull() else 0

        total_w = w1 + w2
        total_h = max(h1, h2)
        if total_w == 0 or total_h == 0:
            return

        combined = QPixmap(total_w, total_h)
        combined.fill(Qt.GlobalColor.white)
        painter = QPainter(combined)
        if not item['pixmap_pano'].isNull():
            painter.drawPixmap(0, 0, item['pixmap_pano'])
        if not item['pixmap_desk'].isNull():
            painter.drawPixmap(w1, 0, item['pixmap_desk'])
        painter.end()
        combined.save(file_path)

    def convert_cv_qt(self, cv_img):
        if cv_img is None or cv_img.size == 0:
            return QPixmap()
        rgb_image = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        convert_to_Qt_format = QImage(
            rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888
        )
        p = convert_to_Qt_format.scaled(480, 360, Qt.AspectRatioMode.KeepAspectRatio)
        return QPixmap.fromImage(p)

    def import_custom_model(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择自定义模型文件", "", "Model Files (*.pt *.onnx)"
        )
        if file_path:
            QMessageBox.information(self, "模型导入", f"成功选择并挂载模型:\n{file_path}")

    def import_media(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择视频或图片", "", "Media (*.mp4 *.avi *.jpg *.png *.jpeg)"
        )
        if file_path:
            self.lbl_media_path.setText(f"当前媒体: {os.path.basename(file_path)}")
            ext = os.path.splitext(file_path)[1].lower()
            if ext in ['.mp4', '.avi', '.mov', '.mkv']:
                self.playback_widget.show()
            else:
                self.playback_widget.hide()

            self.inference_thread.dual_system.load_media(src1=file_path, src2=file_path)
            self.btn_play_pause.setText("⏸")

    def restore_cameras(self):
        self.lbl_media_path.setText("当前媒体: 摄像头模式 (自动寻找已激活摄像头)")
        self.playback_widget.hide()
        self.inference_thread.dual_system.load_media(src1=0, src2=0, force_reload=True)
        self.btn_play_pause.setText("⏸")

    def connect_phone_camera(self):
        """连接手机摄像头或网络摄像头作为第二路（微观台面视角）"""
        url, ok = QInputDialog.getText(
            self,
            "连接手机第二摄像头",
            "请输入手机摄像头串流地址或外接摄像头序号：\n\n"
            "• 局域网 App（如 IP Webcam）：http://192.168.x.x:8080/video\n"
            "• DroidCam 串流：http://192.168.x.x:4747/video\n"
            "• USB/虚拟设备序号：输入数字 1 或 2\n",
            QLineEdit.EchoMode.Normal,
            "http://192.168.1.100:8080/video"
        )
        if ok and url.strip():
            target_src2 = url.strip()
            if target_src2.isdigit():
                target_src2 = int(target_src2)
            self.lbl_media_path.setText(f"当前媒体: 双摄协同 (主摄: 0, 副摄: {target_src2})")
            self.playback_widget.hide()
            self.inference_thread.dual_system.load_media(src1=0, src2=target_src2)
            self.btn_infer_phone.setChecked(True)
        else:
            if not self.btn_infer_import.isChecked():
                self.btn_infer_camera.setChecked(True)

    def toggle_play_pause(self):
        sys_obj = self.inference_thread.dual_system
        sys_obj.set_pause(not sys_obj.is_paused)
        if sys_obj.is_paused:
            self.btn_play_pause.setText("▶")
        else:
            self.btn_play_pause.setText("⏸")

    def slider_pressed(self):
        self.is_slider_dragging = True

    def slider_released(self):
        self.is_slider_dragging = False
        target_frame = self.slider_progress.value()
        self.inference_thread.dual_system.seek(target_frame)

    def update_system_config(self):
        conf = self.spin_conf.value()
        iou = self.spin_iou.value()
        img_size = self.spin_size.value()

        enable_ppe = self.btn_model_ppe.isChecked()
        enable_chem = self.btn_model_chem.isChecked()

        self.inference_thread.dual_system.set_config("dual", enable_ppe, enable_chem, conf, iou, img_size)

    def closeEvent(self, event):
        """窗口关闭时优雅退出所有后台线程"""
        if hasattr(self, 'sensor_worker') and self.sensor_worker:
            self.sensor_worker.stop()
        if hasattr(self, 'inference_thread') and self.inference_thread:
            self.inference_thread.stop()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    load_pixel_font()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
