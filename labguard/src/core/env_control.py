"""
外部温湿度控制（排风扇 + 加湿雾化片）
src/core/env_control.py

控制律在下位机 ESP32 本地执行（断网、断上位机也能工作），这里实现"同一套控制律"的 Python 版：
  · 接入下位机时：上位机只下发设定值 / 手动指令（CMD 0x04 / 0x05），执行器状态以下位机上报（MSG 0x13）为准；
  · 未接入下位机时：用同一控制律仿真执行器状态，供界面演示。

控制律（与 firmware/lab_guard_esp32 一致）：
  排风扇 PWM：烟雾 ≥ smoke_fan_raw → 100%（同时下位机切断加热）；
             否则取 max(温度需求, 湿度需求)：
               温度需求 = 环境温度 > t_set+1 时 40% + 20%/℃
               湿度需求 = 湿度 > h_high 时 40% + 6%/%RH
             需求为 0 但仍在回差区（温度 > t_set 或 湿度 > h_high-3）时保持 30%，避免频繁启停
  加湿片：湿度 < h_low-2 开，> h_low+2 关；有烟雾或湿度高于 h_high 时强制关闭
  手动模式：总后台手动指定，manual_timeout_s 后自动回到自动模式
"""
import time
import threading
from typing import Dict, Any, Optional, Tuple

from src.config import cfg, set_value
from src.core.telemetry import telemetry


def control_law(temp: Optional[float], humi: Optional[float], smoke: Optional[int],
                t_set: float, h_low: float, h_high: float, smoke_fan_raw: float,
                prev_fan: int = 0, prev_humid: bool = False) -> Tuple[int, bool, str]:
    """返回 (fan_pwm 0-100, humid_on, 原因说明)"""
    if smoke is not None and smoke >= smoke_fan_raw:
        return 100, False, "烟雾超标：排风满速，禁止加湿"
    fan, reasons = 0, []
    if temp is not None and temp > t_set + 1:
        fan = max(fan, min(100, int(40 + 20 * (temp - t_set - 1))))
        reasons.append(f"温度 {temp:.1f}℃ 高于设定 {t_set:.0f}℃")
    if humi is not None and humi > h_high:
        fan = max(fan, min(100, int(40 + 6 * (humi - h_high))))
        reasons.append(f"湿度 {humi:.0f}% 高于上限 {h_high:.0f}%")
    if fan == 0 and prev_fan > 0 and ((temp is not None and temp > t_set) or
                                      (humi is not None and humi > h_high - 3)):
        fan = 30
        reasons.append("回差区保持低速")
    humid = prev_humid
    if humi is not None:
        if humi < h_low - 2:
            humid = True
        elif humi > h_low + 2:
            humid = False
        if humi > h_high:
            humid = False
    if humid:
        if humi is None:
            reasons.append("湿度数据暂缺，保持原加湿状态")
        else:
            reasons.append(f"湿度 {humi:.0f}% 低于下限 {h_low:.0f}%：加湿")
    return fan, humid, "；".join(reasons) if reasons else "环境正常，执行器待机"


class EnvController:
    def __init__(self, serial_link=None):
        self.serial = serial_link
        self._lock = threading.RLock()
        self.fan_pwm = 0
        self.humid_on = False
        self.reason = "等待遥测"
        self.manual_until = 0.0
        self._manual = (0, False)
        self._sent_setpoint = None

    # ------------------------------------------------------------------
    def _params(self):
        return (float(cfg("env.t_set_c", 26.0)), float(cfg("env.h_low", 40.0)),
                float(cfg("env.h_high", 70.0)), float(cfg("env.smoke_fan_raw", 1200)))

    def _hw(self) -> Optional[dict]:
        link = self.serial
        if link is not None and getattr(link, "is_alive", False):
            return link.get_sensor_data()
        return None

    @property
    def manual(self) -> bool:
        with self._lock:
            return time.time() < self.manual_until

    def tick(self):
        """引擎每秒调用一次"""
        with self._lock:
            t_set, h_low, h_high, smoke_fan = self._params()
            hw = self._hw()
            if hw is not None:
                sp = (t_set, h_low, h_high, self.manual)
                if sp != self._sent_setpoint and hasattr(self.serial, "set_env"):
                    if self.serial.set_env(1 if self.manual else 0, t_set, h_low, h_high):
                        self._sent_setpoint = sp
                if hw.get("fan_pwm") is not None:
                    self.fan_pwm = int(hw.get("fan_pwm") or 0)
                    self.humid_on = bool(hw.get("humid_on"))
                    self.reason = "下位机本地闭环控制" + ("（手动）" if self.manual else "")
                return
            self._sent_setpoint = None
            snap = telemetry.snapshot()
            if snap.get("smoke") is not None and snap["smoke"] >= smoke_fan:
                self.fan_pwm, self.humid_on = 100, False
                self.reason = "烟雾超标：排风满速，禁止加湿"
                return
            if self.manual:
                self.fan_pwm, self.humid_on = self._manual
                self.reason = "总后台手动控制"
                return
            self.fan_pwm, self.humid_on, self.reason = control_law(
                snap.get("temp"), snap.get("humi"), snap.get("smoke"),
                t_set, h_low, h_high, smoke_fan, self.fan_pwm, self.humid_on)

    def set_setpoint(self, t_set: float, h_low: float, h_high: float) -> str:
        t_set, h_low, h_high = float(t_set), float(h_low), float(h_high)
        if not (15 <= t_set <= 35):
            raise ValueError("温度设定需在 15–35℃")
        if not (20 <= h_low <= 60 and 50 <= h_high <= 90 and h_high - h_low >= 10):
            raise ValueError("湿度下限 20–60%、上限 50–90%，且上限比下限至少高 10%")
        with self._lock:
            set_value("env.t_set_c", t_set)
            set_value("env.h_low", h_low)
            set_value("env.h_high", h_high)
        self.tick()
        return "设定值已更新"

    def set_manual(self, fan_pwm: Optional[int], humid: Optional[bool]) -> str:
        """手动控制；fan_pwm 为 None 表示恢复自动"""
        if fan_pwm is None:
            with self._lock:
                self.manual_until = 0.0
                self._sent_setpoint = None
            self.tick()
            return "已恢复自动控制"
        fan_pwm = max(0, min(100, int(fan_pwm)))
        with self._lock:
            self._manual = (fan_pwm, bool(humid))
            self.manual_until = time.time() + float(cfg("env.manual_timeout_s", 600.0))
            if self._hw() is not None and hasattr(self.serial, "set_actuator"):
                self.serial.set_actuator(fan_pwm, bool(humid))
        self.tick()
        return f"手动控制 {cfg('env.manual_timeout_s', 600) / 60:.0f} 分钟后自动恢复"

    def status(self) -> Dict[str, Any]:
        with self._lock:
            t_set, h_low, h_high, smoke_fan = self._params()
            return {"mode": "manual" if self.manual else "auto",
                    "manual_left_s": max(0, round(self.manual_until - time.time())) if self.manual else 0,
                    "t_set": t_set, "h_low": h_low, "h_high": h_high,
                    "fan_pwm": self.fan_pwm, "humid_on": self.humid_on, "reason": self.reason,
                    "source": "hardware" if self._hw() is not None else "simulated"}
