"""
上位机 ⇄ 下位机（ESP32-S3 / STM32）串口通信模块
LabSafetyMonitor/serial_comm.py

设计要点：
1. 整个系统只有这一个模块打开串口（温湿度也从这里取），避免多个模块争抢同一端口。
2. 端口自动识别：config.yaml 指定 > /dev/labguard（udev 软链接）> 常见 USB 串口芯片 VID。
3. 帧协议带包头同步 + CRC16，丢字节后能在下一个包头处自动重新对齐。
4. 断电指令需要下位机 ACK，超时自动重发 3 次；失败如实上报，绝不"假装成功"。
5. 每秒发送心跳（携带当前告警等级），下位机据此驱动三色灯，并在失联时进入兜底保护。
6. 拔线 / 重启后自动重连，不需要重启上位机程序。

帧格式（小端）：
    AA 55 | LEN | SEQ | CMD | PAYLOAD(LEN 字节) | CRC16_L CRC16_H
    CRC16-CCITT(多项式 0x1021, 初值 0xFFFF)，计算范围：LEN、SEQ、CMD、PAYLOAD

上位机 → 下位机：
    0x01 HEARTBEAT   payload: [alert_level u8]   0=绿 1=黄 2=红
    0x02 POWER_OFF   payload: [reason u8]        需要 ACK
    0x03 PLAY_VOICE  payload: [track u8]         DFPlayer 曲目号
下位机 → 上位机：
    0x10 TELEMETRY   payload: <h temp*10, H humi*10, h obj_temp*10, H smoke_raw, B flags, B cut_reason>
                     无效值：temp/obj_temp = 0x7FFF，humi/smoke = 0xFFFF
                     flags: bit0 继电器闭合  bit1 断电锁定  bit2 心跳正常  bit3 SHT30 正常
                            bit4 MLX90614 正常  bit5 MQ-2 已预热  bit6 本地告警中
    0x11 ACK         payload: <B seq, B cmd, B result>
    0x12 EVENT       payload: <B code, h value>

注意：协议规定"上位机只能断电、不能上电"。恢复供电必须由人在现场按下控制盒上的复位键。
"""
import os
import sys
import time
import queue
import struct
import threading
from typing import Callable, Dict, List, Optional

import serial

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover
    list_ports = None

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
try:
    from src.config import cfg
except Exception:  # pragma: no cover
    def cfg(_path, default=None):
        return default

HEADER = b"\xAA\x55"

CMD_HEARTBEAT = 0x01
CMD_POWER_OFF = 0x02
CMD_PLAY_VOICE = 0x03
CMD_SET_ENV = 0x04      # [mode(0 自动/1 手动), t_set×10 (int16), h_low, h_high]
CMD_SET_ACT = 0x05      # [fan_pwm 0-100, humid 0/1]，手动指令，下位机 10 分钟后自动回到自动模式
MSG_TELEMETRY = 0x10
MSG_ACK = 0x11
MSG_EVENT = 0x12
MSG_ENV_STATUS = 0x13   # [fan_pwm, humid, mode]

INVALID_I16 = 0x7FFF
INVALID_U16 = 0xFFFF

# 断电原因码（POWER_OFF 的 payload，以及遥测里的 cut_reason）
CUT_REASONS = {
    0: "无",
    1: "热源超温（下位机本地规则）",
    2: "烟雾超标（下位机本地规则）",
    3: "上位机失联兜底（下位机看门狗）",
    4: "上位机视觉 + 遥测联合决策",
}
# 下位机主动上报的事件码
EVENT_CODES = {
    1: "下位机本地断电：热源超温",
    2: "下位机本地断电：烟雾超标",
    3: "下位机看门狗断电：上位机失联且仍在加热",
    4: "下位机已执行上位机断电指令",
    5: "现场复位键：已解除锁定并恢复供电",
    6: "现场复位被拒绝：危险尚未解除",
    7: "下位机启动（继电器默认断开）",
}

# 常见 USB 串口芯片 VID
KNOWN_VIDS = {
    0x303A: "Espressif 原生 USB",
    0x1A86: "WCH CH340/CH343",
    0x10C4: "Silicon Labs CP210x",
    0x0483: "STM32 虚拟串口",
    0x0403: "FTDI",
}

