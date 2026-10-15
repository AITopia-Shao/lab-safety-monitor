"""
水色像素粗野风 · 主监控窗口
src/ui/main_window.py

设计语言（沿用原版）：马卡龙水蓝底色 + 柔和靛蓝描边、Zpix 像素字体、无模糊实心硬阴影、
按下会"塌陷"的机械按键、底部流动海浪。
本版在此基础上：
  · 统一色板：遥测卡片从亮黄黑边改为与整体一致的靛蓝体系，状态色（安全/警告/紧急）全局统一
  · 信息补全：热源温度、烟雾、下位机链路、继电器与断电锁定状态上屏；紧急状态顶部横幅
  · 事件卡片：视觉违规 / 安全告警自动生成卡片（不再只能手动抓拍），评审结果写回事件库
  · 香橙派性能：大面板的硬阴影改为自绘（不再使用离屏渲染的 QGraphicsEffect）；
    海浪只重绘底部波浪带并可配置帧率；样式只在状态变化时更新
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
import time
import html
import platform
from collections import deque
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
        QDoubleSpinBox, QSpinBox, QLineEdit, QTextEdit, QInputDialog, QSizePolicy
    )
    from PySide6.QtGui import (
        QImage, QPixmap, QFont, QColor, QFontDatabase, QPainter, QPainterPath, QPen, QCursor
    )
except ImportError:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QTimer, QDateTime, QRect
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QScrollArea, QFrame, QGridLayout, QButtonGroup,
        QFileDialog, QSlider, QGraphicsDropShadowEffect, QDialog, QMessageBox,
        QDoubleSpinBox, QSpinBox, QLineEdit, QTextEdit, QInputDialog, QSizePolicy
    )
    from PyQt6.QtGui import (
        QImage, QPixmap, QFont, QColor, QFontDatabase, QPainter, QPainterPath, QPen, QCursor
    )

from src.config import cfg
from src.core.events import events
from src.core.telemetry import telemetry
from src.hardware.sensor_worker import SensorWorker
from src.core.state_machine import SafetyStateMachine
from src.core.engine import SafetyEngine, InferenceThread  # InferenceThread 保留导出，兼容旧代码
from dual_inference import DualInferenceSystem
from serial_comm import SerialComm

# 全局字体配置
PIXEL_FONT_FAMILY = "monospace"
ICON_FONT_FAMILY = "monospace"


# ==============================================================================
# 零、 设计令牌（色板）
# ==============================================================================
class T:
    BG = "#E1F5FE"          # 马卡龙水蓝底
    PAPER = "#FFFFFF"
    INK = "#3949AB"         # 深靛蓝：标题与正文
    PRIMARY = "#5C6BC0"     # 柔和靛蓝：描边、阴影、标题栏
    PRIMARY_SOFT = "#E8EAF6"
    PRIMARY_MID = "#C5CAE9"
    CYAN = "#4DD0E1"        # 按下反馈
    SKY = "#B3E5FC"
    DIM = "#5A64A8"         # 次要文字（各底色上对比度 ≥ 4.5）
    SAFE = "#047857"        # 白字 5.5:1
    SAFE_SOFT = "#DDF7EC"
    WARN = "#F59E0B"        # 琥珀只做填充，上面配深色字 WARN_INK（白字只有 2.2:1）
    WARN_INK = "#3B2300"    # 琥珀底上的文字 6.9:1
    WARN_TEXT = "#92400E"   # 浅底上的警告文字 ≥ 5.9:1
    WARN_SOFT = "#FFF4D6"
    DANGER = "#C62839"      # 白字 5.6:1
    DANGER_DEEP = "#8E1B2A"
    DANGER_SOFT = "#FFE3E7"
    SCREEN = "#1A1F3D"      # 视频底色（深靛而非纯黑，更柔和）


# 状态 → (主色, 浅底色, 英文徽标, 中文)
STATE_STYLE = {
    "NORMAL": (T.SAFE, T.SAFE_SOFT, "SAFE", "正常守护中"),
    "WARNING": (T.WARN, T.WARN_SOFT, "WARNING", "环境/行为异常"),
    "CRITICAL": (T.DANGER, T.DANGER_SOFT, "CRITICAL", "紧急处置中"),
}
# 状态必须"形状图标 + 文字"双重编码：红绿色弱用户无法仅凭颜色区分安全与紧急
STATE_ICON = {"NORMAL": "√", "WARNING": "▲", "CRITICAL": "■"}


def ink_on(bg):
    """填充色上应使用的文字颜色：琥珀底用深棕，其余用白"""
    return T.WARN_INK if bg == T.WARN else T.PAPER
# 事件等级 → (主色, 浅底色, 中文)
LEVEL_STYLE = {
    "INFO": (T.DIM, T.PRIMARY_SOFT, "信息"),
    "NOTICE": (T.PRIMARY, T.PRIMARY_SOFT, "提醒"),
    "WARNING": (T.WARN, T.WARN_SOFT, "警告"),
    "CRITICAL": (T.DANGER, T.DANGER_SOFT, "紧急"),
}
# 不生成卡片的事件（链路变化、锁定状态等，已在横幅 / 遥测卡片中展示）
CARD_SKIP_KINDS = {"power_latched", "link", "power_reset", "power_off_ack", "mcu_event",
                   "vision_offline", "unattended", "model", "system"}


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
    QMainWindow, QDialog {{
        background-color: {T.BG};
    }}
    QLabel {{
        color: {T.INK};
        background: transparent;
        border: none;
    }}
    QPushButton {{
        background-color: {T.PAPER};
        color: {T.PRIMARY};
        border: 2px solid {T.PRIMARY};
        border-radius: 0px;
        padding: 7px 12px;
        margin-top: 0px; margin-left: 0px; margin-bottom: 4px; margin-right: 4px;
    }}
    QPushButton:hover {{
        background-color: {T.BG};
    }}
    QPushButton:checked {{
        background-color: {T.PRIMARY};
        color: {T.PAPER};
        margin-top: 2px; margin-left: 2px; margin-bottom: 2px; margin-right: 2px;
    }}
    QPushButton:pressed {{
        background-color: {T.CYAN};
        color: {T.PAPER};
        margin-top: 4px; margin-left: 4px; margin-bottom: 0px; margin-right: 0px;
    }}
    QPushButton:disabled {{
        background-color: transparent; color: #9FA8DA; border: 2px dashed #9FA8DA;
    }}
    QAbstractSpinBox {{
        background: {T.PAPER}; color: {T.INK};
        border: 2px solid {T.PRIMARY}; border-radius: 0px; padding: 2px 4px;
        selection-background-color: {T.PRIMARY_MID};
    }}
    QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
        width: 14px; background: {T.PRIMARY_SOFT}; border-left: 2px solid {T.PRIMARY};
    }}
    QSlider::groove:horizontal {{
        height: 8px; background: {T.PRIMARY_SOFT}; border: 2px solid {T.PRIMARY};
    }}
    QSlider::sub-page:horizontal {{
        background: {T.PRIMARY};
    }}
    QSlider::handle:horizontal {{
        width: 12px; margin: -6px 0; background: {T.PAPER}; border: 2px solid {T.PRIMARY};
    }}
    QScrollArea {{
        background-color: transparent;
        border: none;
    }}
    QScrollBar:vertical {{
        background-color: {T.PAPER};
        width: 14px;
        border: 2px solid {T.PRIMARY};
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background-color: {T.PRIMARY};
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}
    QToolTip {{
        background: {T.PAPER}; color: {T.INK}; border: 2px solid {T.PRIMARY};
    }}
    """


