"""
全局遥测快照
src/core/telemetry.py

SensorWorker 负责写入，状态机与推理日志负责读取。
这样状态机可以拿到界面层不传递的额外通道（热源温度、烟雾），而不需要修改界面代码。
"""
import time
import threading
from typing import Optional, Dict, Any

# 数据来源
SOURCE_NONE = "none"
SOURCE_HARDWARE = "hardware"      # 下位机串口
SOURCE_IPC = "ipc"                # 网页大屏「演示数据」注入
SOURCE_SIMULATED = "simulated"    # 内部自主模拟


class TelemetryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "temp": None,       # 环境温度 ℃（SHT30）
            "humi": None,       # 相对湿度 %（SHT30）
            "obj_temp": None,   # 热源表面温度 ℃（MLX90614），None = 未接入
            "smoke": None,      # MQ-2 原始 ADC，None = 未接入或未预热
            "source": SOURCE_NONE,
            "ts": 0.0,
        }

    def update(self, source: str, **fields):
        with self._lock:
            self._data.update(fields)
            self._data["source"] = source
            self._data["ts"] = time.time()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Optional[Any] = None):
        with self._lock:
            return self._data.get(key, default)


telemetry = TelemetryStore()