ACK_TIMEOUT_S = 0.25
ACK_MAX_TRIES = 3
TELEMETRY_STALE_S = 3.0


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_frame(seq: int, cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([len(payload), seq & 0xFF, cmd]) + payload
    return HEADER + body + struct.pack("<H", crc16_ccitt(body))


class FrameParser:
    """流式解帧：按包头重新同步，长度非法或 CRC 错误时丢弃并继续寻找下一个包头"""

    MAX_LEN = 64

    def __init__(self):
        self.buf = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes) -> List[tuple]:
        self.buf.extend(data)
        frames = []
        while True:
            idx = self.buf.find(HEADER)
            if idx < 0:
                # 末尾可能是半个包头 0xAA，保留它
                if self.buf[-1:] == b"\xAA":
                    del self.buf[:-1]
                else:
                    self.buf.clear()
                break
            if idx > 0:
                del self.buf[:idx]
            if len(self.buf) < 5:
                break
            length = self.buf[2]
            if length > self.MAX_LEN:
                del self.buf[:2]
                continue
            total = 5 + length + 2
            if len(self.buf) < total:
                break
            body = bytes(self.buf[2:5 + length])
            (crc,) = struct.unpack_from("<H", self.buf, 5 + length)
            if crc16_ccitt(body) == crc:
                frames.append((body[1], body[2], body[3:]))
                del self.buf[:total]
            else:
                self.crc_errors += 1
                del self.buf[:2]
        return frames