def apply_hard_shadow(widget, offset_x=4, offset_y=4, color=T.PRIMARY):
    """纯粹实心的无模糊硬阴影（用于按钮等小控件；大面板请使用自绘的 Card，避免离屏渲染开销）"""
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(0)
    shadow.setColor(QColor(color))
    shadow.setOffset(offset_x, offset_y)
    widget.setGraphicsEffect(shadow)


def _wave_fps() -> int:
    val = cfg("ui.wave_fps", None)
    if val is not None:
        return max(0, int(val))
    arm = platform.machine().lower() in ("aarch64", "arm64", "armv7l")
    return 10 if arm else 25


# ==============================================================================
# 一、 基础组件：自绘硬阴影卡片 / 状态胶囊
# ==============================================================================
class Card(QWidget):
    """
    粗野主义卡片：实色面板 + 描边 + 右下实心硬阴影 + 可选左侧色条。
    阴影在 paintEvent 中直接绘制，不使用 QGraphicsEffect（香橙派上每次重绘都离屏渲染，代价很高）。
    """

    def __init__(self, parent=None, bg=T.PAPER, border=T.PRIMARY, shadow=T.PRIMARY,
                 offset=5, border_w=2, accent=None, padding=(12, 10, 12, 10)):
        super().__init__(parent)
        self._bg = QColor(bg)
        self._border = QColor(border)
        self._shadow = QColor(shadow)
        self._offset = offset
        self._bw = border_w
        self._accent = QColor(accent) if accent else None
        self._padding = padding
        self.body = QVBoxLayout(self)
        self.body.setSpacing(8)
        self._apply_margins()

    def _apply_margins(self):
        l, t, r, b = self._padding
        extra_l = 6 if self._accent is not None else 0
        self.body.setContentsMargins(l + self._bw + extra_l, t + self._bw,
                                     r + self._bw + self._offset, b + self._bw + self._offset)

    def set_colors(self, bg=None, border=None, shadow=None, accent=None):
        if bg:
            self._bg = QColor(bg)
        if border:
            self._border = QColor(border)
        if shadow:
            self._shadow = QColor(shadow)
        if accent:
            self._accent = QColor(accent)
            self._apply_margins()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        o, bw = self._offset, self._bw
        face = self.rect().adjusted(0, 0, -o, -o)
        if o > 0:
            p.fillRect(face.translated(o, o), self._shadow)
        p.fillRect(face, self._border)
        inner = face.adjusted(bw, bw, -bw, -bw)
        p.fillRect(inner, self._bg)
        if self._accent is not None:
            p.fillRect(QRect(inner.left(), inner.top(), 6, inner.height()), self._accent)
        p.end()


