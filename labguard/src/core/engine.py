"""
安全核心引擎（无界面）
src/core/engine.py

把原来写在主窗口里的"推理结果 → 状态机研判"编排逻辑搬到这里，使安全逻辑不再依赖任何界面：
  · 小屏 Kiosk、PC 大窗口、Web 总后台都只是"视图"，订阅引擎的信号或读取 snapshot()；
  · 界面关闭 / 崩溃 / 没有屏幕（headless）时，安全判断与断电闭环照常运行。

线程模型：
  · InferenceThread（QThread）拉取双路推理结果 → frames_ready 信号回到引擎所在线程；
  · SensorWorker（QThread）采集遥测 → 引擎研判；
  · Web 线程只调用 snapshot() / latest_jpeg() / 控制方法，全部加锁、不碰 Qt 对象。
"""
import time
import threading
from typing import Optional, Dict, Any, List

import numpy as np

try:
    from PySide6.QtCore import QObject, QThread, Signal, QTimer
except ImportError:
    from PyQt6.QtCore import QObject, QThread, pyqtSignal as Signal, QTimer

from src.config import cfg
from src.core.events import events, LEVEL_RANK
from src.core.telemetry import telemetry
from src.core.state_machine import SafetyStateMachine
from src.core.world_model import PHASE_TEXT
from src.hardware.sensor_worker import SensorWorker
from dual_inference import DualInferenceSystem
from serial_comm import SerialComm

STATE_TEXT = {"NORMAL": "正常守护", "WARNING": "警告", "CRITICAL": "紧急"}
PPE_ITEMS = [("ppe_goggles", "护目镜"), ("ppe_coat", "实验服"), ("ppe_gloves", "手套"), ("ppe_mask", "口罩")]


class InferenceThread(QThread):
    """拉取双路推理结果的线程（原位于主窗口，接口不变）"""
    update_frames_signal = Signal(np.ndarray, np.ndarray, dict, list, int, int, str)

    def __init__(self):
        super().__init__()
        self.running = True
        self.dual_system = DualInferenceSystem(src1=0, src2=0)   # 0 = 按 config.yaml 的机位
        # 串口全局单例：状态机、事件中心、遥测线程共用同一条链路
        self.serial = SerialComm.get_shared() or SerialComm()

    def run(self):
        self.dual_system.start()
        self.serial.connect()
        while self.running:
            res = self.dual_system.get_results()
            if res is not None:
                self.update_frames_signal.emit(*res)
            self.msleep(30)

    def stop(self):
        self.running = False
        self.dual_system.stop()
        self.serial.disconnect()
        self.wait(1500)