class SerialComm:
    """
    硬件交互多模态联动模块（单例共享）。
    保持旧接口：connect() / send_power_off() / get_sensor_data() / disconnect()
    """

    _shared: Optional["SerialComm"] = None
    _shared_lock = threading.Lock()

    @classmethod
    def get_shared(cls) -> Optional["SerialComm"]:
        return cls._shared

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 115200):
        cfg_port = cfg("serial.port", "auto")
        self.requested_port = port
        self.port_setting = cfg_port if cfg_port and str(cfg_port).lower() != "auto" else "auto"
        self.port: Optional[str] = None
        self.baudrate = int(cfg("serial.baudrate", baudrate) or baudrate)
        self.heartbeat_s = float(cfg("serial.heartbeat_s", 1.0) or 1.0)

        self.serial_conn: Optional[serial.Serial] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._parser = FrameParser()
        self._cmd_q: "queue.Queue[tuple]" = queue.Queue()
        self._pending: Dict[int, dict] = {}
        self._seq = 0
        self._listeners: List[Callable[[str, dict], None]] = []
        self._data_lock = threading.Lock()

        self.alert_level = 0
        self._hb_dirty = True
        self.power_off_state = "idle"   # idle / pending / acked / failed_no_link / failed_no_ack
        self.last_error = ""

        # 兼容旧字段名 temp / hum / gas
        self.sensor_data = {
            "temp": None, "hum": None, "humi": None, "obj_temp": None, "smoke": None, "gas": None,
            "relay_on": False, "latched": False, "pc_link_ok": False, "sht_ok": False,
            "mlx_ok": False, "smoke_ready": False, "local_alarm": False, "cut_reason": 0, "ts": 0.0,
            "fan_pwm": None, "humid_on": None, "env_mode": None,
        }

        with SerialComm._shared_lock:
            if SerialComm._shared is None:
                SerialComm._shared = self

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.serial_conn is not None and self.serial_conn.is_open

    @property
    def is_alive(self) -> bool:
        """端口已打开，且最近 3 秒内收到过下位机遥测"""
        with self._data_lock:
            ts = self.sensor_data["ts"]
        return self.is_open and (time.time() - ts) < TELEMETRY_STALE_S

    def status_text(self) -> str:
        if self.is_alive:
            return f"下位机在线 ({self.port})"
        if self.is_open:
            return f"串口已打开 ({self.port})，等待下位机数据"
        return "下位机未连接" + (f"：{self.last_error}" if self.last_error else "")

    def get_sensor_data(self) -> dict:
        with self._data_lock:
            return dict(self.sensor_data)

    def add_listener(self, fn: Callable[[str, dict], None]):
        """注册事件回调 fn(kind, info)。回调在串口线程中执行，必须线程安全。
        kind: telemetry / ack / power_off_acked / power_off_failed / mcu_event / link_up / link_lost"""
        self._listeners.append(fn)

    def _notify(self, kind: str, info: dict):
        for fn in list(self._listeners):
            try:
                fn(kind, info)
            except Exception as e:
                print(f"[Serial] 回调异常: {e}")

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def _resolve_port(self) -> Optional[str]:
        if self.port_setting != "auto":
            return self.port_setting
        if os.path.exists("/dev/labguard"):
            return "/dev/labguard"
        if list_ports is None:
            return self.requested_port
        ports = list(list_ports.comports())
        names = [p.device for p in ports]
        known = [p.device for p in ports if p.vid in KNOWN_VIDS]
        if self.requested_port in known:
            return self.requested_port
        if known:
            return known[0]
        if self.requested_port in names:
            return self.requested_port
        return None

    def _try_open(self) -> bool:
        port = self._resolve_port()
        if not port:
            self.last_error = "未找到可用串口设备"
            return False
        try:
            s = serial.Serial()
            s.port = port
            s.baudrate = self.baudrate
            s.timeout = 0.05
            s.write_timeout = 0.5
            s.dtr = False   # 避免带自动下载电路的开发板在打开串口时被复位
            s.rts = False
            s.open()
            self.serial_conn = s
            self.port = port
            self.last_error = ""
            self._parser = FrameParser()
            self._hb_dirty = True
            print(f"[Serial] 已打开 {port} @ {self.baudrate}")
            self._notify("link_up", {"port": port})
            return True
        except Exception as e:
            self.last_error = f"{port}: {e}"
            self.serial_conn = None
            return False

    def _close(self, reason: str = ""):
        was_open = self.serial_conn is not None
        if self.serial_conn is not None:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.serial_conn = None
        # 重连后必须重新收到遥测，不能把上一次连接的数据当作新下位机的状态。
        with self._data_lock:
            self.sensor_data.update({"ts": 0.0, "fan_pwm": None, "humid_on": None,
                                     "env_mode": None})
        if self._pending:
            for item in self._pending.values():
                if item["cmd"] == CMD_POWER_OFF:
                    self.power_off_state = "failed_no_ack"
                    self._notify("power_off_failed", {"reason": "串口断开"})
            self._pending.clear()
        if was_open:
            self.last_error = reason
            print(f"[Serial] 连接断开: {reason}")
            self._notify("link_lost", {"reason": reason})

    def connect(self) -> bool:
        """打开串口并启动后台收发线程；首次打开失败也会在后台每 2 秒重试"""
        if not self.running:
            self.running = True
            self._try_open()
            self._thread = threading.Thread(target=self._loop, name="SerialComm", daemon=True)
            self._thread.start()
        return self.is_open

    def disconnect(self):
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._close("主动关闭")

    # ------------------------------------------------------------------
    # 下行指令（任何线程都可以调用，实际写串口只在后台线程中进行）
    # ------------------------------------------------------------------
    def send_power_off(self, reason: int = 4) -> bool:
        """
        向下位机发送断电指令。
        返回 True 表示指令已进入发送队列（结果以 ACK 为准，见 power_off_state）；
        返回 False 表示串口未连接，指令无法下发。
        """
        if not self.is_open:
            self.power_off_state = "failed_no_link"
            print("[Serial] 断电指令无法下发：串口未连接")
            return False
        self.power_off_state = "pending"
        self._cmd_q.put((CMD_POWER_OFF, bytes([reason & 0xFF]), True))
        return True

    def play_voice(self, track: int) -> bool:
        if not self.is_open or not track:
            return False
        self._cmd_q.put((CMD_PLAY_VOICE, bytes([int(track) & 0xFF]), False))
        return True

    def set_env(self, mode: int, t_set: float, h_low: float, h_high: float) -> bool:
        """下发外部温湿度控制设定值（控制律在下位机本地执行）"""
        if not self.is_open:
            return False
        payload = struct.pack("<BhBB", int(mode) & 0xFF, int(round(t_set * 10)),
                              max(0, min(100, int(h_low))), max(0, min(100, int(h_high))))
        self._cmd_q.put((CMD_SET_ENV, payload, False))
        return True

    def set_actuator(self, fan_pwm: int, humid: bool) -> bool:
        """手动控制排风扇与加湿片（不涉及加热电源）"""
        if not self.is_open:
            return False
        self._cmd_q.put((CMD_SET_ACT, bytes([max(0, min(100, int(fan_pwm))), 1 if humid else 0]), False))
        return True

    def set_alert_level(self, level: int):
        level = max(0, min(2, int(level)))
        if level != self.alert_level:
            self.alert_level = level
            self._hb_dirty = True

    def _calculate_checksum(self, data_bytes: bytes) -> int:
        """保留旧接口；新协议使用 CRC16"""
        return crc16_ccitt(data_bytes)

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def _write(self, frame: bytes):
        self.serial_conn.write(frame)

    def _loop(self):
        last_try = 0.0
        last_hb = 0.0
        while self.running:
            if self.serial_conn is None:
                now = time.time()
                if now - last_try >= 2.0:
                    last_try = now
                    self._try_open()
                time.sleep(0.1)
                continue
            try:
                data = self.serial_conn.read(256)
                if data:
                    for seq, cmd, payload in self._parser.feed(data):
                        self._handle(seq, cmd, payload)

                now = time.time()
                if self._hb_dirty or now - last_hb >= self.heartbeat_s:
                    self._hb_dirty = False
                    last_hb = now
                    self._write(build_frame(self._next_seq(), CMD_HEARTBEAT, bytes([self.alert_level])))

                while True:
                    try:
                        cmd, payload, need_ack = self._cmd_q.get_nowait()
                    except queue.Empty:
                        break
                    seq = self._next_seq()
                    frame = build_frame(seq, cmd, payload)
                    self._write(frame)
                    if need_ack:
                        self._pending[seq] = {"cmd": cmd, "frame": frame, "tries": 1, "sent": now}

                for seq in list(self._pending.keys()):
                    item = self._pending[seq]
                    if now - item["sent"] < ACK_TIMEOUT_S:
                        continue
                    if item["tries"] >= ACK_MAX_TRIES:
                        del self._pending[seq]
                        if item["cmd"] == CMD_POWER_OFF:
                            self.power_off_state = "failed_no_ack"
                            print("[Serial] 断电指令连续 3 次未收到 ACK")
                            self._notify("power_off_failed", {"reason": "下位机无应答"})
                        continue
                    item["tries"] += 1
                    item["sent"] = now
                    self._write(item["frame"])
            except (serial.SerialException, OSError) as e:
                self._close(f"通信异常: {e}")
            except Exception as e:
                print(f"[Serial] 未预期异常: {e}")
                time.sleep(0.1)

    def _handle(self, seq: int, cmd: int, payload: bytes):
        if cmd == MSG_TELEMETRY and len(payload) >= 10:
            t, h, ot, smoke, flags, cut = struct.unpack_from("<hHhHBB", payload)
            temp = None if t == INVALID_I16 else t / 10.0
            humi = None if h == INVALID_U16 else h / 10.0
            obj = None if ot == INVALID_I16 else ot / 10.0
            smoke_ready = bool(flags & 0x20)
            smk = None if (smoke == INVALID_U16 or not smoke_ready) else int(smoke)
            with self._data_lock:
                self.sensor_data.update({
                    "temp": temp, "hum": humi, "humi": humi, "obj_temp": obj,
                    "smoke": smk, "gas": smk,
                    "relay_on": bool(flags & 0x01), "latched": bool(flags & 0x02),
                    "pc_link_ok": bool(flags & 0x04), "sht_ok": bool(flags & 0x08),
                    "mlx_ok": bool(flags & 0x10), "smoke_ready": smoke_ready,
                    "local_alarm": bool(flags & 0x40), "cut_reason": cut, "ts": time.time(),
                })
                snap = dict(self.sensor_data)
            self._notify("telemetry", snap)
        elif cmd == MSG_ACK and len(payload) >= 3:
            ack_seq, ack_cmd, result = payload[0], payload[1], payload[2]
            item = self._pending.get(ack_seq)
            # SEQ 会循环使用；其他指令的确认不能误报为已完成断电。
            if item is not None and item["cmd"] == ack_cmd:
                del self._pending[ack_seq]
            else:
                item = None
            self._notify("ack", {"seq": ack_seq, "cmd": ack_cmd, "result": result})
            if item is not None and item["cmd"] == CMD_POWER_OFF:
                if result == 0:
                    self.power_off_state = "acked"
                    print("[Serial] 下位机已确认断电")
                    self._notify("power_off_acked", {})
                else:
                    self.power_off_state = "failed_no_ack"
                    self._notify("power_off_failed", {"reason": f"下位机返回错误码 {result}"})
        elif cmd == MSG_ENV_STATUS and len(payload) >= 3:
            with self._data_lock:
                self.sensor_data.update({"fan_pwm": int(payload[0]), "humid_on": bool(payload[1]),
                                         "env_mode": int(payload[2])})
        elif cmd == MSG_EVENT and len(payload) >= 3:
            code, value = struct.unpack_from("<Bh", payload)
            self._notify("mcu_event", {"code": code, "value": value,
                                       "desc": EVENT_CODES.get(code, f"未知事件 {code}")})
