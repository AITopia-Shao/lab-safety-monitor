"""
环境遥测采集后台线程
src/hardware/sensor_worker.py

数据来源（按优先级自动切换，每个周期都会重新检查，支持热插拔）：
  1. 下位机串口（SerialComm 单例）：SHT30 温湿度 + MLX90614 热源温度 + MQ-2 烟雾
  2. 网页大屏「演示数据」写入的 IPC 文件（src/core/demo.py）（仅在未接入下位机、且 config.demo.allow_ipc_telemetry 为 true 时）
  3. 内部自主模拟（温湿度在 25℃ / 55% 附近轻微抖动；热源温度与烟雾标记为"未接入"）

本线程不再自己打开串口：串口统一由 SerialComm 管理，避免两个模块争抢同一端口。
对外的信号与属性保持不变：sensor_data_updated(float, float)、mode_changed(bool, str)、
is_simulated、is_external_controlled。
"""
import sys
import os

_current_file = os.path.abspath(__file__)
_root_dir = os.path.dirname(os.path.dirname(os.path.dirname(_current_file)))
for _p in (_root_dir, os.path.join(_root_dir, "LabSafetyMonitor")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import re
import json
import math
import time
import random
import tempfile
from typing import Optional, Tuple

try:
    from PySide6.QtCore import QThread, Signal
except ImportError:
    from PyQt6.QtCore import QThread, pyqtSignal as Signal

from src.config import cfg
from src.core.telemetry import telemetry, SOURCE_HARDWARE, SOURCE_IPC, SOURCE_SIMULATED

MODE_HARDWARE = "hardware"
MODE_IPC = "ipc"
MODE_SIM = "simulated"


def _serial_link():
    try:
        from serial_comm import SerialComm
        return SerialComm.get_shared()
    except Exception:
        return None


class SensorWorker(QThread):
    """环境遥测后台线程"""

    # 实时环境温度 (℃) 与相对湿度 (%)
    sensor_data_updated = Signal(float, float)
    # 是否为模拟数据 (bool)，状态描述 (str)
    mode_changed = Signal(bool, str)

    IPC_TELEMETRY_PATH = os.path.join(tempfile.gettempdir(), "lab_safety_env_telemetry.json")

    def __init__(self, port: str = "auto", baudrate: int = 115200, timeout: float = 1.0, parent=None):
        super().__init__(parent)
        # port / baudrate 仅为兼容旧调用保留；实际串口由 SerialComm 统一管理（见 config.yaml → serial）
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout

        self._running = False
        self._mode: Optional[str] = None
        self._sim_temp_base = 25.0
        self._sim_humi_base = 55.0
        self._last_hw_ts = 0.0

    @property
    def is_simulated(self) -> bool:
        return self._mode != MODE_HARDWARE

    @property
    def is_external_controlled(self) -> bool:
        return self._mode == MODE_IPC

    # 旧版 ASCII 协议解析，保留供调试工具使用
    def parse_sensor_ascii(self, line: str) -> Optional[Tuple[float, float]]:
        if not line:
            return None
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                t = float(data.get("temp", data.get("temperature", data.get("t", 0.0))))
                h = float(data.get("humi", data.get("humidity", data.get("h", 0.0))))
                return t, h
            except Exception:
                pass
        temp_match = re.search(r"(?:TEMPERATURE|TEMP|T)\s*[:=]\s*([+-]?\d+(?:\.\d+)?)", line, re.IGNORECASE)
        humi_match = re.search(r"(?:HUMIDITY|HUMI|HUM|H)\s*[:=]\s*([+-]?\d+(?:\.\d+)?)", line, re.IGNORECASE)
        if temp_match and humi_match:
            return float(temp_match.group(1)), float(humi_match.group(1))
        parts = re.split(r"[,;\s]+", line)
        if len(parts) >= 2:
            try:
                return float(parts[0]), float(parts[1])
            except ValueError:
                pass
        return None

    # ------------------------------------------------------------------
    def _switch_mode(self, mode: str, msg: str):
        if mode != self._mode:
            self._mode = mode
            print(f"[SensorWorker] {msg}")
            self.mode_changed.emit(mode != MODE_HARDWARE, msg)

    def _read_ipc(self) -> Optional[dict]:
        if not bool(cfg("demo.allow_ipc_telemetry", True)):
            return None
        try:
            if os.path.exists(self.IPC_TELEMETRY_PATH) and \
                    time.time() - os.path.getmtime(self.IPC_TELEMETRY_PATH) < 2.5:
                with open(self.IPC_TELEMETRY_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "temp" in data and "humi" in data:
                    # IPC 文件也可能由调试工具写入，拒绝坏值，避免采集线程退出。
                    for key in ("temp", "humi", "obj_temp", "smoke"):
                        value = data.get(key)
                        if value is None and key in ("obj_temp", "smoke"):
                            continue
                        value = float(value)
                        if not math.isfinite(value):
                            return None
                        data[key] = int(value) if key == "smoke" else value
                    return data
        except Exception:
            pass
        return None

    def _sleep(self, ms: int):
        step = 100
        for _ in range(max(1, ms // step)):
            if not self._running:
                return
            self.msleep(step)

    def run(self):
        self._running = True
        while self._running:
            link = _serial_link()
            hw = link.get_sensor_data() if (link is not None and link.is_alive) else None

            if hw is not None:
                self._switch_mode(MODE_HARDWARE, f"已接入下位机实时遥测（{link.port}）")
                if hw["ts"] != self._last_hw_ts:
                    self._last_hw_ts = hw["ts"]
                    telemetry.update(SOURCE_HARDWARE, temp=hw["temp"], humi=hw["humi"],
                                     obj_temp=hw["obj_temp"], smoke=hw["smoke"])
                    if hw["temp"] is not None and hw["humi"] is not None:
                        self.sensor_data_updated.emit(round(hw["temp"], 1), round(hw["humi"], 1))
                    else:
                        print("[SensorWorker] 下位机报告 SHT30 温湿度传感器异常")
                self._sleep(200)
                continue

            # 串口刚连接或暂时没有新遥测时，也不能转去读取演示文件。
            ext = self._read_ipc() if not (link is not None and link.is_open) else None
            if ext is not None:
                scenario = ext.get("scenario", "")
                self._switch_mode(MODE_IPC, f"未接入下位机，使用环境模拟控制器数据（{scenario or '实时调控'}）")
                t, h = float(ext["temp"]), float(ext["humi"])
                obj = ext.get("obj_temp")
                smk = ext.get("smoke")
                telemetry.update(SOURCE_IPC, temp=t, humi=h,
                                 obj_temp=float(obj) if obj is not None else None,
                                 smoke=int(smk) if smk is not None else None)
                self.sensor_data_updated.emit(round(t, 1), round(h, 1))
                self._sleep(100)
                continue

            status = link.status_text() if link is not None else "串口模块未启动"
            self._switch_mode(MODE_SIM, f"{status}，切换为自主模拟遥测（仅用于界面演示）")
            t = max(24.0, min(26.0, self._sim_temp_base + random.gauss(0.0, 0.25)))
            h = max(50.0, min(60.0, self._sim_humi_base + random.gauss(0.0, 0.8)))
            telemetry.update(SOURCE_SIMULATED, temp=t, humi=h, obj_temp=None, smoke=None)
            self.sensor_data_updated.emit(round(t, 1), round(h, 1))
            self._sleep(1000)

        print("[SensorWorker] 采集线程已安全退出")

    def stop(self):
        self._running = False
        self.wait(1500)
