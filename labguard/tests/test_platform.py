"""
平台层离线测试：世界模型、环境控制律与新协议、事件历史、Agent 降级链、Web 总后台接口。
不需要摄像头、下位机或大模型。运行：
    python tests/test_platform.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "LabSafetyMonitor"), os.path.join(ROOT, "tests")):
    sys.path.insert(0, p)

from src import config as config_mod

config_mod.load()
_tmp = tempfile.mkdtemp(prefix="labsafety_platform_")
config_mod._override_path = lambda: os.path.join(_tmp, "override.yaml")
_cfg = config_mod._config
_cfg["events"]["db"] = os.path.join(_tmp, "events.db")
_cfg["events"]["snapshot_dir"] = os.path.join(_tmp, "snaps")
_cfg["notify"]["webhook_url"] = ""
_cfg["web"]["token"] = "TESTTOKEN"
_cfg["agent"]["remote"]["api_key_env"] = "LABSAFETY_TEST_NO_KEY"
_cfg["agent"]["local"]["base_url"] = "http://127.0.0.1:9"   # 不可达 → 触发降级
_cfg["demo"]["allow_ipc_telemetry"] = True

try:
    from PySide6.QtCore import QCoreApplication
except ImportError:
    from PyQt6.QtCore import QCoreApplication

app = QCoreApplication.instance() or QCoreApplication(sys.argv)
PASSED = []


def check(cond, name):
    if not cond:
        raise AssertionError(name)
    PASSED.append(name)
    print(f"  ✔ {name}")


# ---------------------------------------------------------------------------
print("\n[1] 热力学世界模型")
from src.core.world_model import ThermalWorldModel

wm = ThermalWorldModel()
t0 = 1000.0
# 一阶升温：T∞=180℃，τ=200s，从 60℃ 开始，真实到达 120℃ 的时间可解析求出
import math
T_inf, tau, T0 = 180.0, 400.0, 104.5   # 已越过沸点（液体已烧干）后的升温段
true_eta_from = lambda T: -tau * math.log((T_inf - 120.0) / (T_inf - T))
res = None
for k in range(0, 61):
    T = T_inf - (T_inf - T0) * math.exp(-k / tau)
    res = wm.update(T, now=t0 + k)
check(res["model"] == "linear", "越过沸点后只用最近 15 秒外推（不受之前数据干扰）")
true_eta = true_eta_from(res["temp"])
check(res["eta_s"] is not None and abs(res["eta_s"] - true_eta) / true_eta < 0.15,
      f"干烧 ETA 误差 < 15%（预测 {res['eta_s']:.0f}s，真实 {true_eta:.0f}s）")
check(res["phase"] == "heating" and res["risk"] > 0, "阶段=升温中，风险分 > 0")

wm_b = ThermalWorldModel()
for k in range(0, 40):
    rb = wm_b.update(40.0 + 1.2 * k, now=t0 + k)
check(rb["eta_s"] is None and rb["eta_boil_s"] is not None,
      "低于沸点时只预测沸腾时间，不误报干烧（液体仍在）")

wm_f = ThermalWorldModel()
for k in range(0, 61):
    rf = wm_f.update(90.0 - (90.0 - 30.0) * math.exp(-k / 40.0), now=t0 + k)
check(rf["model"] == "first_order" and rf["t_inf"] is not None and abs(rf["t_inf"] - 90.0) < 8,
      "沸点以下的升温段识别为一阶热平衡模型，估出渐近温度")

wm2 = ThermalWorldModel()
for k in range(0, 80):
    T = 70.0 - (70.0 - 30.0) * math.exp(-k / 15.0)   # 渐近 70℃ < 干烧阈值
    r2 = wm2.update(T, now=t0 + k)
check(r2["will_dry_burn"] is False, "渐近温度低于阈值 → 判定不会干烧")

wm3 = ThermalWorldModel()
for k in range(0, 40):
    r3 = wm3.update(99.0 + (0.1 if k % 2 else -0.1), now=t0 + k * 2)
check(r3["phase"] == "boiling" and r3["plateau_s"] > 30, "沸腾平台检测")
wm4 = ThermalWorldModel()
for k in range(0, 40):
    wm4.update(99.0, now=t0 + k)                       # 沸腾平台
for k in range(0, 14):
    r4 = wm4.update(99.0 + 0.8 * k, now=t0 + 40 + k)   # 烧干后继续升温
check(r4["eta_s"] is not None and abs(r4["eta_s"] - (120 - r4["temp"]) / 0.8) < 5,
      "沸腾平台之后越过沸点继续升温 → 立即给出干烧倒计时")
check(wm3.update(None)["phase"] == "no_sensor", "无热源测温时清空")

# ---------------------------------------------------------------------------
print("\n[2] 环境控制律")
from src.core.env_control import control_law

fan, hum, _ = control_law(25, 55, 300, 26, 40, 70, 1200)
check(fan == 0 and hum is False, "正常环境执行器待机")
fan, hum, _ = control_law(30, 55, 300, 26, 40, 70, 1200)
check(fan == 100 or 40 < fan <= 100, "高温 → 排风扇按偏差调速")
fan, hum, _ = control_law(25, 85, 300, 26, 40, 70, 1200)
check(fan > 0 and hum is False, "高湿 → 排风、不加湿")
fan, hum, _ = control_law(25, 30, 300, 26, 40, 70, 1200)
check(hum is True, "过干 → 加湿")
fan, hum, _ = control_law(25, 30, 2000, 26, 40, 70, 1200)
check(fan == 100 and hum is False, "烟雾 → 排风满速且禁止加湿")
fan, hum, _ = control_law(26.5, 55, 300, 26, 40, 70, 1200, prev_fan=60)
check(fan == 30, "回差区保持低速，避免频繁启停")

# ---------------------------------------------------------------------------
print("\n[3] 新协议帧：SET_ENV / SET_ACT / ENV_STATUS")
import serial_comm
from serial_comm import SerialComm
from fake_mcu import FakeMCU

mcu = FakeMCU()
link = SerialComm()
link.serial_conn = mcu
link.port = "FAKE"
link.running = True
th = threading.Thread(target=link._loop, daemon=True)
th.start()
time.sleep(0.4)
mcu.temp = 31.0
time.sleep(0.4)
d = link.get_sensor_data()
check(d["fan_pwm"] is not None and d["fan_pwm"] > 0, "下位机上报排风扇 PWM（本地闭环）")
link.set_actuator(55, True)
time.sleep(0.4)
check(mcu.fan_pwm == 55 and mcu.humid_on, "手动指令到达下位机")
link.set_env(0, 24.0, 35, 75)
time.sleep(0.4)
check(mcu.env["t_set"] == 24.0 and mcu.env["h_high"] == 75, "设定值到达下位机并恢复自动")
link.running = False
th.join(1)
SerialComm._shared = None

# ---------------------------------------------------------------------------
print("\n[4] 事件历史与监听")
from src.core.events import events

seen = []
events.add_listener(lambda rec: seen.append(rec["uid"]))
events.update("smoke", True, "CRITICAL", "烟雾 / 可燃气体超标", "测试")
ev = events.history(1)[0]
check(ev["title"] == "烟雾 / 可燃气体超标" and ev["uid"] in seen, "事件进入历史并通知监听者")
events.set_review(ev["uid"], True)
events.set_agent_note(ev["uid"], "测试研判")
e2 = events.get_event(ev["uid"])
check(e2["review"] == 1 and e2["agent_note"] == "测试研判", "复核结果与研判写回")
events.update("smoke", False)
check(events.get_event(ev["uid"])["ended"] is True, "事件解除后标记结束")
for on in (True, False, True, False, True):
    events.update("edge_risk", on, "NOTICE", "器皿靠近台面边缘", "测试")
check(sum(1 for e in events.history(50) if e["kind"] == "edge_risk") == 1,
      "识别结果时有时无：10 秒内反复出现只记一次，不刷屏")
events.update("edge_risk", False)

# ---------------------------------------------------------------------------
print("\n[5] 安全引擎 + Agent 降级链 + Web 接口")
from src.core.engine import SafetyEngine, attach_extensions
from src.agent.agent import TOOLS, SafetyAgent

engine = attach_extensions(SafetyEngine())
agent = engine.extensions["agent"]
names = {t["function"]["name"] for t in TOOLS}
check(not any(k in n for n in names for k in ("power", "relay", "reset", "unlock", "cut")),
      "Agent 工具表中没有任何电源 / 继电器 / 解锁类工具")
res = agent.ask("干烧了怎么办")
check(res["provider"] == "rules" and "手册" in res["text"] or "1.2" in res["text"],
      "远程无 Key、本地不可达 → 降级为规则模板，仍给出手册条款")


# 本地桩 OpenAI 服务：先让模型调用一次工具，再给出结论
class Stub(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.calls.append(body)
        has_tool_result = any(m.get("role") == "tool" for m in body["messages"])
        if body.get("tools") and not has_tool_result:
            msg = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "search_manual", "arguments": json.dumps({"query": "干烧"})}}]}
        else:
            msg = {"role": "assistant", "content": "原因：测试\n处置：断电\n依据：手册 1.2"}
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
_cfg["agent"]["local"]["base_url"] = f"http://127.0.0.1:{srv.server_port}/v1"
_cfg["agent"]["route"] = "local"
res = agent.ask("干烧了怎么办")
check(res["provider"] == "local" and res["tools"] == ["search_manual"], "本地线路：模型调用只读工具后作答")
check("1.2" in res["text"], "本地线路返回内容")

from fastapi.testclient import TestClient
from src.web.server import create_app

client = TestClient(create_app(engine))
H = {"X-Token": "TESTTOKEN"}
check(client.get("/api/snapshot").status_code == 401, "无口令访问被拒绝")
snap = client.get("/api/snapshot", headers=H).json()
check(snap["state"] in ("NORMAL", "WARNING", "CRITICAL") and "world_model" in snap and "env" in snap,
      "快照包含状态、世界模型、环境控制")
r = client.patch("/api/config", headers=H, json={"rules.dry_burn_c": 999})
check(r.status_code == 422, "越界参数被拒绝")
r = client.patch("/api/config", headers=H, json={"serial.port": "/dev/evil"})
check(r.status_code == 422, "白名单外参数被拒绝")
r = client.patch("/api/config", headers=H, json={"rules.dry_burn_c": 130})
check(r.status_code == 200 and config_mod.cfg("rules.dry_burn_c") == 130, "合法参数生效")
routes = {getattr(rt, "path", "") for rt in client.app.routes}
check(not any(k in p for p in routes for k in ("power/on", "power/restore", "unlock", "reset", "release")),
      "不存在任何上电 / 解锁接口")
r = client.post("/api/power/cut", headers=H, json={})
check(r.status_code == 400, "断电需要显式确认")
r = client.post("/api/power/cut", headers=H, json={"confirm": True})
check(r.status_code == 200 and engine.state_machine.latched, "总后台断电 → 软件锁定")
r = client.post("/api/demo/start", headers=H, json={"scenario": "humid"})
check(r.status_code == 200 and r.json()["scenario"] == "humid", "无下位机时允许演示注入")
client.post("/api/demo/stop", headers=H)


class AliveLink:
    is_alive = True


engine.serial, saved = AliveLink(), engine.serial
r = client.post("/api/demo/start", headers=H, json={"scenario": "smoke"})
check(r.status_code == 409, "接入下位机后拒绝演示注入")
engine.serial = saved
r = client.post("/api/env/setpoint", headers=H, json={"t_set": 50, "h_low": 40, "h_high": 70})
check(r.status_code == 422, "环境设定值越界被拒绝")
r = client.post("/api/agent/ask", headers=H, json={"question": "现在安全吗"})
check(r.status_code == 200 and r.json()["text"], "总后台问答接口")
r = client.post("/api/jepa/score", headers=H, json={"score": 0.42, "model": "vjepa2"})
check(r.status_code == 200 and engine.extensions["jepa"].status()["online"], "视觉世界模型分数回写")

srv.shutdown()
print(f"\n全部通过：{len(PASSED)} 项")
