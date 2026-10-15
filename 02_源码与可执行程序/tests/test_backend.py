"""
后端离线测试：协议、串口链路、状态机、视觉时序逻辑。
不需要摄像头、模型或真实下位机。运行：
    python tests/test_backend.py
"""
import os
import sys
import time
import struct
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "LabSafetyMonitor"), os.path.join(ROOT, "tests")):
    sys.path.insert(0, p)

from src import config as config_mod

config_mod.load()
_cfg = config_mod._config
_tmp = tempfile.mkdtemp(prefix="labsafety_test_")
_cfg["events"]["db"] = os.path.join(_tmp, "events.db")
_cfg["events"]["snapshot_dir"] = os.path.join(_tmp, "snaps")
_cfg["rules"]["unattended_heating_cut_s"] = 1.0
_cfg["rules"]["sim_latch_release_s"] = 0.5
_cfg["rules"]["item_hold_s"] = 0.0   # 测试里器皿逐步切换，不需要防抖
_cfg["events"]["retrigger_s"] = 0.0   # 测试里事件快速开关，不需要防抖
_cfg["presence"]["absent_seconds"] = 0.5
_cfg["presence"]["recover_seconds"] = 0.2
_cfg["voting"]["window_s"] = 0.5
_cfg["voting"]["min_frames"] = 3
_cfg["sleep"]["still_seconds"] = 0.5
_cfg["notify"]["webhook_url"] = ""

try:
    from PySide6.QtCore import QCoreApplication
except ImportError:
    from PyQt6.QtCore import QCoreApplication

import numpy as np
import serial_comm
from serial_comm import SerialComm, build_frame, FrameParser, crc16_ccitt, MSG_TELEMETRY
from fake_mcu import FakeMCU
from src.core.telemetry import telemetry
from src.core.events import events
from src.core.state_machine import SafetyStateMachine

app = QCoreApplication.instance() or QCoreApplication(sys.argv)
PASSED = []


def check(cond, name):
    if not cond:
        raise AssertionError(name)
    PASSED.append(name)
    print(f"  ✔ {name}")