class Chip(QLabel):
    """状态胶囊：只在内容或配色变化时更新样式（setStyleSheet 每次都会触发样式重算）"""

    def __init__(self, text="", fg=T.PAPER, bg=T.PRIMARY, border=None, size=12, parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._size = size
        self._key = None
        self.set(text, fg, bg, border)

    def set(self, text, fg, bg, border=None):
        key = (text, fg, bg, border)
        if key == self._key:
            return
        self._key = key
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{ color: {fg}; background: {bg}; border: 2px solid {border or bg};"
            f" font-size: {self._size}px; padding: 2px 8px; }}"
        )


def section_title(text, size=15):
    lbl = QLabel(f"▌{text}")
    lbl.setStyleSheet(f"font-size: {size}px; color: {T.PRIMARY}; font-weight: 900;")
    return lbl


# ==============================================================================
# 二、 环境遥测面板（TelemetryCard）
# ==============================================================================
class MetricTile(QWidget):
    """单个遥测数值块：标题 / 大号数值 / 阈值提示，边框颜色随状态变化"""

    def __init__(self, caption, hint="", parent=None):
        super().__init__(parent)
        self._key = None
        self.setObjectName("MetricTile")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(0)
        self.lbl_caption = QLabel(caption)
        self.lbl_caption.setStyleSheet(f"font-size: 11px; color: {T.DIM};")
        self.lbl_value = QLabel("--")
        self.lbl_value.setStyleSheet(f"font-size: 22px; font-weight: 900; color: {T.INK};")
        self.lbl_hint = QLabel(hint)
        self.lbl_hint.setStyleSheet(f"font-size: 10px; color: {T.DIM};")
        lay.addWidget(self.lbl_caption)
        lay.addWidget(self.lbl_value)
        lay.addWidget(self.lbl_hint)
        self.set_value("--", "idle")

    def set_value(self, text, level="ok", hint=None):
        self.lbl_value.setText(text)
        if hint is not None:
            self.lbl_hint.setText(hint)
        if level == self._key:
            return
        self._key = level
        # 数值文字始终用文字色（INK），状态由边框 / 底色 / 提示行里的图标承担
        border, bg, fg, bw = {
            "ok": (T.PRIMARY, T.PAPER, T.INK, 2),
            "idle": (T.PRIMARY_MID, T.PAPER, T.DIM, 2),
            "warn": (T.WARN, T.WARN_SOFT, T.INK, 3),
            "danger": (T.DANGER, T.DANGER_SOFT, T.INK, 3),
        }[level]
        self.setStyleSheet(f"QWidget#MetricTile {{ background: {bg}; border: {bw}px solid {border}; }}")
        self.lbl_value.setStyleSheet(f"font-size: 22px; font-weight: 900; color: {fg};")


class TelemetryCard(Card):
    """
    环境遥测面板：环境温度 / 相对湿度 / 热源表面温度 / 烟雾 四个数值块，
    以及数据来源、综合安全状态、下位机链路、继电器与断电锁定状态。
    """

    def __init__(self, parent=None):
        super().__init__(parent, padding=(12, 8, 12, 8))
        self.init_ui()

    def init_ui(self):
        layout = self.body
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.lbl_title = QLabel("⚡ 多源环境遥测 / TELEMETRY")
        self.lbl_title.setStyleSheet(f"font-size: 15px; font-weight: 900; color: {T.INK};")
        self.lbl_mode_badge = Chip("INIT", T.PAPER, T.DIM, size=11)
        self.lbl_link_badge = Chip("下位机 --", T.DIM, T.PAPER, T.PRIMARY_MID, size=11)
        self.lbl_relay_badge = Chip("继电器 --", T.DIM, T.PAPER, T.PRIMARY_MID, size=11)
        self.lbl_status_badge = Chip("√ SAFE", T.PAPER, T.SAFE, size=13)
        header.addWidget(self.lbl_title)
        header.addStretch()
        header.addWidget(self.lbl_mode_badge)
        header.addWidget(self.lbl_link_badge)
        header.addWidget(self.lbl_relay_badge)
        header.addWidget(self.lbl_status_badge)
        layout.addLayout(header)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tile_temp = MetricTile("SENSOR A // 环境温度")
        self.tile_humi = MetricTile("SENSOR B // 相对湿度")
        self.tile_obj = MetricTile("SENSOR C // 热源表面温度")
        self.tile_smoke = MetricTile("SENSOR D // 烟雾浓度")
        for t in (self.tile_temp, self.tile_humi, self.tile_obj, self.tile_smoke):
            tiles.addWidget(t, stretch=1)
        layout.addLayout(tiles)

        self.lbl_footer = QLabel("")
        self.lbl_footer.setStyleSheet(f"font-size: 11px; color: {T.INK};")
        layout.addWidget(self.lbl_footer)
        self._footer_key = None

        # 兼容旧代码中直接访问的属性名
        self.lbl_temp = self.tile_temp.lbl_value
        self.lbl_humi = self.tile_humi.lbl_value

    def update_telemetry(
        self,
        temp: float,
        humi: float,
        is_alert: bool = False,
        is_simulated: bool = False,
        is_external: bool = False,
        status_text: Optional[str] = None,
        obj_temp: Optional[float] = None,
        smoke: Optional[int] = None,
        link_online: Optional[bool] = None,
        relay_on: Optional[bool] = None,
        latched: Optional[bool] = None,
        footer: Optional[str] = None,
        state: Optional[str] = None,
    ):
        """更新遥测数值与徽标。综合状态以状态机为准（state / status_text），不再自行判断阈值。"""
        amb_high = float(cfg("rules.ambient_high_c", 40.0))
        humi_high = float(cfg("rules.humidity_high", 80.0))
        heat_on = float(cfg("rules.heating_on_c", 50.0))
        dry = float(cfg("rules.dry_burn_c", 120.0))
        smoke_th = int(cfg("rules.smoke_raw", 1800))

        t_hot = temp > amb_high
        h_hi = humi > humi_high
        self.tile_temp.set_value(f"{temp:.1f} ℃", "warn" if t_hot else "ok",
                                 f"{'▲ 偏高' if t_hot else '√ 正常'} · 告警 > {amb_high:.0f}℃")
        self.tile_humi.set_value(f"{humi:.1f} %", "warn" if h_hi else "ok",
                                 f"{'▲ 过高' if h_hi else '√ 正常'} · 告警 > {humi_high:.0f}%")
        if obj_temp is None:
            self.tile_obj.set_value("未接入", "idle", "MLX90614 红外测温")
        else:
            lvl = "danger" if obj_temp >= dry else ("warn" if obj_temp >= heat_on else "ok")
            tag = "■ 干烧" if obj_temp >= dry else ("▲ 加热中" if obj_temp >= heat_on else "√ 常温")
            self.tile_obj.set_value(f"{obj_temp:.1f} ℃", lvl, f"{tag} · 干烧 ≥ {dry:.0f}℃")
        if smoke is None:
            self.tile_smoke.set_value("未接入", "idle", "MQ-2 未接入或预热中")
        else:
            hi = smoke >= smoke_th
            self.tile_smoke.set_value(f"{smoke}", "danger" if hi else "ok",
                                      f"{'■ 超标' if hi else '√ 正常'} · 阈值 {smoke_th}")

        # 数据来源
        if not is_simulated:
            self.lbl_mode_badge.set("硬件实测", T.PAPER, T.INK)
        elif is_external:
            self.lbl_mode_badge.set("模拟器注入", T.PAPER, T.PRIMARY)
        else:
            self.lbl_mode_badge.set("自主模拟", T.PAPER, T.DIM)

        # 下位机与继电器
        if link_online:
            self.lbl_link_badge.set("● 下位机在线", T.SAFE, T.PAPER, T.SAFE)
        elif link_online is False:
            self.lbl_link_badge.set("○ 下位机离线", T.DIM, T.PAPER, T.PRIMARY_MID)
        if latched:
            self.lbl_relay_badge.set("■ 断电锁定", T.PAPER, T.DANGER)
        elif relay_on is True:
            self.lbl_relay_badge.set("继电器 闭合", T.INK, T.PAPER, T.PRIMARY)
        elif relay_on is False:
            self.lbl_relay_badge.set("继电器 断开", T.DIM, T.PAPER, T.PRIMARY_MID)
        else:
            self.lbl_relay_badge.set("继电器 --", T.DIM, T.PAPER, T.PRIMARY_MID)

        # 综合状态
        if state is None:
            state = "CRITICAL" if status_text == "CRITICAL" else ("WARNING" if is_alert else "NORMAL")
        color, soft, badge, _ = STATE_STYLE.get(state, STATE_STYLE["NORMAL"])
        self.lbl_status_badge.set(f"{STATE_ICON.get(state, '')} {badge}", ink_on(color), color)
        self.set_colors(border=color if state != "NORMAL" else T.PRIMARY,
                        shadow=color if state != "NORMAL" else T.PRIMARY)

        if footer is None:
            footer = (f"联动阈值：热源 ≥ {dry:.0f}℃ 断电 · 加热无人 {float(cfg('rules.unattended_heating_cut_s', 60)):.0f}s 断电"
                      f" · 烟雾 ≥ {smoke_th} 断电 · 湿度 > {humi_high:.0f}% 告警")
        if footer != self._footer_key:
            self._footer_key = footer
            self.lbl_footer.setText(footer)


# ==============================================================================
# 三、 底层动画水浪背景
# ==============================================================================
class WaveBackgroundWidget(QWidget):
    """
    自定义中心容器：水蓝底 + 像素点阵 + 双层流动的半透明海浪。
    性能：只重绘底部波浪带（而非整窗），帧率可在 config.yaml → ui.wave_fps 配置（0 = 静止）。
    """

    BAND_RATIO = 0.70

    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase = 0.0
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.animate_wave)
        fps = _wave_fps()
        if fps > 0:
            self.anim_timer.start(int(1000 / fps))
        # 原版每 30ms 前进 0.1；按实际帧间隔等比换算，保证不同帧率下波速一致
        self._step = 0.1 * (1000.0 / fps) / 30.0 if fps > 0 else 0.0

    def animate_wave(self):
        self.phase += self._step
        top = int(self.height() * self.BAND_RATIO)
        self.update(QRect(0, top, self.width(), self.height() - top))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = event.rect()
        painter.fillRect(clip, QColor(T.BG))

        spacing = 60
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(129, 212, 250, 120))
        x0 = clip.left() - clip.left() % spacing
        y0 = clip.top() - clip.top() % spacing
        for x in range(x0, clip.right() + spacing, spacing):
            for y in range(y0, clip.bottom() + spacing, spacing):
                painter.drawRect(x + 28, y + 28, 4, 4)   # 方形像素点，呼应像素风

        w = self.width()
        h = self.height()
        base_y = h * 0.82

        path1 = QPainterPath()
        path1.moveTo(0, h)
        for x in range(0, w + 12, 12):
            path1.lineTo(x, base_y + math.sin((x / 150.0) - (self.phase * 0.8)) * 26)
        path1.lineTo(w, h)
        path1.closeSubpath()
        painter.setBrush(QColor(129, 212, 250, 150))
        painter.drawPath(path1)

        path2 = QPainterPath()
        path2.moveTo(0, h)
        for x in range(0, w + 12, 12):
            path2.lineTo(x, base_y + 18 + math.sin((x / 100.0) + self.phase) * 18)
        path2.lineTo(w, h)
        path2.closeSubpath()
        painter.setBrush(QColor(92, 107, 192, 100))
        painter.drawPath(path2)
        painter.end()


