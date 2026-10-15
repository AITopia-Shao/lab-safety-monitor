"""
仿真下位机：按 firmware/lab_guard_esp32 的协议与规则在 Python 中模拟 MCU 行为。
用于在没有硬件时测试 SerialComm 与状态机（tests/test_backend.py），
也可以作为固件行为的"可执行说明书"。
"""
import struct
import threading
import time

from serial_comm import (build_frame, FrameParser, CMD_HEARTBEAT, CMD_POWER_OFF, CMD_PLAY_VOICE,
                         CMD_SET_ENV, CMD_SET_ACT, MSG_TELEMETRY, MSG_ACK, MSG_EVENT, MSG_ENV_STATUS)


class FakeMCU:
    """实现 pyserial 对象中 SerialComm 用到的部分：read / write / close / is_open"""

    HARD_LIMIT_C = 150.0

    def __init__(self):
        self.is_open = True
        self._rx = bytearray()          # MCU → PC
        self._lock = threading.Lock()
        self._parser = FrameParser()
        self._seq = 0
        self.temp, self.humi, self.obj_temp, self.smoke = 25.0, 55.0, 30.0, 300
        self.relay_on, self.latched, self.cut_reason = True, False, 0
        self.alert_level = 0
        self.voices = []
        self.drop_acks = False
        self.last_hb = 0.0
        self._last_tel = 0.0
        # 外部温湿度控制
        self.env = {"mode": 0, "t_set": 26.0, "h_low": 40, "h_high": 70}
        self.fan_pwm, self.humid_on, self.manual_until = 0, False, 0.0

    # ---- pyserial 接口 ----
    def read(self, n):
        self._tick()
        time.sleep(0.01)
        with self._lock:
            out = bytes(self._rx[:n])
            del self._rx[:n]
        return out

    def write(self, data):
        for seq, cmd, payload in self._parser.feed(data):
            self._on_cmd(seq, cmd, payload)
        return len(data)

    def close(self):
        self.is_open = False

    # ---- MCU 行为 ----
    def _send(self, cmd, payload):
        self._seq = (self._seq + 1) & 0xFF
        with self._lock:
            self._rx.extend(build_frame(self._seq, cmd, payload))

    def _cut(self, reason, event_code):
        self.relay_on = False
        self.latched = True
        self.cut_reason = reason
        self._send(MSG_EVENT, struct.pack("<Bh", event_code, int(self.obj_temp * 10)))

    def _on_cmd(self, seq, cmd, payload):
        if cmd == CMD_HEARTBEAT:
            self.last_hb = time.time()
            self.alert_level = payload[0]
        elif cmd == CMD_POWER_OFF:
            if self.drop_acks:
                return
            if not self.latched:
                self._cut(4, 4)
            self._send(MSG_ACK, bytes([seq, cmd, 0]))
        elif cmd == CMD_PLAY_VOICE:
            self.voices.append(payload[0])
        elif cmd == CMD_SET_ENV and len(payload) >= 5:
            mode, t10, hl, hh = struct.unpack("<BhBB", payload[:5])
            self.env = {"mode": mode, "t_set": t10 / 10.0, "h_low": hl, "h_high": hh}
            if mode == 0:
                self.manual_until = 0.0
        elif cmd == CMD_SET_ACT and len(payload) >= 2:
            self.fan_pwm, self.humid_on = payload[0], bool(payload[1])
            self.manual_until = time.time() + 600

    def press_reset(self):
        hazard = self.obj_temp >= self.HARD_LIMIT_C
        if hazard:
            self._send(MSG_EVENT, struct.pack("<Bh", 6, 0))
            return
        self.latched = False
        self.relay_on = True
        self.cut_reason = 0
        self._send(MSG_EVENT, struct.pack("<Bh", 5, 0))

    def _tick(self):
        now = time.time()
        if self.obj_temp >= self.HARD_LIMIT_C and not self.latched:
            self._cut(1, 1)
        if now - self._last_tel >= 0.1:
            self._last_tel = now
            flags = (self.relay_on << 0) | (self.latched << 1) | ((now - self.last_hb < 5) << 2) \
                | (1 << 3) | (1 << 4) | (1 << 5)
            payload = struct.pack("<hHhHBB", int(self.temp * 10), int(self.humi * 10),
                                  int(self.obj_temp * 10), int(self.smoke), flags, self.cut_reason)
            self._send(MSG_TELEMETRY, payload)
            if time.time() >= self.manual_until:
                from src.core.env_control import control_law
                self.fan_pwm, self.humid_on, _ = control_law(
                    self.temp, self.humi, self.smoke, self.env["t_set"], self.env["h_low"],
                    self.env["h_high"], 1200, self.fan_pwm, self.humid_on)
            self._send(MSG_ENV_STATUS, bytes([self.fan_pwm, int(self.humid_on),
                                              1 if time.time() < self.manual_until else 0]))