class SafetyEngine(QObject):
    # pano, desk, alarms, violations, curr_f, tot_f, log_str, state
    frames_ready = Signal(object, object, dict, list, int, int, str, str)
    # 遥测更新（参数为综合状态）
    telemetry_changed = Signal(str)
    # 遥测来源切换：是否模拟、说明
    sensor_mode_changed = Signal(bool, str)
    # 状态机推送的结构化告警
    alert = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started_at = time.time()
        self.inference_thread = InferenceThread()
        self.serial = self.inference_thread.serial
        self.dual_system = self.inference_thread.dual_system
        self.state_machine = SafetyStateMachine(serial_controller=self.serial, parent=self)
        self.sensor_worker = SensorWorker(parent=self)

        self.current_temp = 25.0
        self.current_humi = 55.0
        self.latest_chem_items: List[str] = []
        self.latest_unattended = False
        self.latest_alarms: Dict[str, Any] = {}
        self.latest_violations: List[str] = []
        self.progress = (0, 0)
        self.fps_text = ""
        self.media_label = "摄像头"

        self._lock = threading.Lock()
        self._frames = (None, None)
        self._frame_seq = 0
        self._jpeg_cache: Dict[str, tuple] = {}
        self._mute_until = 0.0
        self.extensions: Dict[str, Any] = {}   # agent / env / jepa 等可选模块挂在这里

        self.inference_thread.update_frames_signal.connect(self._on_frames)
        self.state_machine.alert_triggered.connect(self.alert.emit)
        self.sensor_worker.sensor_data_updated.connect(self._on_sensor)
        self.sensor_worker.mode_changed.connect(self.sensor_mode_changed.emit)

        # 没有画面时也要定时研判（例如摄像头全部掉线，但热源仍在加热）
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(1000)

    # ------------------------------------------------------------------
    def start(self):
        self.inference_thread.start()
        self.sensor_worker.start()

    def stop(self):
        self._tick.stop()
        self.sensor_worker.stop()
        self.inference_thread.stop()

    # ------------------------------------------------------------------
    def _evaluate(self):
        state, _ = self.state_machine.evaluate(
            micro_detections=self.latest_chem_items,
            macro_unattended=self.latest_unattended,
            current_temp=self.current_temp,
            current_humi=self.current_humi,
        )
        return state

    def _on_tick(self):
        self._evaluate()
        for ext in list(self.extensions.values()):
            tick = getattr(ext, "tick", None)
            if tick is not None:
                try:
                    tick()
                except Exception as e:
                    print(f"[Engine] 扩展模块异常: {e}")

    def _on_sensor(self, temp: float, humi: float):
        self.current_temp = temp
        self.current_humi = humi
        state = self._evaluate()
        self.telemetry_changed.emit(state)

    def _on_frames(self, pano, desk, alarms, violations, curr_f, tot_f, log_str):
        self.latest_unattended = alarms.get("unattended", False)
        self.latest_chem_items = alarms.get("chem_items", [])
        self.latest_alarms = dict(alarms)
        self.latest_violations = list(violations)
        self.progress = (curr_f, tot_f)
        line = next((l for l in log_str.splitlines() if l.startswith("[系统]")), "")
        if "FPS" in line:
            self.fps_text = line.split("|")[0].replace("[系统]", "").strip()
        with self._lock:
            self._frames = (pano, desk)
            self._frame_seq += 1
        state = self._evaluate()
        self.frames_ready.emit(pano, desk, alarms, violations, curr_f, tot_f, log_str, state)

    # ------------------------------------------------------------------
    # 画面（供 MJPEG 推流与 Agent 看图）
    # ------------------------------------------------------------------
    def latest_frames(self):
        with self._lock:
            return self._frames

    def latest_jpeg(self, which: str = "pano", width: Optional[int] = None, quality: int = 70) -> Optional[bytes]:
        """返回最新带框画面的 JPEG（同一帧只编码一次）"""
        import cv2
        width = max(1, int(width or cfg("web.stream_width", 640)))
        quality = max(1, min(100, int(quality)))
        with self._lock:
            seq = self._frame_seq
            frame = self._frames[0 if which == "pano" else 1]
            cached = self._jpeg_cache.get(which)
        if frame is None:
            return None
        cache_key = (seq, width, quality)
        if cached and cached[0] == cache_key:
            return cached[1]
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
        if not ok:
            return None
        data = buf.tobytes()
        with self._lock:
            self._jpeg_cache[which] = (cache_key, data)
        return data

    @property
    def frame_seq(self) -> int:
        return self._frame_seq

    # ------------------------------------------------------------------
    # 人工控制（小屏按钮 / Web 总后台）——只能断电、静音，不能上电或解锁
    # ------------------------------------------------------------------
    def cut_power(self, source: str = "人工") -> str:
        return self.state_machine.manual_power_off(source)

    def mute(self, seconds: float = 60.0) -> float:
        """静音语音提醒。横幅、灯光与告警状态不变；紧急状态下最多静音 30 秒"""
        cap = 30.0 if self.state_machine.current_state == "CRITICAL" else 60.0
        seconds = max(0.0, min(float(seconds), cap))
        self._mute_until = time.time() + seconds
        events.mute_until = self._mute_until
        events.log("语音静音", f"{seconds:.0f} 秒内不播放语音提醒（告警与断电不受影响）", kind="mute")
        return self._mute_until

    def call_admin(self, who: str = "实验员") -> None:
        events.log("呼叫管理员", f"{who}在终端上发起求助", level="WARNING", kind="call_admin", snapshot=True)

    # ------------------------------------------------------------------
    def _ppe_checklist(self) -> List[Dict[str, str]]:
        present = self.latest_alarms.get("person_present")
        out = []
        for kind, name in PPE_ITEMS:
            if present is None or present is False:
                st = "unknown"
            else:
                st = "bad" if events.is_active(kind) else "ok"
            out.append({"kind": kind, "name": name, "status": st})
        return out

    def snapshot(self) -> Dict[str, Any]:
        """JSON 友好的全量状态快照（Web / 小屏 / Agent 共用）"""
        sm = self.state_machine
        state = sm.snapshot()
        tel = telemetry.snapshot()
        link = self.serial
        alive = bool(link is not None and link.is_alive)
        hw = link.get_sensor_data() if alive else {}
        wm = sm.world_model.snapshot()
        wm["phase_text"] = PHASE_TEXT.get(wm.get("phase"), wm.get("phase"))
        active = sorted(events.active_events(), key=lambda e: -LEVEL_RANK.get(e.get("level"), 0))
        a = self.latest_alarms
        snap = {
            "ts": time.time(),
            "uptime_s": round(time.time() - self.started_at),
            "state": state["state"],
            "state_text": "断电锁定中" if state["latched"] else STATE_TEXT.get(state["state"], state["state"]),
            "latched": state["latched"],
            "latch_reason": state["latch_reason"],
            "power_off_state": getattr(link, "power_off_state", "idle") if link else "idle",
            "telemetry": {
                "temp": tel.get("temp"), "humi": tel.get("humi"), "obj_temp": tel.get("obj_temp"),
                "smoke": tel.get("smoke"), "source": tel.get("source"),
            },
            "link": {
                "mcu": alive, "mcu_text": link.status_text() if link else "下位机未启用",
                "relay_on": hw.get("relay_on") if alive else None,
                "hw_latched": hw.get("latched") if alive else None,
                "fan_pwm": hw.get("fan_pwm") if alive else None,
                "humid_on": hw.get("humid_on") if alive else None,
            },
            "vision": {
                "ok": a.get("vision_ok", True), "person_present": a.get("person_present"),
                "unattended": a.get("unattended", False), "chem_items": list(a.get("chem_items", [])),
                "falling": a.get("falling_warning", False), "sleeping": a.get("sleeping", False),
                "violations": list(self.latest_violations), "fps": self.fps_text,
                "ppe": self._ppe_checklist(), "progress": list(self.progress),
                "media": self.media_label,
            },
            "world_model": wm,
            "active_events": [{k: e.get(k) for k in ("kind", "level", "title", "message", "time_str", "uid")}
                              for e in active if e.get("level") != "INFO"],
            "stats": events.stats(),
            "mute_until": self._mute_until if self._mute_until > time.time() else 0,
            "rules": {k: cfg(f"rules.{k}") for k in ("dry_burn_c", "heating_on_c", "unattended_heating_cut_s",
                                                       "smoke_raw", "humidity_high", "ambient_high_c",
                                                       "predict_eta_warn_s")},
        }
        for name, ext in self.extensions.items():
            try:
                snap[name] = ext.status()
            except Exception as e:
                snap[name] = {"error": str(e)}
        return snap


def attach_extensions(engine: SafetyEngine, agent: bool = True) -> SafetyEngine:
    """挂载可选模块：外部温湿度控制、演示注入、视觉世界模型回写、Agent。任何一个失败都不影响安全主循环"""
    from src.core.env_control import EnvController
    from src.core.demo import DemoInjector
    from src.core.jepa_link import JepaLink
    engine.extensions["env"] = EnvController(engine.serial)
    engine.extensions["demo"] = DemoInjector(engine)
    engine.extensions["jepa"] = JepaLink()
    if agent:
        try:
            from src.agent.agent import SafetyAgent
            engine.extensions["agent"] = SafetyAgent(engine)
        except Exception as e:
            print(f"[Engine] Agent 未启用: {e}")
    return engine
