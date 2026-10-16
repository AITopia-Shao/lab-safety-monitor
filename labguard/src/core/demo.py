"""
演示场景注入（网页大屏「环境控制 → 演示数据」）
src/core/demo.py

路演现场不能真的把烧杯烧干，所以用"场景注入"模拟遥测：
  · 只在 未接入下位机 且 config.demo.allow_ipc_telemetry = true 时可用（接上真硬件就拒绝，真假数据不混用）；
  · 写入一个临时 IPC 文件，SensorWorker 原样读取，后续研判链路与真实数据完全一致。
"""
import json
import math
import os
import threading
import time
from typing import Dict, Any, Optional

from src.config import cfg
from src.hardware.sensor_worker import SensorWorker, _serial_link

# 场景：基础值 + 可选的热源温度曲线 obj(t)
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "normal": {"name": "正常环境", "temp": 25.0, "humi": 55.0, "obj": None, "smoke": None},
    "heating": {"name": "正常加热→沸腾", "temp": 26.0, "humi": 58.0,
                "obj": lambda t: min(99.0, 40.0 + 1.2 * t), "smoke": 300},
    "dry_burn": {"name": "烧干（升温突破阈值）", "temp": 27.0, "humi": 52.0,
                 "obj": lambda t: 99.0 if t < 8 else min(160.0, 99.0 + 0.8 * (t - 8)), "smoke": 350},
    "smoke": {"name": "烟雾超标", "temp": 27.0, "humi": 50.0, "obj": lambda t: 85.0, "smoke": 2300},
    "humid": {"name": "高湿受潮", "temp": 25.0, "humi": 86.0, "obj": None, "smoke": None},
    "dry_air": {"name": "空气过干", "temp": 25.0, "humi": 28.0, "obj": None, "smoke": None},
    "hot": {"name": "环境高温", "temp": 42.0, "humi": 50.0, "obj": None, "smoke": None},
}


class DemoInjector:
    def __init__(self, engine=None):
        self.engine = engine
        self.scenario: Optional[str] = None
        self.custom: Dict[str, Any] = {}
        self.started = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

    def available(self) -> (bool, str):
        if not bool(cfg("demo.allow_ipc_telemetry", True)):
            return False, "配置 demo.allow_ipc_telemetry 为 false，演示注入已关闭"
        link = getattr(self.engine, "serial", None)
        if link is None:
            link = _serial_link()
        if link is not None and (getattr(link, "is_open", False) or getattr(link, "is_alive", False)):
            return False, "已接入下位机，真实数据优先，拒绝演示注入"
        return True, ""

    def start(self, scenario: str, custom: Optional[Dict[str, Any]] = None) -> str:
        if scenario != "custom" and scenario not in SCENARIOS:
            raise ValueError("未知场景")
        custom = dict(custom or {})
        if scenario == "custom":
            for key, default in (("temp", 25), ("humi", 55), ("obj_temp", None), ("smoke", None)):
                value = custom.get(key, default)
                if value is None and default is None:
                    custom[key] = None
                    continue
                try:
                    value = float(value)
                    if not math.isfinite(value):
                        raise ValueError
                    custom[key] = int(value) if key == "smoke" else value
                except (TypeError, ValueError, OverflowError):
                    raise ValueError(f"{key} 需要有效数值") from None
        with self._lifecycle_lock:
            ok, why = self.available()
            if not ok:
                raise PermissionError(why)
            self._stop_thread()
            with self._lock:
                self.scenario = scenario
                self.custom = custom
                self.started = time.time()
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._loop, name="DemoInjector", daemon=True)
            self._thread.start()
        return SCENARIOS.get(scenario, {}).get("name", "自定义数值")

    def stop(self) -> str:
        with self._lifecycle_lock:
            self._stop_thread()
        return "演示注入已停止"

    def _stop_thread(self):
        """调用方持有生命周期锁；先等写入结束，再删除文件，避免停止后被重新写回。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        with self._lock:
            self.scenario = None
        self._remove_ipc()

    @staticmethod
    def _remove_ipc():
        for path in (SensorWorker.IPC_TELEMETRY_PATH, SensorWorker.IPC_TELEMETRY_PATH + ".tmp"):
            try:
                os.remove(path)
            except OSError:
                pass

    def _values(self) -> Dict[str, Any]:
        t = time.time() - self.started
        if self.scenario == "custom":
            c = self.custom
            return {"temp": float(c.get("temp", 25)), "humi": float(c.get("humi", 55)),
                    "obj_temp": c.get("obj_temp"), "smoke": c.get("smoke")}
        sc = SCENARIOS[self.scenario]
        obj = sc["obj"](t) if callable(sc["obj"]) else sc["obj"]
        return {"temp": sc["temp"], "humi": sc["humi"],
                "obj_temp": round(obj, 1) if obj is not None else None, "smoke": sc["smoke"]}

    def _loop(self):
        try:
            while not self._stop.is_set() and self.available()[0]:
                payload = dict(self._values(), scenario=SCENARIOS.get(self.scenario, {}).get("name", "自定义"),
                               source="WEB_DEMO", timestamp=time.time())
                path = SensorWorker.IPC_TELEMETRY_PATH
                try:
                    with open(path + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(payload, f, allow_nan=False)
                    os.replace(path + ".tmp", path)
                except Exception as e:
                    print(f"[Demo] 写入失败: {e}")
                self._stop.wait(0.5)
        finally:
            self._remove_ipc()
            with self._lock:
                self.scenario = None

    def status(self) -> Dict[str, Any]:
        ok, why = self.available()
        with self._lock:
            return {"available": ok, "reason": why, "scenario": self.scenario,
                    "scenario_name": SCENARIOS.get(self.scenario, {}).get("name") if self.scenario else None,
                    "elapsed_s": round(time.time() - self.started) if self.scenario else 0,
                    "scenarios": {k: v["name"] for k, v in SCENARIOS.items()}}