def wait_until(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
print("[1] 帧协议")
check(crc16_ccitt(b"123456789") == 0x29B1, "CRC16-CCITT 标准校验值 0x29B1")
f1 = build_frame(1, MSG_TELEMETRY, struct.pack("<hHhHBB", 251, 553, 300, 400, 0x3F, 0))
f2 = build_frame(2, 0x11, bytes([7, 2, 0]))
bad = bytearray(build_frame(3, 0x11, bytes([1, 2, 0])))
bad[-1] ^= 0xFF
stream = b"\x00\xAA\x13garbage" + f1 + bytes(bad) + f2
p = FrameParser()
got = []
for i in range(0, len(stream), 3):      # 模拟串口分片到达
    got += p.feed(stream[i:i + 3])
check([g[1] for g in got] == [MSG_TELEMETRY, 0x11], "噪声 + 分片 + CRC 错误帧后仍能正确解出两帧")
check(p.crc_errors == 1, "CRC 错误帧被丢弃并计数")

# ---------------------------------------------------------------------------
print("[2] 演示模式（无下位机）：兜底规则 + 自动解锁")
SerialComm._shared = None
sm = SafetyStateMachine(serial_controller=None)
alerts = []
sm.alert_triggered.connect(lambda d: alerts.append(d))
telemetry.update("ipc", temp=68.0, humi=35.0, obj_temp=None, smoke=None)
state, _ = sm.evaluate(["Beaker"], True, 68.0, 35.0)
check(state == "CRITICAL" and sm.latched, "器皿 + 无人值守 + 环境 68℃ → CRITICAL 并锁定")
check(alerts and "演示模式" in alerts[-1]["message"], "演示模式下如实说明是软件模拟断电")
check(events.is_active("power_latched"), "事件中心记录断电锁定")
telemetry.update("ipc", temp=25.0, humi=55.0, obj_temp=None, smoke=None)
state, _ = sm.evaluate(["Beaker"], True, 25.0, 55.0)
check(state == "CRITICAL", "危险刚解除时仍保持锁定")
time.sleep(0.6)
sm.evaluate(["Beaker"], True, 25.0, 55.0)
state, _ = sm.evaluate(["Beaker"], True, 25.0, 55.0)
check(state == "NORMAL" and not sm.latched, "危险解除 0.5 秒后自动解锁")
state, _ = sm.evaluate(["Volumetric flask"], True, 68.0, 35.0)
check(state == "WARNING" and not sm.latched, "容量瓶不算可加热器皿，不触发兜底断电（只有环境高温告警）")

# ---------------------------------------------------------------------------
print("[3] 串口未连接时，断电如实报告失败")
link_down = SerialComm(port="COM_NOT_EXIST")
SerialComm._shared = link_down
link_down.port_setting = "COM_NOT_EXIST"
check(link_down.send_power_off() is False and link_down.power_off_state == "failed_no_link",
      "send_power_off() 返回 False，不再假装成功")

# ---------------------------------------------------------------------------
print("[4] 仿真下位机：干烧断电 → ACK → 锁定 → 现场复位")
SerialComm._shared = None
mcu = FakeMCU()
link = SerialComm(port="FAKE")


def fake_open(self=link):
    self.serial_conn = mcu
    self.port = "FAKE"
    self._notify("link_up", {"port": "FAKE"})
    return True


link._try_open = fake_open
link.connect()
check(wait_until(lambda: link.is_alive), "链路建立，收到遥测")
check(wait_until(lambda: mcu.last_hb > 0), "下位机收到心跳")

sm = SafetyStateMachine(serial_controller=link)
alerts.clear()
sm.alert_triggered.connect(lambda d: alerts.append(d))


def push_hw():
    d = link.get_sensor_data()
    telemetry.update("hardware", temp=d["temp"], humi=d["humi"], obj_temp=d["obj_temp"], smoke=d["smoke"])
    return d


mcu.obj_temp = 30.0
time.sleep(0.2)
push_hw()
state, _ = sm.evaluate(["Beaker"], False, 25.0, 55.0)
check(state == "NORMAL", "热源 30℃ 正常")

mcu.obj_temp = 130.0
wait_until(lambda: link.get_sensor_data()["obj_temp"] == 130.0)
push_hw()
state, _ = sm.evaluate(["Beaker"], False, 25.0, 55.0)
check(state == "CRITICAL" and sm.latched, "热源 130℃ ≥ 120℃ → 干烧 CRITICAL")
check(alerts[-1]["event_type"] == "HIGH_RISK_DRY_BURN" and "等待下位机确认" in alerts[-1]["message"],
      "告警说明'已下发，等待确认'，而不是直接宣称成功")
check(wait_until(lambda: link.power_off_state == "acked"), "下位机 ACK 确认断电")
check(wait_until(lambda: link.get_sensor_data()["latched"]), "遥测显示下位机已锁定、继电器断开")
check(wait_until(lambda: mcu.alert_level == 2), "心跳携带红灯等级")

mcu.obj_temp = 30.0
wait_until(lambda: link.get_sensor_data()["obj_temp"] == 30.0)
push_hw()
state, _ = sm.evaluate(["Beaker"], False, 25.0, 55.0)
check(state == "CRITICAL", "温度恢复后仍保持锁定（等待现场复位）")

mcu.press_reset()
wait_until(lambda: not link.get_sensor_data()["latched"])
time.sleep(0.15)
push_hw()
sm.evaluate(["Beaker"], False, 25.0, 55.0)
state, _ = sm.evaluate(["Beaker"], False, 25.0, 55.0)
check(state == "NORMAL" and not sm.latched, "现场按复位键后解除锁定")
check(wait_until(lambda: mcu.alert_level == 0), "解除后恢复绿灯")

# ---------------------------------------------------------------------------
print("[5] 加热中无人值守：先警告，超时后断电")
mcu.obj_temp = 70.0
wait_until(lambda: link.get_sensor_data()["obj_temp"] == 70.0)
push_hw()
state, _ = sm.evaluate(["Beaker"], True, 25.0, 55.0)
check(state == "WARNING" and not sm.latched, "加热 70℃ + 无人 → WARNING（倒计时）")
time.sleep(1.1)
push_hw()
state, _ = sm.evaluate(["Beaker"], True, 25.0, 55.0)
check(state == "CRITICAL" and sm.latched, "持续超过 cut_s → 断电")
mcu.obj_temp = 30.0
mcu.press_reset()
wait_until(lambda: not link.get_sensor_data()["latched"])
time.sleep(0.15)
push_hw()
sm.evaluate([], False, 25.0, 55.0)

# ---------------------------------------------------------------------------
print("[6] 下位机本地超温自主断电 → 上位机同步锁定")
mcu.obj_temp = 160.0
check(wait_until(lambda: link.get_sensor_data()["latched"]), "下位机本地规则断电")
push_hw()
state, _ = sm.evaluate([], False, 25.0, 55.0)
check(state == "CRITICAL" and sm.latched, "上位机同步为锁定状态")
mcu.obj_temp = 30.0
mcu.press_reset()
wait_until(lambda: not link.get_sensor_data()["latched"])
time.sleep(0.15)
push_hw()
sm.evaluate([], False, 25.0, 55.0)
check(not sm.latched, "复位后解除")

# ---------------------------------------------------------------------------
print("[7] 下位机不回 ACK：断电失败如实上报，并持续重发")
mcu.drop_acks = True
mcu.obj_temp = 130.0
wait_until(lambda: link.get_sensor_data()["obj_temp"] == 130.0)
push_hw()
sm.evaluate([], False, 25.0, 55.0)
check(wait_until(lambda: link.power_off_state == "failed_no_ack", 2.0), "3 次未收到 ACK → failed_no_ack")
check(any("断电指令未确认" in line for line in events.recent_lines(20)), "事件日志提示人工断电")
mcu.drop_acks = False
link.disconnect()
check(not link.is_open, "主动断开")

# ---------------------------------------------------------------------------
print("[8] 视觉时序逻辑（假模型）")
import dual_inference as di


class Box:
    def __init__(self, cls, conf, xyxy):
        self.cls, self.conf, self.xyxy = [cls], [conf], [xyxy]


class Res:
    def __init__(self, boxes):
        self.boxes = boxes


class FakeModel:
    def __init__(self, names):
        self.names = names
        self.boxes = []

    def __call__(self, *a, **k):
        return [Res(list(self.boxes))]


SerialComm._shared = None
sysd = di.DualInferenceSystem()
ppe_names = {0: "Drinking", 1: "Eating", 2: "Gloves", 3: "Googles", 4: "Head Mask", 5: "Lab Coat",
             6: "Mask", 7: "No Gloves", 8: "No Head Mask", 9: "No Lab coat", 10: "No Mask", 11: "No googles"}
chem_names = {0: "Beaker", 12: "Volumetric flask"}
sysd.ppe_model, sysd.ppe_class_names = FakeModel(ppe_names), ppe_names
sysd.chem_model, sysd.chem_class_names = FakeModel(chem_names), chem_names
sysd.vision_ok = True
f = np.zeros((480, 640, 3), np.uint8)
meta = {"pano_valid": True, "desk_valid": True, "simulated": False}


def step(n=1, dt=0.05):
    out = None
    for _ in range(n):
        sysd._process(f, f, meta, time.time(), 20.0)
        out = sysd.get_results()
        time.sleep(dt)
    return out


sysd.ppe_model.boxes = [Box(5, 0.9, (100, 100, 300, 400)), Box(3, 0.8, (180, 110, 230, 140))]
r = step(3)
check(r[2]["unattended"] is False, "只检测到实验服 + 护目镜（没有口罩）也算'有人'")
sysd.ppe_model.boxes = []
step(1)
r = step(12)
check(r[2]["unattended"] is True, "连续 absent_seconds 无人员装备 → 无人值守")
sysd.ppe_model.boxes = [Box(5, 0.9, (100, 100, 300, 400))]
r = step(6)
check(r[2]["unattended"] is False, "人员回来 recover_seconds 后解除")

sysd.ppe_model.boxes = [Box(5, 0.9, (100, 100, 300, 400)), Box(11, 0.7, (180, 110, 230, 140))]
r = step(2)
check(not r[2]["ppe_violation"], "单帧/少量帧的'未戴护目镜'不立即报警（投票防抖）")
r = step(10)
check(r[2]["ppe_violation"] and "未佩戴护目镜" in r[3], "持续出现 → 确认违规")
check(events.is_active("ppe_goggles"), "事件中心记录违规")
sysd.ppe_model.boxes = [Box(5, 0.9, (100, 100, 300, 400)), Box(3, 0.8, (180, 110, 230, 140))]
r = step(14)
check(not r[2]["ppe_violation"] and not events.is_active("ppe_goggles"), "纠正后自动解除")

sysd.chem_model.boxes = [Box(0, 0.9, (300, 300, 360, 470))]       # 底部 y=470 → 0.979，越过桌边线 0.92
r = step(10)
check(r[2]["falling_warning"], "器皿越过/靠近桌边线 → 坠落风险")
sysd.chem_model.boxes = [Box(0, 0.9, (300, 100, 360, 200))]
r = step(14)
check(not r[2]["falling_warning"] and r[2]["chem_items"] == ["Beaker"], "器皿在台面内侧 → 无风险")

sysd.ppe_model.boxes = [Box(5, 0.9, (100, 200, 300, 470)), Box(6, 0.8, (180, 380, 230, 420))]
r = step(14)
check(r[2]["sleeping"], "面部装备持续偏低且静止 → 疑似伏案")

sysd.enable_ppe = False
sysd.ppe_model.boxes = []
r = step(15)
check(r[2]["unattended"] is False, "关闭人员模型时不判定无人值守（旧版会误判）")
r = step(1)
check("====== 最近事件 ======" in r[6], "日志文本包含最近事件，不会被每帧覆盖丢失")

print(f"\n全部通过：{len(PASSED)} 项")