# ==============================================================================
# 四、 推理与视频流线程
# ==============================================================================
# ==============================================================================
# 五、 专家评审模式弹窗 (ReviewDialog)
# ==============================================================================
class ReviewDialog(QDialog):
    def __init__(self, parent_gui, initial_index):
        super().__init__(parent_gui)
        self.parent_gui = parent_gui
        self.current_index = initial_index
        self.setWindowTitle("大图评审与导出")
        self.setModal(True)
        self.setStyleSheet(parent_gui.styleSheet())
        self.resize(1040, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        head = Card(padding=(12, 8, 12, 8))
        self.lbl_level = Chip("提醒", T.PAPER, T.PRIMARY, size=13)
        self.lbl_title = QLabel()
        self.lbl_title.setStyleSheet(f"font-size: 18px; color: {T.INK};")
        self.lbl_index = QLabel()
        self.lbl_index.setStyleSheet(f"font-size: 13px; color: {T.DIM};")
        row = QHBoxLayout()
        row.addWidget(self.lbl_level)
        row.addWidget(self.lbl_title, stretch=1)
        row.addWidget(self.lbl_index)
        head.body.addLayout(row)
        self.lbl_message = QLabel()
        self.lbl_message.setWordWrap(True)
        self.lbl_message.setStyleSheet(f"font-size: 13px; color: {T.DIM};")
        head.body.addWidget(self.lbl_message)
        layout.addWidget(head)

        images_layout = QHBoxLayout()
        images_layout.setSpacing(14)
        self.lbl1 = QLabel()
        self.lbl2 = QLabel()
        for lbl in (self.lbl1, self.lbl2):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setMinimumSize(480, 360)
            lbl.setStyleSheet(f"background: {T.SCREEN}; border: 3px solid {T.PRIMARY};")
            images_layout.addWidget(lbl)
        layout.addLayout(images_layout)

        btn_layout = QHBoxLayout()
        self.btn_prev = QPushButton("◀ 上一张")
        self.btn_next = QPushButton("下一张 ▶")
        self.btn_correct = QPushButton("√ 正确 (计分)")
        self.btn_wrong = QPushButton("✘ 误报 (不计分)")
        self.btn_export = QPushButton("导出单张")

        btn_layout.addWidget(self.btn_prev)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_correct)
        btn_layout.addWidget(self.btn_wrong)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_export)
        btn_layout.addWidget(self.btn_next)
        self.btn_correct.setStyleSheet(f"QPushButton {{ color: {T.SAFE}; border-color: {T.SAFE}; font-size: 15px; padding: 10px 18px; }}")
        self.btn_wrong.setStyleSheet(f"QPushButton {{ color: {T.DANGER}; border-color: {T.DANGER}; font-size: 15px; padding: 10px 18px; }}")
        for btn in [self.btn_prev, self.btn_next, self.btn_correct, self.btn_wrong, self.btn_export]:
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
        color, _, tag = LEVEL_STYLE.get(item.get("level", "NOTICE"), LEVEL_STYLE["NOTICE"])
        if item.get("kind") == "manual":
            tag = "抓拍"
        self.lbl_level.set(tag, ink_on(color), color)
        self.lbl_title.setText(f"{item.get('title', '抓拍')}  ·  {item['time']}")
        self.lbl_index.setText(f"{self.current_index + 1} / {len(self.parent_gui.snapshots)}")
        self.lbl_message.setText(item.get("message", ""))

        self.lbl1.setPixmap(item['pixmap_pano'].scaled(480, 360, Qt.AspectRatioMode.KeepAspectRatio))
        if not item['pixmap_desk'].isNull():
            self.lbl2.setPixmap(item['pixmap_desk'].scaled(480, 360, Qt.AspectRatioMode.KeepAspectRatio))
            self.lbl2.show()
        else:
            self.lbl2.hide()

        self.btn_correct.setEnabled(not item["reviewed"])
        self.btn_wrong.setEnabled(not item["reviewed"])
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
# 六、 系统主监控窗口 (MainWindow)
# ==============================================================================
class MainWindow(QMainWindow):
    """
    实验室安全监控系统主窗口
    集成：
    1. 双路实时监控推流与渲染
    2. 多源环境遥测面板（环境温湿度 / 热源温度 / 烟雾 / 下位机 / 继电器）
    3. 边缘状态机多模态安全判定与继电器硬件执行闭环（紧急横幅 + 断电锁定展示）
    4. 事件卡片自动生成 + 专家评审计分流水线（评审结果写回事件库）
    """

    MAX_CARDS = 50

    def __init__(self, engine: Optional[SafetyEngine] = None):
        super().__init__()
        self.setWindowTitle("实验室AI视觉守护系统 · 双摄协同 × 多源传感 × 边缘断电闭环")
        self.resize(1400, 940)

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
        self.latest_pano = None
        self.latest_desk = None

        self._feed_seq = 0
        self._last_log_html = ""
        self._ui_msgs = deque(maxlen=4)
        self._last_tele_refresh = 0.0
        self._banner_blink = False
        self._banner_key = None

        # 安全核心引擎：推理、遥测、状态机研判都在引擎里，窗口只负责显示与操作
        self._own_engine = engine is None
        self.engine = engine or SafetyEngine(self)
        self.inference_thread = self.engine.inference_thread
        self.state_machine = self.engine.state_machine
        self.sensor_worker = self.engine.sensor_worker
        self.engine.frames_ready.connect(self.update_display)
        self.engine.alert.connect(self.on_safety_alert)
        self.engine.telemetry_changed.connect(self.on_engine_telemetry)
        self.engine.sensor_mode_changed.connect(self.on_sensor_mode_changed)

        self.init_ui()

        # 事件卡片拉取（300ms）与紧急横幅闪烁（500ms）
        self.feed_timer = QTimer(self)
        self.feed_timer.timeout.connect(self.pull_event_feed)
        self.feed_timer.start(300)
        self.banner_timer = QTimer(self)
        self.banner_timer.timeout.connect(self.refresh_banner)
        self.banner_timer.start(500)

        # 启动引擎（由外部传入的引擎由调用方负责启动）
        if self._own_engine:
            self.engine.start()

    # ==========================================================================
    # 界面搭建
    # ==========================================================================
    def _video_panel(self, title):
        card = Card(padding=(0, 0, 0, 0), offset=6)
        card.body.setSpacing(0)
        bar = QWidget()
        bar.setStyleSheet(f"background: {T.PRIMARY};")
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(10, 5, 8, 5)
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color: {T.PAPER}; font-size: 15px;")
        chip = Chip("等待画面", T.PRIMARY, T.PAPER, size=11)
        bar_l.addWidget(lbl)
        bar_l.addStretch()
        bar_l.addWidget(chip)
        screen = QLabel()
        screen.setFixedSize(480, 360)
        screen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        screen.setStyleSheet(f"background-color: {T.SCREEN}; color: {T.DIM}; font-size: 14px;")
        screen.setText("NO SIGNAL")
        card.body.addWidget(bar)
        card.body.addWidget(screen)
        return card, screen, chip

    def init_ui(self):
        central_widget = WaveBackgroundWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(18, 16, 18, 16)
        main_layout.setSpacing(10)

        # === 1. 顶部 Header ===
        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)

        title_card = Card(padding=(14, 6, 14, 6), offset=5)
        lbl_title = QLabel("基于双摄协同的实验室安全事前干预监测系统")
        lbl_title.setStyleSheet(f"font-size: 24px; color: {T.PRIMARY};")
        lbl_sub = QLabel("宏观行为 × 微观台面 · 多源环境传感 · 边缘断电闭环")
        lbl_sub.setStyleSheet(f"font-size: 12px; color: {T.DIM};")
        title_card.body.setSpacing(4)
        lbl_title.setMinimumHeight(30)
        title_card.body.addWidget(lbl_title)
        title_card.body.addWidget(lbl_sub)

        status_card = Card(padding=(12, 6, 12, 6), offset=5)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.lbl_state_chip = Chip("√ SAFE", T.PAPER, T.SAFE, size=15)
        self.lbl_stats = QLabel(" 运行 00:00:00 | 正常守护中 ")
        self.lbl_stats.setStyleSheet(f"font-size: 15px; color: {T.INK};")
        status_row.addWidget(self.lbl_state_chip)
        status_row.addWidget(self.lbl_stats)
        status_card.body.addLayout(status_row)
        self.status_card = status_card

        header_layout.addWidget(title_card, stretch=1)
        header_layout.addWidget(status_card)
        main_layout.addLayout(header_layout)

        # === 紧急 / 警告横幅（平时隐藏） ===
        self.alert_banner = QLabel("")
        self.alert_banner.setWordWrap(True)
        self.alert_banner.hide()
        main_layout.addWidget(self.alert_banner)

        self.uptime_timer = QTimer(self)
        self.uptime_timer.timeout.connect(self.update_stats)
        self.uptime_timer.start(1000)

        # === 2. 内容区 ===
        content_layout = QHBoxLayout()
        content_layout.setSpacing(16)

        left_layout = QVBoxLayout()
        left_layout.setSpacing(12)

        video_layout = QHBoxLayout()
        video_layout.setSpacing(16)
        cam_a, self.lbl_pano, self.chip_pano = self._video_panel("宏观行为监控 · Camera A")
        cam_b, self.lbl_desk, self.chip_desk = self._video_panel("微观台面监控 · Camera B")
        video_layout.addWidget(cam_a)
        video_layout.addWidget(cam_b)
        left_layout.addLayout(video_layout)

        self.telemetry_card = TelemetryCard(self)
        left_layout.addWidget(self.telemetry_card)

        controls_hlayout = QHBoxLayout()
        controls_hlayout.setSpacing(16)

        # --- 控制面板 ---
        control_container = Card(padding=(12, 8, 12, 8))
        control_layout = control_container.body
        control_layout.setSpacing(6)
        control_layout.addWidget(section_title("模型设置 · Model Settings"))

        model_btn_layout = QHBoxLayout()
        self.btn_model_ppe = QPushButton("人员模型")
        self.btn_model_chem = QPushButton("物品模型")
        self.btn_model_custom = QPushButton("自定义...")
        self.btn_model_custom.setToolTip("加载自己训练的 .pt / .onnx / RKNN 模型，替换人员或物品模型")
        self.btn_model_custom.setStyleSheet(f"""
            QPushButton {{ background-color: {T.PRIMARY_SOFT}; color: {T.PRIMARY}; font-size: 12px; padding: 5px 8px; }}
        """)
        for btn in [self.btn_model_ppe, self.btn_model_chem]:
            btn.setCheckable(True)
            model_btn_layout.addWidget(btn, stretch=2)
        model_btn_layout.addWidget(self.btn_model_custom, stretch=1)
        self.btn_model_ppe.setChecked(True)
        self.btn_model_chem.setChecked(True)
        control_layout.addLayout(model_btn_layout)

        params_layout = QHBoxLayout()
        params_layout.setSpacing(6)
        self.spin_conf = QDoubleSpinBox()
        self.spin_conf.setRange(0.05, 0.95)
        self.spin_conf.setSingleStep(0.05)
        self.spin_conf.setValue(float(cfg("vision.conf", 0.35)))
        self.spin_iou = QDoubleSpinBox()
        self.spin_iou.setRange(0.05, 0.95)
        self.spin_iou.setSingleStep(0.05)
        self.spin_iou.setValue(float(cfg("vision.iou", 0.45)))
        self.spin_size = QSpinBox()
        self.spin_size.setRange(128, 2048)
        self.spin_size.setSingleStep(32)
        self.spin_size.setValue(int(cfg("vision.imgsz", 640)))
        self.btn_apply_params = QPushButton("确认修改")
        self.btn_apply_params.setStyleSheet(f"""
            QPushButton {{ background-color: {T.SAFE_SOFT}; color: {T.SAFE}; border-color: {T.SAFE}; font-size: 12px; padding: 4px 8px; }}
            QPushButton:pressed {{ background-color: #B9F0D8; }}
        """)
        for text, w in (("置信度", self.spin_conf), ("IoU", self.spin_iou), ("尺寸", self.spin_size)):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"font-size: 12px; color: {T.DIM};")
            params_layout.addWidget(lbl)
            params_layout.addWidget(w)
        params_layout.addWidget(self.btn_apply_params)
        control_layout.addLayout(params_layout)

        control_layout.addWidget(section_title("推理模式 · Inference Mode"))
        infer_btn_layout = QHBoxLayout()
        self.btn_infer_camera = QPushButton("单摄模式")
        self.btn_infer_phone = QPushButton("连接手机/副摄")
        self.btn_infer_import = QPushButton("导入媒体")
        for b in (self.btn_infer_camera, self.btn_infer_phone, self.btn_infer_import):
            b.setCheckable(True)
            infer_btn_layout.addWidget(b, stretch=1)
        self.infer_group = QButtonGroup(self)
        self.infer_group.addButton(self.btn_infer_camera, 1)
        self.infer_group.addButton(self.btn_infer_phone, 2)
        self.infer_group.addButton(self.btn_infer_import, 3)
        self.btn_infer_camera.setChecked(True)
        control_layout.addLayout(infer_btn_layout)

        self.media_info_layout = QVBoxLayout()
        self.media_info_layout.setSpacing(4)
        self.lbl_media_path = QLabel("当前媒体: (摄像头直播中)")
        self.lbl_media_path.setStyleSheet(f"color: {T.DIM}; font-size: 12px;")
        self.media_info_layout.addWidget(self.lbl_media_path)

        playback_layout = QHBoxLayout()
        playback_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_play_pause = QPushButton("⏸")
        self.btn_play_pause.setFixedSize(40, 38)
        self.slider_progress = QSlider(Qt.Orientation.Horizontal)
        self.lbl_progress = QLabel("Live")
        self.lbl_progress.setStyleSheet(f"font-size: 12px; color: {T.DIM};")
        playback_layout.addWidget(self.btn_play_pause)
        playback_layout.addWidget(self.slider_progress)
        playback_layout.addWidget(self.lbl_progress)
        self.playback_widget = QWidget()
        self.playback_widget.setLayout(playback_layout)
        self.playback_widget.hide()
        self.media_info_layout.addWidget(self.playback_widget)
        control_layout.addLayout(self.media_info_layout)
        control_layout.addStretch()

        # --- 终端输出 ---
        terminal_container = Card(padding=(12, 8, 12, 8))
        terminal_layout = terminal_container.body
        term_head = QHBoxLayout()
        term_head.addWidget(section_title("终端输出 · Terminal"))
        term_head.addStretch()
        self.lbl_fps = Chip("视觉 --", T.PRIMARY, T.PRIMARY_SOFT, T.PRIMARY_MID, size=11)
        term_head.addWidget(self.lbl_fps)
        terminal_layout.addLayout(term_head)
        self.txt_terminal = QTextEdit()
        self.txt_terminal.setReadOnly(True)
        self.txt_terminal.setStyleSheet(
            f"QTextEdit {{ background-color: #F3F5FF; color: {T.INK}; font-size: 12px; font-weight: bold;"
            f" border: 2px solid {T.PRIMARY}; padding: 6px; }}"
        )
        terminal_layout.addWidget(self.txt_terminal)

        controls_hlayout.addWidget(control_container, stretch=2)
        controls_hlayout.addWidget(terminal_container, stretch=2)
        left_layout.addLayout(controls_hlayout, stretch=1)
        content_layout.addLayout(left_layout, stretch=3)

        # === 3. 右侧：事件卡片与专家评审 ===
        review_container = Card(padding=(0, 0, 0, 0), offset=6)
        review_layout = review_container.body
        review_layout.setSpacing(0)

        lbl_review_title = QLabel("  事件流 / 专家评审流水线")
        lbl_review_title.setStyleSheet(f"background-color: {T.PRIMARY}; color: {T.PAPER}; font-size: 15px; padding: 8px 4px;")
        review_layout.addWidget(lbl_review_title)

        self.lbl_review_summary = QLabel("事件 0 · 待评审 0 · 准确率 --")
        self.lbl_review_summary.setStyleSheet(
            f"background: {T.PRIMARY_SOFT}; color: {T.INK}; font-size: 12px; padding: 6px 10px;"
            f" border-bottom: 2px solid {T.PRIMARY_MID};"
        )
        review_layout.addWidget(self.lbl_review_summary)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.snapshots_widget = QWidget()
        self.snapshots_widget.setStyleSheet("background: transparent;")
        self.snapshots_layout = QVBoxLayout(self.snapshots_widget)
        self.snapshots_layout.setContentsMargins(10, 10, 6, 10)
        self.snapshots_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.snapshots_layout.setSpacing(10)
        self.lbl_empty = QLabel("暂无事件\n\n违规与告警会自动生成卡片\n也可以点击下方「抓拍」手动记录")
        self.lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_empty.setStyleSheet(f"color: {T.DIM}; font-size: 13px; padding: 40px 0;")
        self.snapshots_layout.addWidget(self.lbl_empty)
        self.scroll_area.setWidget(self.snapshots_widget)
        review_layout.addWidget(self.scroll_area, stretch=1)

        snap_btn_layout = QHBoxLayout()
        snap_btn_layout.setContentsMargins(10, 8, 10, 6)
        self.btn_manual_snap = QPushButton("📸 抓拍")
        self.btn_export_all = QPushButton("导出")
        self.btn_manual_snap.setStyleSheet(f"""
            QPushButton {{ background-color: #FFF9C4; color: {T.PRIMARY}; font-size: 19px; padding: 12px; border: 3px solid {T.PRIMARY}; }}
            QPushButton:pressed {{ background-color: #FFF176; }}
        """)
        self.btn_export_all.setStyleSheet(f"""
            QPushButton {{ background-color: {T.BG}; color: {T.PRIMARY}; font-size: 14px; padding: 8px; }}
            QPushButton:pressed {{ background-color: {T.SKY}; }}
        """)
        apply_hard_shadow(self.btn_manual_snap, 5, 5)
        apply_hard_shadow(self.btn_export_all, 3, 3)
        snap_btn_layout.addWidget(self.btn_manual_snap, stretch=4)
        snap_btn_layout.addWidget(self.btn_export_all, stretch=1, alignment=Qt.AlignmentFlag.AlignBottom)
        btn_container = QWidget()
        btn_container.setLayout(snap_btn_layout)
        review_layout.addWidget(btn_container)

        self.btn_manual_snap.clicked.connect(self.manual_snapshot)
        self.btn_export_all.clicked.connect(self.export_all_images)

        score_panel = QWidget()
        score_panel.setStyleSheet(f"background-color: {T.BG}; border-top: 3px solid {T.PRIMARY};")
        score_layout = QVBoxLayout(score_panel)
        score_layout.setContentsMargins(8, 6, 8, 8)
        self.lbl_score = QLabel("TOTAL SCORE: 0.00 / 35")
        self.lbl_score.setStyleSheet(f"font-size: 20px; font-weight: 900; color: {T.PRIMARY}; border: none;")
        self.lbl_score.setAlignment(Qt.AlignmentFlag.AlignCenter)
        score_layout.addWidget(self.lbl_score)
        review_layout.addWidget(score_panel)

        content_layout.addWidget(review_container, stretch=1)
        main_layout.addLayout(content_layout, stretch=1)

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
        self.telemetry_card.update_telemetry(self.current_temp, self.current_humi, is_simulated=True)

    # ==========================================================================
    # 七、 遥测与状态机联动
    # ==========================================================================
    def _hw_status(self):
        link = self.inference_thread.serial
        alive = bool(link is not None and link.is_alive)
        hw = link.get_sensor_data() if alive else {}
        return alive, hw

    def _refresh_telemetry(self, state, force=False):
        now = time.time()
        if not force and now - self._last_tele_refresh < 0.25:
            return
        self._last_tele_refresh = now
        snap = telemetry.snapshot()
        alive, hw = self._hw_status()
        sm = self.state_machine
        footer = None
        if sm.latched:
            footer = f"■ 断电锁定：{sm.latch_reason} · 需现场长按控制盒复位键恢复供电（软件无法上电）"
        self.telemetry_card.update_telemetry(
            temp=self.current_temp,
            humi=self.current_humi,
            is_alert=state != "NORMAL",
            is_simulated=self.sensor_worker.is_simulated,
            is_external=self.sensor_worker.is_external_controlled,
            status_text=state,
            obj_temp=snap.get("obj_temp"),
            smoke=snap.get("smoke"),
            link_online=alive,
            relay_on=hw.get("relay_on") if alive else None,
            latched=(hw.get("latched") if alive else False) or sm.latched,
            footer=footer,
            state=state,
        )

    def on_engine_telemetry(self, state: str):
        """引擎完成一次遥测研判后刷新面板（研判本身在引擎中完成）"""
        self.current_temp = self.engine.current_temp
        self.current_humi = self.engine.current_humi
        self._refresh_telemetry(state)

    def on_sensor_data_updated(self, temp: float, humi: float):
        """兼容旧接口：只更新显示，不再在界面层研判"""
        self.current_temp = temp
        self.current_humi = humi
        self._refresh_telemetry(self.state_machine.current_state)

    def on_sensor_mode_changed(self, is_simulated: bool, msg: str):
        """传感器模式切换提示"""
        self.append_terminal_log(f"[硬件采集] {msg}")
        self._refresh_telemetry(self.state_machine.current_state, force=True)

    def on_safety_alert(self, alert_dict: dict):
        """状态机告警：立即刷新横幅与遥测卡片（日志与卡片由事件中心统一生成）"""
        self.refresh_banner(force=True)
        self._refresh_telemetry(self.state_machine.current_state, force=True)

    def append_terminal_log(self, text: str):
        """界面层消息（显示在终端顶部，不会被每帧的识别日志覆盖）"""
        stamp = datetime.now().strftime("%H:%M:%S")
        self._ui_msgs.append(f"[{stamp}] {text}")
        self._last_log_html = ""

    def refresh_banner(self, force=False):
        """紧急 / 警告横幅：CRITICAL 红色闪烁，WARNING 琥珀色常亮，NORMAL 隐藏"""
        sm = self.state_machine
        state = sm.current_state
        if state == "NORMAL":
            if self.alert_banner.isVisible():
                self.alert_banner.hide()
                self._banner_key = None
            return
        if state == "CRITICAL":
            link = self.inference_thread.serial
            po = getattr(link, "power_off_state", "idle")
            po_text = {
                "pending": "断电指令已下发，等待下位机确认",
                "acked": "下位机已确认断电，继电器已断开",
                "failed_no_link": "串口未连接 —— 请立即人工切断加热电源",
                "failed_no_ack": "下位机未应答 —— 请立即人工切断加热电源",
            }.get(po, "演示模式：软件已模拟断电锁定")
            reason = sm.latch_reason or "高危事件"
            text = f"■ 紧急处置｜{reason}｜{po_text}｜恢复供电需现场长按复位键"
            self._banner_blink = not self._banner_blink
            # 闪烁的两个相位都保证白字对比度 ≥ 4.5（5.6 / 8.7）
            bg = T.DANGER if self._banner_blink else T.DANGER_DEEP
            fg, border = T.PAPER, T.DANGER_DEEP
        else:
            warns = [e["title"] for e in events.active_events() if e.get("level") == "WARNING"]
            text = "⚠ 警告｜" + ("、".join(warns[:4]) if warns else "环境或行为异常，请关注")
            bg, fg, border = T.WARN_SOFT, T.WARN_TEXT, T.WARN
        key = (text, bg)
        if key == self._banner_key and not force:
            return
        self._banner_key = key
        self.alert_banner.setText(text)
        self.alert_banner.setStyleSheet(
            f"QLabel {{ background: {bg}; color: {fg}; border: 3px solid {border};"
            f" font-size: 15px; padding: 6px 12px; }}"
        )
        self.alert_banner.show()

    # ==========================================================================
    # 八、 视觉处理与业务流程
    # ==========================================================================
    def update_stats(self):
        secs = self.start_time.secsTo(QDateTime.currentDateTime())
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        state = self.state_machine.current_state
        color, soft, badge, cn = STATE_STYLE.get(state, STATE_STYLE["NORMAL"])
        if state == "CRITICAL" and self.state_machine.latched:
            cn = "断电锁定中"
        self.lbl_state_chip.set(f"{STATE_ICON.get(state, '')} {badge}", ink_on(color), color)
        self.lbl_stats.setText(f" 运行 {h:02d}:{m:02d}:{s:02d} | {cn} ")
        self.status_card.set_colors(border=color if state != "NORMAL" else T.PRIMARY,
                                    shadow=color if state != "NORMAL" else T.PRIMARY)
        self._refresh_telemetry(state, force=True)

    def _render_terminal(self, log_str: str):
        """把识别日志渲染为带颜色的 HTML；内容未变化时不重绘"""
        colors = {"[紧急]": T.DANGER, "[警告]": T.WARN_TEXT, "[提醒]": T.PRIMARY,
                  "[解除]": T.SAFE, "[信息]": T.DIM, "[系统]": T.DIM, "======": T.PRIMARY}
        # 把"最近事件"挪到最前面：它比逐帧识别结果更重要，不应该需要滚动才能看到
        head, sep, recent = log_str.partition("====== 最近事件 ======")
        ordered = (sep + recent + "\n" + head) if sep else log_str
        parts = []
        for msg in self._ui_msgs:
            parts.append(f"<span style='color:{T.DIM}'>{html.escape(msg)}</span>")
        for line in ordered.strip().splitlines():
            if not line.strip():
                continue
            color = T.INK
            for tag, c in colors.items():
                if tag in line:
                    color = c
                    break
            parts.append(f"<span style='color:{color}'>{html.escape(line)}</span>")
        doc = "".join(f"<p style='margin:0 0 3px 0; line-height:125%;'>{x}</p>" for x in parts)
        if doc != self._last_log_html:
            self._last_log_html = doc
            self.txt_terminal.setHtml(doc)
        fps_line = next((l for l in log_str.splitlines() if l.startswith("[系统]")), "")
        if "FPS" in fps_line:
            seg = fps_line.split("|")[0].replace("[系统]", "").strip()
            self.lbl_fps.set(seg, T.PRIMARY, T.PRIMARY_SOFT, T.PRIMARY_MID)

    def update_display(self, pano_img, desk_img, alarms, violations, curr_f, tot_f, log_str, state=None):
        # 研判已在引擎中完成，这里只做显示
        self.latest_unattended = alarms.get("unattended", False)
        self.latest_chem_items = alarms.get("chem_items", [])
        state = state or self.state_machine.current_state
        self._refresh_telemetry(state)

        if log_str:
            self._render_terminal(log_str)

        # 视频标题栏状态胶囊
        present = alarms.get("person_present")
        if not alarms.get("vision_ok", True):
            self.chip_pano.set("视觉离线", T.PAPER, T.DANGER)
        elif present is None:
            self.chip_pano.set("画面无效", T.DIM, T.PAPER, T.PRIMARY_MID)
        elif self.latest_unattended:
            self.chip_pano.set("▲ 无人值守", T.WARN_INK, T.WARN)
        elif alarms.get("ppe_violation") or alarms.get("eating_drinking") or alarms.get("sleeping"):
            self.chip_pano.set("■ 违规", T.PAPER, T.DANGER)
        else:
            self.chip_pano.set("√ 人员在岗", T.SAFE, T.PAPER, T.SAFE)
        n_items = len(self.latest_chem_items)
        if alarms.get("falling_warning"):
            self.chip_desk.set(f"▲ 器皿近边 · {n_items}件", T.WARN_INK, T.WARN)
        elif n_items:
            self.chip_desk.set(f"器皿 × {n_items}", T.SAFE, T.PAPER, T.SAFE)
        else:
            self.chip_desk.set("台面空闲", T.PRIMARY, T.PAPER, T.PRIMARY_MID)

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

        self.lbl_pano.setPixmap(self.convert_cv_qt(pano_img))
        self.lbl_desk.setPixmap(self.convert_cv_qt(desk_img))

        self.latest_pano = pano_img
        self.latest_desk = desk_img

    # ------------------------------------------------------------------
    # 事件卡片
    # ------------------------------------------------------------------
    def pull_event_feed(self):
        """从事件中心拉取新事件，自动生成卡片"""
        for seq, ev in events.drain_feed(self._feed_seq):
            self._feed_seq = max(self._feed_seq, seq)
            if ev.get("kind") in CARD_SKIP_KINDS or ev.get("level") == "INFO":
                continue
            pano, desk = ev.get("frames", (None, None))
            if pano is None and desk is None:
                pano, desk = self.latest_pano, self.latest_desk
            if pano is None:
                continue
            self._append_item(ev, pano, desk)

    def _append_item(self, ev, pano, desk):
        item = {
            "time": ev.get("time_str", QDateTime.currentDateTime().toString("HH:mm:ss")),
            "title": ev.get("title", "抓拍"),
            "message": ev.get("message", ""),
            "level": ev.get("level", "NOTICE"),
            "kind": ev.get("kind", "manual"),
            "uid": ev.get("uid"),
            "pixmap_pano": self.convert_cv_qt(pano),
            "pixmap_desk": self.convert_cv_qt(desk) if desk is not None else QPixmap(),
            "reviewed": False,
            "is_correct": False,
            "card": None,
            "btn_correct": None,
            "btn_wrong": None,
        }
        self.snapshots.append(item)
        self.add_snapshot_card(len(self.snapshots) - 1)
        self._update_review_summary()

    def manual_snapshot(self):
        """手动抓拍：作为一条事件记录（入库 + 截图），由事件流生成卡片"""
        if self.latest_pano is None:
            return
        events.set_latest_frames(self.latest_pano, self.latest_desk)
        events.log("人工抓拍", "值守人员 / 评委手动记录当前画面", level="NOTICE", kind="manual", snapshot=True)
        self.pull_event_feed()

    def add_snapshot_card(self, index):
        item = self.snapshots[index]
        color, soft, tag = LEVEL_STYLE.get(item["level"], LEVEL_STYLE["NOTICE"])
        if item["kind"] == "manual":
            tag = "抓拍"
        tag = {"WARNING": "▲ ", "CRITICAL": "■ "}.get(item["level"], "") + tag
        card = Card(padding=(10, 8, 8, 8), offset=4, accent=color)
        card.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        lay = card.body
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        chip = Chip(tag, ink_on(color), color, size=11)
        lbl_title = QLabel(item["title"])
        lbl_title.setStyleSheet(f"font-size: 13px; color: {T.INK};")
        lbl_title.setWordWrap(True)
        lbl_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lbl_title.setToolTip(item["title"])
        lbl_time = QLabel(item["time"])
        lbl_time.setStyleSheet(f"font-size: 11px; color: {T.DIM};")
        top.addWidget(chip)
        top.addWidget(lbl_title, stretch=1)
        top.addWidget(lbl_time)
        lay.addLayout(top)

        if item["message"]:
            lbl_msg = QLabel(item["message"])
            lbl_msg.setWordWrap(True)
            lbl_msg.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lbl_msg.setMinimumHeight(30)
            lbl_msg.setStyleSheet(f"font-size: 11px; color: {T.DIM};")
            lay.addWidget(lbl_msg)

        images_layout = QHBoxLayout()
        images_layout.setSpacing(6)
        for pm in (item['pixmap_pano'], item['pixmap_desk']):
            if pm.isNull():
                continue
            lbl_img = QLabel()
            lbl_img.setFixedSize(108, 81)
            lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_img.setStyleSheet(f"border: 2px solid {T.PRIMARY}; background: {T.SCREEN};")
            lbl_img.setPixmap(pm.scaled(104, 77, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
            images_layout.addWidget(lbl_img)
        images_layout.addStretch()
        lay.addLayout(images_layout)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)
        btn_correct = QPushButton("√ 正确")
        btn_wrong = QPushButton("✘ 误报")
        btn_correct.setStyleSheet(f"QPushButton {{ color: {T.SAFE}; border-color: {T.SAFE}; font-size: 12px; padding: 5px; margin-bottom: 3px; margin-right: 3px; }}"
                                  f" QPushButton:pressed {{ background: {T.SAFE}; color: {T.PAPER}; margin: 3px 0 0 3px; }}")
        btn_wrong.setStyleSheet(f"QPushButton {{ color: {T.DANGER}; border-color: {T.DANGER}; font-size: 12px; padding: 5px; margin-bottom: 3px; margin-right: 3px; }}"
                                f" QPushButton:pressed {{ background: {T.DANGER}; color: {T.PAPER}; margin: 3px 0 0 3px; }}")
        for b in (btn_correct, btn_wrong):
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        btn_correct.clicked.connect(lambda: self.mark_item_reviewed(index, True))
        btn_wrong.clicked.connect(lambda: self.mark_item_reviewed(index, False))
        btn_layout.addWidget(btn_correct)
        btn_layout.addWidget(btn_wrong)
        lay.addLayout(btn_layout)

        item["card"] = card
        item["btn_correct"] = btn_correct
        item["btn_wrong"] = btn_wrong

        def on_click(event):
            dialog = ReviewDialog(self, index)
            dialog.exec()

        card.mousePressEvent = on_click
        self.lbl_empty.hide()
        self.snapshots_layout.insertWidget(0, card)

        # 只保留最近 MAX_CARDS 张卡片，避免长时间运行占用内存与布局开销
        live = [it for it in self.snapshots if it["card"] is not None]
        if len(live) > self.MAX_CARDS:
            old = live[0]
            old["card"].setParent(None)
            old["card"].deleteLater()
            old["card"] = None

    def _update_review_summary(self):
        total = len(self.snapshots)
        reviewed = [it for it in self.snapshots if it["reviewed"]]
        correct = sum(1 for it in reviewed if it["is_correct"])
        prec = f"{correct / len(reviewed) * 100:.0f}%" if reviewed else "--"
        self.lbl_review_summary.setText(
            f"事件 {total} · 待评审 {total - len(reviewed)} · 准确率 {prec}（{correct}/{len(reviewed)}）"
        )

    def mark_item_reviewed(self, index, is_correct):
        item = self.snapshots[index]
        if item["reviewed"]:
            return

        item["reviewed"] = True
        item["is_correct"] = is_correct
        events.set_review(item.get("uid"), is_correct)

        if is_correct:
            self.expert_score = min(35.0, self.expert_score + 1.75)
            self.lbl_score.setText(f"TOTAL SCORE: {self.expert_score:.2f} / 35")
            bg = T.SAFE_SOFT
        else:
            bg = T.DANGER_SOFT

        if item["card"] is not None:
            item["btn_correct"].setEnabled(False)
            item["btn_wrong"].setEnabled(False)
            item["card"].set_colors(bg=bg)
        self._update_review_summary()

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
            file_name = f"snapshot_{i+1:02d}_{item['time'].replace(':','-')}_{item.get('kind', 'event')}.jpg"
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
            self, "选择自定义模型文件（只加载自己训练的可信模型）", "",
            "Model Files (*.pt *.onnx);;RKNN 模型目录中的任意文件 (*)"
        )
        if not file_path:
            return
        # 选中 RKNN 模型目录中的文件时，使用其所在目录
        parent = os.path.dirname(file_path)
        if parent.endswith("_rknn_model"):
            file_path = parent
        slot, ok = QInputDialog.getItem(self, "替换哪个模型？", "目标通道：",
                                        ["人员模型（全景 Camera A）", "物品模型（台面 Camera B）"], 0, False)
        if not ok:
            return
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            success, msg = self.inference_thread.dual_system.reload_model(
                "ppe" if slot.startswith("人员") else "chem", file_path)
        finally:
            QApplication.restoreOverrideCursor()
        if success:
            QMessageBox.information(self, "模型已挂载", msg)
        else:
            QMessageBox.warning(self, "模型加载失败", msg)

    def import_media(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择视频或图片", "", "Media (*.mp4 *.avi *.mov *.mkv *.jpg *.png *.jpeg)"
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
        self.lbl_media_path.setText("当前媒体: 摄像头模式（按 config.yaml 中的机位）")
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
            "• USB/虚拟设备序号：输入数字 1 或 2\n"
            "提示：请在手机 App 中开启访问密码，避免同网段他人查看画面",
            QLineEdit.EchoMode.Normal,
            "http://192.168.1.100:8080/video"
        )
        if ok and url.strip():
            target_src2 = url.strip()
            if target_src2.isdigit():
                target_src2 = int(target_src2)
            self.lbl_media_path.setText(f"当前媒体: 双摄协同 (主摄: 默认, 副摄: {target_src2})")
            self.playback_widget.hide()
            self.inference_thread.dual_system.load_media(src1=0, src2=target_src2)
            self.btn_infer_phone.setChecked(True)
        else:
            if not self.btn_infer_import.isChecked():
                self.btn_infer_camera.setChecked(True)

    def toggle_play_pause(self):
        sys_obj = self.inference_thread.dual_system
        sys_obj.set_pause(not sys_obj.is_paused)
        self.btn_play_pause.setText("▶" if sys_obj.is_paused else "⏸")

    def slider_pressed(self):
        self.is_slider_dragging = True

    def slider_released(self):
        self.is_slider_dragging = False
        self.inference_thread.dual_system.seek(self.slider_progress.value())

    def update_system_config(self):
        self.inference_thread.dual_system.set_config(
            "dual", self.btn_model_ppe.isChecked(), self.btn_model_chem.isChecked(),
            self.spin_conf.value(), self.spin_iou.value(), self.spin_size.value())

    def closeEvent(self, event):
        """窗口关闭时优雅退出所有后台线程"""
        for t in (getattr(self, "feed_timer", None), getattr(self, "banner_timer", None)):
            if t is not None:
                t.stop()
        if getattr(self, "_own_engine", False):
            self.engine.stop()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    load_pixel_font()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
