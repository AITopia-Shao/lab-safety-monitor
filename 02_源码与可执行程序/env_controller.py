#!/usr/bin/env python3
"""
实验室外部环境模拟控制台 (Lab Environment Simulator & Controller)
用于动态调节温度与湿度，提供预设危险场景注入与阶梯温升模拟，并通过 IPC 实时驱动主监控系统。
"""
import sys
import os
import json
import time

# 自动环境引导：确保直接使用系统 python 也能识别本地 venv 依赖
_current_file = os.path.abspath(__file__)
_root_dir = os.path.dirname(_current_file)
for _cand_root in [_root_dir, os.path.join(_root_dir, "LabSafetyMonitor")]:
    _venv_lib = os.path.join(_cand_root, "venv", "lib")
    if os.path.isdir(_venv_lib):
        for _py_ver in sorted(os.listdir(_venv_lib), reverse=True):
            _sp = os.path.join(_venv_lib, _py_ver, "site-packages")
            if os.path.isdir(_sp) and _sp not in sys.path:
                sys.path.insert(0, _sp)

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QSlider, QDoubleSpinBox, QFrame, QGridLayout,
        QGraphicsDropShadowEffect
    )
    from PySide6.QtGui import QColor, QFont
except ImportError:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QSlider, QDoubleSpinBox, QFrame, QGridLayout,
        QGraphicsDropShadowEffect
    )
    from PyQt6.QtGui import QColor, QFont

import tempfile

IPC_FILE_PATH = os.path.join(tempfile.gettempdir(), "lab_safety_env_telemetry.json")


def apply_hard_shadow(widget, offset_x=4, offset_y=4, color="#000000"):
    """为控件挂载符合新粗野主义规范的纯黑物理硬阴影"""
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(0)
    shadow.setOffset(offset_x, offset_y)
    shadow.setColor(QColor(color))
    widget.setGraphicsEffect(shadow)


class EnvControllerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("实验室环境仿真控制台 / Lab Environment Controller")
        self.resize(780, 680)

        # 状态变量
        self.current_temp = 25.0
        self.current_humi = 55.0
        self.current_scenario = "正常受控模式"

        # 自动升温模拟器状态
        self.is_ramping = False
        self.ramp_target_temp = 72.0
        self.ramp_step = 1.2

        self.init_ui()

        # IPC 广播定时器 (10Hz 高频同步)
        self.ipc_timer = QTimer(self)
        self.ipc_timer.timeout.connect(self.broadcast_telemetry)
        self.ipc_timer.start(100)

        # 动态温升定时器 (每 500ms 推进一次)
        self.ramp_timer = QTimer(self)
        self.ramp_timer.timeout.connect(self._on_ramp_tick)

    def init_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        central.setStyleSheet("background-color: #ECEFF1;")

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(24, 20, 24, 20)
        main_layout.setSpacing(16)

        # === 1. 顶部 Header 标题栏 ===
        header_card = QFrame()
        header_card.setStyleSheet("""
            QFrame {
                background-color: #1A237E;
                border: 2px solid #000000;
                border-radius: 0px;
            }
        """)
        apply_hard_shadow(header_card, 4, 4)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(16, 12, 16, 12)

        lbl_title = QLabel("⚡ 实验室外部环境模拟控制中心 (TELEMETRY SIMULATOR)")
        lbl_title.setStyleSheet("color: #FFFFFF; font-size: 16px; font-weight: 900; border: none; background: transparent;")
        
        self.lbl_ipc_status = QLabel("● IPC 广播中 (10Hz)")
        self.lbl_ipc_status.setStyleSheet("""
            background-color: #00E676; color: #000000; font-size: 12px; font-weight: bold;
            padding: 4px 10px; border: 1px solid #000000;
        """)

        header_layout.addWidget(lbl_title)
        header_layout.addStretch()
        header_layout.addWidget(self.lbl_ipc_status)
        main_layout.addWidget(header_card)

        # === 2. 实时遥测大数字指示卡 ===
        readout_layout = QHBoxLayout()
        readout_layout.setSpacing(16)

        # 温度显示块
        self.card_temp = QFrame()
        self.card_temp.setStyleSheet("background-color: #FFFFFF; border: 2px solid #000000;")
        apply_hard_shadow(self.card_temp, 4, 4)
        vbox_temp = QVBoxLayout(self.card_temp)
        vbox_temp.setContentsMargins(14, 10, 14, 10)
        lbl_t_header = QLabel("CURRENT TEMPERATURE // 当前模拟温度")
        lbl_t_header.setStyleSheet("color: #616161; font-size: 11px; font-weight: bold; border: none;")
        self.lbl_disp_temp = QLabel("25.0 °C")
        self.lbl_disp_temp.setStyleSheet("color: #000000; font-size: 32px; font-weight: 900; border: none;")
        vbox_temp.addWidget(lbl_t_header)
        vbox_temp.addWidget(self.lbl_disp_temp)
        readout_layout.addWidget(self.card_temp, stretch=1)

        # 湿度显示块
        self.card_humi = QFrame()
        self.card_humi.setStyleSheet("background-color: #FFFFFF; border: 2px solid #000000;")
        apply_hard_shadow(self.card_humi, 4, 4)
        vbox_humi = QVBoxLayout(self.card_humi)
        vbox_humi.setContentsMargins(14, 10, 14, 10)
        lbl_h_header = QLabel("CURRENT HUMIDITY // 当前模拟相对湿度")
        lbl_h_header.setStyleSheet("color: #616161; font-size: 11px; font-weight: bold; border: none;")
        self.lbl_disp_humi = QLabel("55.0 %")
        self.lbl_disp_humi.setStyleSheet("color: #000000; font-size: 32px; font-weight: 900; border: none;")
        vbox_humi.addWidget(lbl_h_header)
        vbox_humi.addWidget(self.lbl_disp_humi)
        readout_layout.addWidget(self.card_humi, stretch=1)

        # 当前场景模式块
        self.card_mode = QFrame()
        self.card_mode.setStyleSheet("background-color: #FFF9C4; border: 2px solid #000000;")
        apply_hard_shadow(self.card_mode, 4, 4)
        vbox_mode = QVBoxLayout(self.card_mode)
        vbox_mode.setContentsMargins(14, 10, 14, 10)
        lbl_m_header = QLabel("ACTIVE SCENARIO // 激活场景")
        lbl_m_header.setStyleSheet("color: #616161; font-size: 11px; font-weight: bold; border: none;")
        self.lbl_disp_scenario = QLabel("正常受控模式")
        self.lbl_disp_scenario.setStyleSheet("color: #E65100; font-size: 18px; font-weight: 900; border: none;")
        vbox_mode.addWidget(lbl_m_header)
        vbox_mode.addWidget(self.lbl_disp_scenario)
        readout_layout.addWidget(self.card_mode, stretch=1)

        main_layout.addLayout(readout_layout)

        # === 3. 滑块无级精细控制区 ===
        control_frame = QFrame()
        control_frame.setStyleSheet("background-color: #FFFFFF; border: 2px solid #000000;")
        apply_hard_shadow(control_frame, 4, 4)
        control_layout = QVBoxLayout(control_frame)
        control_layout.setContentsMargins(16, 14, 16, 14)
        control_layout.setSpacing(12)

        lbl_ctrl_title = QLabel("🎛️ 无级手动调节 (Continuous Sliders)")
        lbl_ctrl_title.setStyleSheet("font-size: 14px; font-weight: 900; color: #1A237E; border: none;")
        control_layout.addWidget(lbl_ctrl_title)

        # 温度滑块行 (-10.0 ~ 100.0, 放大10倍以支持 0.1 精度)
        row_temp = QHBoxLayout()
        lbl_t_tag = QLabel("环境温度 (℃):")
        lbl_t_tag.setStyleSheet("font-size: 13px; font-weight: bold; min-width: 95px; border: none;")
        self.slider_temp = QSlider(Qt.Orientation.Horizontal)
        self.slider_temp.setRange(-100, 1000)  # -10.0 ~ 100.0
        self.slider_temp.setValue(int(self.current_temp * 10))
        self.slider_temp.setStyleSheet("""
            QSlider::groove:horizontal { height: 8px; background: #CFD8DC; border: 1px solid #000000; }
            QSlider::handle:horizontal { width: 20px; background: #FF5722; border: 2px solid #000000; margin: -6px 0; }
        """)
        self.spin_temp = QDoubleSpinBox()
        self.spin_temp.setRange(-10.0, 100.0)
        self.spin_temp.setSingleStep(0.5)
        self.spin_temp.setValue(self.current_temp)
        self.spin_temp.setStyleSheet("border: 2px solid #000000; font-size: 13px; font-weight: bold; padding: 4px;")

        row_temp.addWidget(lbl_t_tag)
        row_temp.addWidget(self.slider_temp, stretch=1)
        row_temp.addWidget(self.spin_temp)
        control_layout.addLayout(row_temp)

        # 湿度滑块行 (0.0 ~ 100.0)
        row_humi = QHBoxLayout()
        lbl_h_tag = QLabel("相对湿度 (%):")
        lbl_h_tag.setStyleSheet("font-size: 13px; font-weight: bold; min-width: 95px; border: none;")
        self.slider_humi = QSlider(Qt.Orientation.Horizontal)
        self.slider_humi.setRange(0, 1000)
        self.slider_humi.setValue(int(self.current_humi * 10))
        self.slider_humi.setStyleSheet("""
            QSlider::groove:horizontal { height: 8px; background: #CFD8DC; border: 1px solid #000000; }
            QSlider::handle:horizontal { width: 20px; background: #0288D1; border: 2px solid #000000; margin: -6px 0; }
        """)
        self.spin_humi = QDoubleSpinBox()
        self.spin_humi.setRange(0.0, 100.0)
        self.spin_humi.setSingleStep(1.0)
        self.spin_humi.setValue(self.current_humi)
        self.spin_humi.setStyleSheet("border: 2px solid #000000; font-size: 13px; font-weight: bold; padding: 4px;")

        row_humi.addWidget(lbl_h_tag)
        row_humi.addWidget(self.slider_humi, stretch=1)
        row_humi.addWidget(self.spin_humi)
        control_layout.addLayout(row_humi)

        main_layout.addWidget(control_frame)

        # === 4. 典型危险预设场景一键注入 ===
        preset_frame = QFrame()
        preset_frame.setStyleSheet("background-color: #FFFFFF; border: 2px solid #000000;")
        apply_hard_shadow(preset_frame, 4, 4)
        preset_layout = QVBoxLayout(preset_frame)
        preset_layout.setContentsMargins(16, 14, 16, 14)
        preset_layout.setSpacing(12)

        lbl_preset_title = QLabel("🎯 典型安全与危险场景一键注入 (Preset Scenarios)")
        lbl_preset_title.setStyleSheet("font-size: 14px; font-weight: 900; color: #1A237E; border: none;")
        preset_layout.addWidget(lbl_preset_title)

        grid = QGridLayout()
        grid.setSpacing(10)

        # 6个预设场景按钮样式
        btn_style_tpl = """
            QPushButton {{
                background-color: {bg};
                color: #000000;
                font-size: 13px;
                font-weight: 900;
                padding: 10px;
                border: 2px solid #000000;
                border-radius: 0px;
                margin-bottom: 2px;
                margin-right: 2px;
            }}
            QPushButton:pressed {{
                background-color: {bg_pressed};
                margin-top: 2px;
                margin-left: 2px;
                margin-bottom: 0px;
                margin-right: 0px;
            }}
        """

        presets = [
            ("🟢 正常受控 (25℃, 55%)", 25.0, 55.0, "标准环境SAFE", "#C8E6C9", "#A5D6A7"),
            ("🔴 高温干烧 (68℃, 35%)", 68.0, 35.0, "高温干烧 -> 继电器断电", "#FFCDD2", "#EF9A9A"),
            ("🟠 蒸汽泄漏 (52℃, 95%)", 52.0, 95.0, "高温高湿蒸汽泄漏", "#FFE0B2", "#FFCC80"),
            ("🟡 干燥静电 (22℃, 18%)", 22.0, 18.0, "低湿防静电火险", "#FFF9C4", "#FFF59D"),
            ("🟡 严重受潮 (25℃, 88%)", 25.0, 88.0, "高湿受潮防漏水", "#E1F5FE", "#B3E5FC"),
            ("🔵 严寒低温 (4℃, 50%)",  4.0,  50.0, "严寒低温试剂防凝", "#E0F2F1", "#B2DFDB"),
        ]

        for idx, (btn_text, p_temp, p_humi, p_name, bg, bg_press) in enumerate(presets):
            btn = QPushButton(btn_text)
            btn.setStyleSheet(btn_style_tpl.format(bg=bg, bg_pressed=bg_press))
            apply_hard_shadow(btn, 3, 3)
            btn.clicked.connect(lambda _, t=p_temp, h=p_humi, n=p_name: self.apply_preset(t, h, n))
            r = idx // 2
            c = idx % 2
            grid.addWidget(btn, r, c)

        preset_layout.addLayout(grid)
        main_layout.addWidget(preset_frame)

        # === 5. 底部动态模拟条与状态栏 ===
        bottom_frame = QFrame()
        bottom_frame.setStyleSheet("background-color: #FFFFFF; border: 2px solid #000000;")
        apply_hard_shadow(bottom_frame, 4, 4)
        bottom_layout = QHBoxLayout(bottom_frame)
        bottom_layout.setContentsMargins(14, 10, 14, 10)

        self.btn_auto_ramp = QPushButton("🚀 启动剧烈温升模拟 (25℃ 连续升至 72℃)")
        self.btn_auto_ramp.setStyleSheet("""
            QPushButton {
                background-color: #FF7043; color: #FFFFFF; font-size: 13px; font-weight: 900;
                padding: 10px 16px; border: 2px solid #000000;
            }
            QPushButton:pressed { background-color: #F4511E; }
        """)
        apply_hard_shadow(self.btn_auto_ramp, 3, 3)
        self.btn_auto_ramp.clicked.connect(self.toggle_ramp_simulation)

        self.lbl_channel_info = QLabel(f"IPC 通道: {IPC_FILE_PATH}")
        self.lbl_channel_info.setStyleSheet("color: #757575; font-size: 11px; border: none;")

        bottom_layout.addWidget(self.btn_auto_ramp)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.lbl_channel_info)
        main_layout.addWidget(bottom_frame)

        # 信号绑定
        self.slider_temp.valueChanged.connect(self._on_slider_temp_changed)
        self.spin_temp.valueChanged.connect(self._on_spin_temp_changed)
        self.slider_humi.valueChanged.connect(self._on_slider_humi_changed)
        self.spin_humi.valueChanged.connect(self._on_spin_humi_changed)

    def _on_slider_temp_changed(self, val):
        t = val / 10.0
        if abs(self.spin_temp.value() - t) > 0.05:
            self.spin_temp.setValue(t)
        self.update_temp(t, "手动调控")

    def _on_spin_temp_changed(self, val):
        s_val = int(val * 10)
        if self.slider_temp.value() != s_val:
            self.slider_temp.setValue(s_val)
        self.update_temp(val, "手动调控")

    def _on_slider_humi_changed(self, val):
        h = val / 10.0
        if abs(self.spin_humi.value() - h) > 0.05:
            self.spin_humi.setValue(h)
        self.update_humi(h, "手动调控")

    def _on_spin_humi_changed(self, val):
        s_val = int(val * 10)
        if self.slider_humi.value() != s_val:
            self.slider_humi.setValue(s_val)
        self.update_humi(val, "手动调控")

    def update_temp(self, temp: float, scenario: str = None):
        self.current_temp = round(temp, 1)
        self.lbl_disp_temp.setText(f"{self.current_temp:.1f} °C")
        if scenario:
            self.current_scenario = scenario
            self.lbl_disp_scenario.setText(scenario)
        # 高温变红警示
        if self.current_temp > 55.0:
            self.card_temp.setStyleSheet("background-color: #FFCDD2; border: 2px solid #D32F2F;")
            self.lbl_disp_temp.setStyleSheet("color: #D32F2F; font-size: 32px; font-weight: 900; border: none;")
        else:
            self.card_temp.setStyleSheet("background-color: #FFFFFF; border: 2px solid #000000;")
            self.lbl_disp_temp.setStyleSheet("color: #000000; font-size: 32px; font-weight: 900; border: none;")

    def update_humi(self, humi: float, scenario: str = None):
        self.current_humi = round(humi, 1)
        self.lbl_disp_humi.setText(f"{self.current_humi:.1f} %")
        if scenario:
            self.current_scenario = scenario
            self.lbl_disp_scenario.setText(scenario)
        if self.current_humi > 80.0 or self.current_humi < 30.0:
            self.card_humi.setStyleSheet("background-color: #FFF9C4; border: 2px solid #FBC02D;")
            self.lbl_disp_humi.setStyleSheet("color: #F57F17; font-size: 32px; font-weight: 900; border: none;")
        else:
            self.card_humi.setStyleSheet("background-color: #FFFFFF; border: 2px solid #000000;")
            self.lbl_disp_humi.setStyleSheet("color: #000000; font-size: 32px; font-weight: 900; border: none;")

    def apply_preset(self, temp: float, humi: float, scenario_name: str):
        if self.is_ramping:
            self.toggle_ramp_simulation()
        self.spin_temp.setValue(temp)
        self.spin_humi.setValue(humi)
        self.update_temp(temp, scenario_name)
        self.update_humi(humi, scenario_name)
        self.broadcast_telemetry()

    def toggle_ramp_simulation(self):
        self.is_ramping = not self.is_ramping
        if self.is_ramping:
            self.btn_auto_ramp.setText("⏹ 停止温升模拟")
            self.btn_auto_ramp.setStyleSheet("""
                QPushButton {
                    background-color: #D32F2F; color: #FFFFFF; font-size: 13px; font-weight: 900;
                    padding: 10px 16px; border: 2px solid #000000;
                }
            """)
            self.current_scenario = "剧烈温升动态模拟中"
            self.lbl_disp_scenario.setText(self.current_scenario)
            self.ramp_timer.start(500)
        else:
            self.btn_auto_ramp.setText("🚀 启动剧烈温升模拟 (25℃ 连续升至 72℃)")
            self.btn_auto_ramp.setStyleSheet("""
                QPushButton {
                    background-color: #FF7043; color: #FFFFFF; font-size: 13px; font-weight: 900;
                    padding: 10px 16px; border: 2px solid #000000;
                }
            """)
            self.ramp_timer.stop()

    def _on_ramp_tick(self):
        new_temp = self.current_temp + self.ramp_step
        if new_temp >= self.ramp_target_temp:
            new_temp = self.ramp_target_temp
            self.spin_temp.setValue(new_temp)
            self.toggle_ramp_simulation()
        else:
            self.spin_temp.setValue(round(new_temp, 1))

    def broadcast_telemetry(self):
        """以原子方式向 IPC 管道写入最新遥测数据"""
        payload = {
            "temp": round(self.current_temp, 1),
            "humi": round(self.current_humi, 1),
            "scenario": self.current_scenario,
            "source": "ENV_CONTROLLER",
            "timestamp": time.time()
        }
        tmp_target = IPC_FILE_PATH + ".tmp"
        try:
            with open(tmp_target, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_target, IPC_FILE_PATH)
        except Exception as e:
            print(f"[EnvController] 写入 IPC 异常: {e}")

    def closeEvent(self, event):
        """窗口退出时清理 IPC 文件，使主系统立刻识别并恢复自主模式"""
        self.ipc_timer.stop()
        self.ramp_timer.stop()
        if os.path.exists(IPC_FILE_PATH):
            try:
                os.remove(IPC_FILE_PATH)
            except Exception:
                pass
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = EnvControllerWindow()
    window.show()
    sys.exit(app.exec())
