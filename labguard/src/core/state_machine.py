"""
跨模态边缘状态机联合决策逻辑
src/core/state_machine.py

输入：
  · 宏观通道（全景摄像头）：是否无人值守
  · 微观通道（台面摄像头）：台面上识别到的器皿
  · 环境遥测：环境温度、湿度（SHT30）；热源表面温度（MLX90614）；烟雾（MQ-2）
    —— 热源温度与烟雾通过全局遥测快照读取，界面层无需改动

判决规则（阈值见 config.yaml → rules）：
  R1 干烧          热源温度 ≥ dry_burn_c                                   → CRITICAL，断电
  R2 升温异常      升温速率 ≥ rate_c_per_s 且温度 ≥ rate_min_c              → WARNING
  R3 加热无人值守  正在加热 且 无人值守：先 WARNING，持续 ≥ cut_s 秒后       → CRITICAL，断电
  R4 兜底规则      未接入热源测温时：可加热器皿 + 无人值守 + 环境温度 > 55℃ → CRITICAL，断电
  R5 烟雾          MQ-2 读数 ≥ smoke_raw                                    → CRITICAL，断电
  R6 高湿          湿度 > humidity_high                                      → WARNING
  R7 环境高温      环境温度 > ambient_high_c                                 → WARNING
  R8 量器加热      正在加热 且 台面出现容量瓶/量筒等量器                     → NOTICE 提醒
  R9 预测干烧      热力学世界模型预测 ETA ≤ predict_eta_warn_s 秒到达干烧阈值，
                   或沸腾平台持续 ≥ boil_unattended_warn_s 秒且无人值守      → WARNING（只预警，不断电）

断电锁定（LATCH）：
  一旦断电，状态保持 CRITICAL，直到现场按下控制盒的复位键（下位机上报）才解除。
  软件只能断电、不能上电。未接入下位机的演示模式下，危险解除 sim_latch_release_s 秒后自动解除。
"""
import threading
import time
from collections import deque
from datetime import datetime
from functools import wraps
from typing import List, Dict, Any, Optional, Tuple

try:
    from PySide6.QtCore import QObject, Signal
except ImportError:
    from PyQt6.QtCore import QObject, pyqtSignal as Signal

from src.config import cfg
from src.core.events import events
from src.core.telemetry import telemetry
from src.core.world_model import ThermalWorldModel

LEVEL_RANK = {"NOTICE": 1, "WARNING": 2, "CRITICAL": 3}

CUT_REASON_TEXT = {
    0: "未知原因",
    1: "热源超温（下位机本地规则）",
    2: "烟雾超标（下位机本地规则）",
    3: "上位机失联兜底（下位机看门狗）",
    4: "上位机联合决策",
}


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class SafetyStateMachine(QObject):
    """跨模态边缘状态机：联合宏观监控、微观监控与环境遥测进行安全决策"""

    # 推送结构化告警（只在事件开始或断电动作发生时推送，不会每帧重复）
    alert_triggered = Signal(dict)
    # 综合安全等级切换（NORMAL / WARNING / CRITICAL）
    state_changed = Signal(str)

    # 兼容旧代码的阈值常量
    HIGH_TEMP_THRESHOLD = float(cfg("rules.fallback_ambient_cut_c", 55.0))
    HIGH_HUMI_THRESHOLD = float(cfg("rules.humidity_high", 80.0))

    # 可以加热的器皿（与 chem_best.pt 的类别名对应，小写比较）
    HEATABLE_VESSELS = {
        "beaker", "conical flask", "flat bottom flask", "round bottom flask",
        "pear shaped flask", "test tube",
    }
    # 规范上禁止直接加热的量器 / 容器
    NON_HEATABLE_VESSELS = {
        "volumetric flask", "measuring cylinder", "reagent bottle", "burette",
        "pipette", "wash bottle",
    }
    # 兼容旧接口名
    DEFAULT_HEATING_KEYWORDS = HEATABLE_VESSELS

    HAZARD_KINDS = (
        "dry_burn", "temp_rate", "unattended_heating", "dry_burn_fallback",
        "smoke", "humidity_high", "ambient_high", "non_heatable_heating", "dry_burn_predict",
    )

    def __init__(self, serial_controller=None, parent=None):
        super().__init__(parent)
        # 网页人工断电和 Qt 评估可能并发，整次状态变更必须保持一致。
        self._lock = threading.RLock()
        self.serial_controller = None
        self.heating_keywords = set(self.HEATABLE_VESSELS)

        self.current_state = "NORMAL"
        self.latched = False
        self.latch_reason = ""
        self.latch_since = 0.0
        self.last_power_off_time = 0.0
        self.power_off_cooldown = 3.0
        self.last_alert_time: Dict[str, float] = {}

        self._unattended_heat_since: Optional[float] = None
        self._hazard_clear_since: Optional[float] = None
        self._temp_hist: deque = deque(maxlen=120)
        self._last_tel_ts = 0.0
        self._prev_hw_latched: Optional[bool] = None
        # 连接中断不能把真实硬件锁定降为可自动解除的演示锁定。
        self._hardware_observed = False
        self._field_reset_flag = False
        self._listener_hooked = set()
        # 器皿识别结果会逐帧跳动：最近 ITEM_HOLD_S 秒内出现过就算"在台面上"，避免规则反复触发 / 解除
        self._item_seen: Dict[str, float] = {}
        # 热力学世界模型：预测温度轨迹与干烧 ETA（只用于预警与展示，不参与断电）
        self.world_model = ThermalWorldModel()

        self.set_serial_controller(serial_controller)

    # ------------------------------------------------------------------
    # 下位机挂载
    # ------------------------------------------------------------------
    @_synchronized
    def set_serial_controller(self, serial_controller):
        self.serial_controller = serial_controller
        if serial_controller is not None and hasattr(serial_controller, "add_listener"):
            if id(serial_controller) not in self._listener_hooked:
                serial_controller.add_listener(self._on_serial_event)
                self._listener_hooked.add(id(serial_controller))

    @_synchronized
    def _on_serial_event(self, kind: str, info: dict):
        """串口线程回调：只记录日志、设置标志，界面相关动作留给 evaluate()（主线程）处理"""
        if kind == "power_off_acked":
            events.log("断电已确认", "下位机已执行断电，继电器已断开", level="CRITICAL", kind="power_off_ack")
        elif kind == "power_off_failed":
            events.log("断电指令未确认", f"{info.get('reason', '')}，请立即人工切断加热电源！",
                       level="CRITICAL", kind="power_off_fail")
        elif kind == "mcu_event":
            code = info.get("code")
            level = "CRITICAL" if code in (1, 2, 3) else "INFO"
            events.log("下位机上报", info.get("desc", ""), level=level, kind="mcu_event")
            if code == 5:
                self._field_reset_flag = True
        elif kind == "link_up":
            events.log("下位机已连接", info.get("port", ""), kind="link")
        elif kind == "link_lost":
            events.log("下位机连接断开", info.get("reason", ""), level="WARNING", kind="link")

    def _hw(self) -> Optional[dict]:
        link = self.serial_controller
        if link is not None and getattr(link, "is_alive", False):
            return link.get_sensor_data()
        return None

    # ------------------------------------------------------------------
    # 辅助判断
    # ------------------------------------------------------------------
    def is_heating_device_detected(self, micro_detections: List[str]) -> bool:
        """兼容旧接口：台面上是否有可加热器皿"""
        return bool(self._match(micro_detections, self.heating_keywords))

    @staticmethod
    def _match(items: List[str], names) -> List[str]:
        found = set()
        for item in items or []:
            low = str(item).strip().lower()
            if low in names:
                found.add(str(item))
        return sorted(found)

    def _temp_rate(self) -> Optional[float]:
        """最近约 5 秒的热源升温速率（℃/s）"""
        if len(self._temp_hist) < 3:
            return None
        t_now, v_now = self._temp_hist[-1]
        for t, v in self._temp_hist:
            if t_now - t <= 5.0:
                if t_now - t < 2.0:
                    return None
                return (v_now - v) / (t_now - t)
        return None

    # ------------------------------------------------------------------
    # 主评估入口（接口保持不变）
    # ------------------------------------------------------------------
    @_synchronized
    def evaluate(
        self,
        micro_detections: List[str],
        macro_unattended: bool,
        current_temp: float,
        current_humi: float,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        now = time.time()
        extra = extra_context or {}
        rules = lambda k, d: float(cfg(f"rules.{k}", d))

        snap = telemetry.snapshot()
        if snap.get("source") == "hardware":
            self._hardware_observed = True
        obj_temp = extra.get("obj_temp", snap.get("obj_temp"))
        smoke = extra.get("smoke", snap.get("smoke"))
        if obj_temp is not None and snap.get("ts", 0) != self._last_tel_ts:
            self._last_tel_ts = snap.get("ts", 0)
            self._temp_hist.append((now, float(obj_temp)))
            wm = self.world_model.update(float(obj_temp), now, bool(macro_unattended))
        elif obj_temp is None:
            self._temp_hist.clear()
            wm = self.world_model.update(None, now)
        else:
            wm = self.world_model.snapshot()

        for item in micro_detections or []:
            self._item_seen[str(item)] = now
        hold = float(cfg("rules.item_hold_s", 3.0))
        recent_items = [k for k, t in self._item_seen.items() if now - t <= hold]
        heatable = self._match(recent_items, self.heating_keywords)
        non_heatable = self._match(recent_items, self.NON_HEATABLE_VESSELS)
        heating = obj_temp is not None and obj_temp >= rules("heating_on_c", 50.0)

        hazards: Dict[str, Dict[str, Any]] = {}

        def add(kind, level, title, message, cut=False, event_type=None):
            hazards[kind] = {"kind": kind, "level": level, "title": title, "message": message,
                             "cut": cut, "event_type": event_type or kind.upper()}

        # R1 干烧
        dry_burn_c = rules("dry_burn_c", 120.0)
        if obj_temp is not None and obj_temp >= dry_burn_c:
            add("dry_burn", "CRITICAL", "干烧高危事件",
                f"热源表面温度 {obj_temp:.1f}℃ ≥ {dry_burn_c:.0f}℃（容器内液体已蒸干），立即断电",
                cut=True, event_type="HIGH_RISK_DRY_BURN")

        # R2 升温异常
        rate = self._temp_rate()
        if (obj_temp is not None and rate is not None and rate >= rules("rate_c_per_s", 2.0)
                and obj_temp >= rules("rate_min_c", 80.0) and "dry_burn" not in hazards):
            add("temp_rate", "WARNING", "热源升温异常",
                f"热源 {obj_temp:.1f}℃，升温速率 {rate:.1f}℃/s，存在干烧趋势")

        # R9 预测干烧（世界模型）：只预警，不断电
        if obj_temp is not None and "dry_burn" not in hazards:
            eta = wm.get("eta_s")
            eta_warn = rules("predict_eta_warn_s", 90.0)
            boil_warn = rules("boil_unattended_warn_s", 300.0)
            if wm.get("phase") == "heating" and eta is not None and 0 < eta <= eta_warn:
                trend = f"渐近温度 {wm['t_inf']:.0f}℃" if wm.get("t_inf") is not None else f"升温 {wm.get('slope') or 0:.2f}℃/s"
                add("dry_burn_predict", "WARNING", "预测即将干烧",
                    f"热源已越过沸点仍在升温（{obj_temp:.1f}℃，{trend}），液体可能已烧干，"
                    f"预计约 {eta:.0f} 秒后达到 {dry_burn_c:.0f}℃，请立即关火或补液")
            elif wm.get("phase") == "boiling" and macro_unattended and wm.get("plateau_s", 0) >= boil_warn:
                add("dry_burn_predict", "WARNING", "沸腾中无人看守",
                    f"已沸腾 {wm['plateau_s']:.0f} 秒且无人值守，液体持续蒸发，存在烧干风险")

        # R3 加热中无人值守
        cut_s = rules("unattended_heating_cut_s", 60.0)
        if heating and macro_unattended:
            if self._unattended_heat_since is None:
                self._unattended_heat_since = now
            dur = now - self._unattended_heat_since
            if dur >= cut_s:
                add("unattended_heating", "CRITICAL", "加热中无人值守超时",
                    f"热源 {obj_temp:.1f}℃，人员离开已 {dur:.0f} 秒，执行断电", cut=True)
            else:
                add("unattended_heating", "WARNING", "加热中无人值守",
                    f"热源 {obj_temp:.1f}℃，人员已离开，{cut_s - dur:.0f} 秒后将自动断电")
        else:
            self._unattended_heat_since = None

        # R4 兜底规则：没有热源测温时，沿用"器皿 + 无人值守 + 环境高温"
        fallback_c = rules("fallback_ambient_cut_c", 55.0)
        if (obj_temp is None and heatable and macro_unattended
                and current_temp is not None and current_temp > fallback_c):
            add("dry_burn_fallback", "CRITICAL", "疑似干烧（环境温度兜底）",
                f"台面有 {', '.join(heatable)}，无人值守，环境温度 {current_temp:.1f}℃ > {fallback_c:.0f}℃"
                f"（未接入热源测温，使用兜底规则）",
                cut=True, event_type="HIGH_RISK_DRY_BURN")

        # R5 烟雾
        smoke_raw = rules("smoke_raw", 1800)
        if smoke is not None and smoke >= smoke_raw:
            add("smoke", "CRITICAL", "烟雾 / 可燃气体超标", f"MQ-2 读数 {smoke} ≥ {smoke_raw:.0f}，立即断电", cut=True)

        # R6 高湿
        humi_high = rules("humidity_high", 80.0)
        if current_humi is not None and current_humi > humi_high:
            add("humidity_high", "WARNING", "高湿受潮环境告警",
                f"湿度 {current_humi:.1f}% > {humi_high:.0f}%，仪器与试剂有受潮风险，请开启除湿",
                event_type="ENV_ALERT_HUMIDITY")

        # R7 环境高温
        amb_high = rules("ambient_high_c", 40.0)
        if current_temp is not None and current_temp > amb_high:
            add("ambient_high", "WARNING", "环境温度过高",
                f"环境温度 {current_temp:.1f}℃ > {amb_high:.0f}℃，请检查通风与热源")

        # R8 量器加热
        if heating and non_heatable:
            add("non_heatable_heating", "NOTICE", "加热中台面出现量器",
                f"检测到 {', '.join(non_heatable)}：量器不可直接加热，请确认")

        # ---------------- 断电与锁定 ----------------
        new_alert: Optional[Dict[str, Any]] = None
        cut_hazards = [h for h in hazards.values() if h["cut"]]
        hw = self._hw()
        if hw is not None:
            self._hardware_observed = True

        # 现场复位（下位机上报复位事件，或硬件锁定由 True 变为 False）
        hw_latched = hw.get("latched") if hw else None
        field_reset = self._field_reset_flag or (self._prev_hw_latched is True and hw_latched is False)
        self._field_reset_flag = False
        if hw is not None:
            self._prev_hw_latched = hw_latched
        if field_reset and self.latched:
            self._release_latch("现场已按下复位键，解除断电锁定，恢复供电")

        # 下位机自主断电（本地超温 / 烟雾 / 看门狗）→ 同步锁定
        if hw is not None and hw_latched and not self.latched:
            reason = CUT_REASON_TEXT.get(hw.get("cut_reason", 0), "未知原因")
            self._set_latch(f"下位机已自主断电：{reason}")
            new_alert = self._make_alert("CRITICAL", "MCU_LOCAL_POWER_OFF", "下位机自主断电",
                                         f"{reason}。继电器已断开，需现场复位", obj_temp, current_temp, current_humi)

        if cut_hazards:
            top = cut_hazards[0]
            need_send = (not self.latched) or (
                (hw is None or not hw_latched) and now - self.last_power_off_time > self.power_off_cooldown)
            if need_send:
                first_time = not self.latched
                self.last_power_off_time = now
                ok, action_msg = self._execute_power_off()
                if first_time:
                    self._set_latch(top["title"])
                    level = "CRITICAL"
                    events.log("执行断电", action_msg, level=level, kind="power_off", snapshot=True)
                    new_alert = self._make_alert(level, top["event_type"], top["title"],
                                                 f"{top['message']}。{action_msg}",
                                                 obj_temp, current_temp, current_humi,
                                                 micro_detections, macro_unattended, ok)
            self._hazard_clear_since = None
        elif self.latched:
            # 纯演示时可自动解锁；真实下位机失联后仍须等现场复位。
            if hw is None and not self._hardware_observed:
                if self._hazard_clear_since is None:
                    self._hazard_clear_since = now
                elif now - self._hazard_clear_since >= rules("sim_latch_release_s", 10.0):
                    self._release_latch("（未接入下位机）危险已解除，软件锁定自动解除；现场继电器状态以控制盒为准")

        # ---------------- 事件上报 ----------------
        for kind in self.HAZARD_KINDS:
            h = hazards.get(kind)
            if h is None:
                events.update(kind, False)
                continue
            started = events.update(kind, True, h["level"], h["title"], h["message"],
                                    details={"obj_temp": obj_temp, "temp": current_temp, "humi": current_humi,
                                             "vessels": list(micro_detections or []), "unattended": macro_unattended,
                                             "eta_s": wm.get("eta_s"), "phase": wm.get("phase"),
                                             "risk": wm.get("risk")})
            if started and not h["cut"] and new_alert is None:
                new_alert = self._make_alert(h["level"], h["event_type"], h["title"], h["message"],
                                             obj_temp, current_temp, current_humi)
        events.update("power_latched", self.latched, "CRITICAL", "断电锁定中",
                      f"原因：{self.latch_reason}。需现场按复位键恢复供电", snapshot=False)

        # ---------------- 综合状态 ----------------
        if self.latched or any(h["level"] == "CRITICAL" for h in hazards.values()):
            new_state = "CRITICAL"
        elif any(h["level"] == "WARNING" for h in hazards.values()):
            new_state = "WARNING"
        else:
            new_state = "NORMAL"
        if new_state != self.current_state:
            self.current_state = new_state
            self.state_changed.emit(new_state)

        if new_alert is not None:
            self.last_alert_time[new_alert["event_type"]] = now
            self.alert_triggered.emit(new_alert)

        top_alert = new_alert
        if top_alert is None and hazards:
            h = max(hazards.values(), key=lambda x: LEVEL_RANK[x["level"]])
            top_alert = self._make_alert(h["level"], h["event_type"], h["title"], h["message"],
                                         obj_temp, current_temp, current_humi)
        return self.current_state, top_alert

    # ------------------------------------------------------------------
    def _make_alert(self, level, event_type, title, message, obj_temp, temp, humi,
                    micro=None, unattended=None, power_off_sent=None) -> Dict[str, Any]:
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            "level": level,
            "title": title,
            "message": message,
            "details": {
                "temperature": temp, "humidity": humi, "obj_temperature": obj_temp,
                "micro_detections": list(micro or []), "macro_unattended": unattended,
                "power_off_sent": power_off_sent, "latched": self.latched,
            },
        }

    def _set_latch(self, reason: str):
        if self._hw() is not None or telemetry.get("source") == "hardware":
            self._hardware_observed = True
        self.latched = True
        self.latch_reason = reason
        self.latch_since = time.time()
        self._hazard_clear_since = None

    def _release_latch(self, msg: str):
        self.latched = False
        self.latch_reason = ""
        self._hazard_clear_since = None
        events.log("解除断电锁定", msg, kind="power_reset")

    def _execute_power_off(self) -> Tuple[bool, str]:
        """向下位机下发断电指令；返回 (是否成功下发, 如实描述)"""
        link = self.serial_controller
        demo_only = not self._hardware_observed and telemetry.get("source") != "hardware"
        no_link_msg = "串口未连接，断电指令无法下发！请立即人工切断加热电源（下位机本地保护仍独立生效）"
        if link is None or not hasattr(link, "send_power_off"):
            return False, "未挂载下位机，已在软件中锁定（演示模式）" if demo_only else no_link_msg
        try:
            ok = link.send_power_off()
        except Exception as e:
            return False, f"断电指令下发异常：{e}，请立即人工切断加热电源！"
        if ok is False:
            if getattr(link, "is_open", False) is False and demo_only:
                return False, "未接入下位机（演示模式），已在软件中模拟断电锁定"
            return False, no_link_msg
        return True, "断电指令已下发，等待下位机确认"

    @_synchronized
    def manual_power_off(self, source: str = "人工") -> str:
        """人工一键断电（小屏长按 / 总后台）。只能断电；恢复供电仍需现场按复位键"""
        ok, action_msg = self._execute_power_off()
        self.last_power_off_time = time.time()
        if not self.latched:
            self._set_latch(f"人工一键断电（{source}）")
        events.log("人工一键断电", f"来源：{source}。{action_msg}", level="CRITICAL", kind="manual_power_off",
                   snapshot=True)
        if self.current_state != "CRITICAL":
            self.current_state = "CRITICAL"
            self.state_changed.emit("CRITICAL")
        return action_msg

    @_synchronized
    def snapshot(self) -> Dict[str, Any]:
        """供其他线程读取同一次状态变更的锁定信息。"""
        return {"state": self.current_state, "latched": self.latched,
                "latch_reason": self.latch_reason}

    @_synchronized
    def reset(self):
        """软件复位（仅清除软件状态；若下位机已锁定，仍需现场复位）"""
        self.latched = False
        self.latch_reason = ""
        self._unattended_heat_since = None
        self._hazard_clear_since = None
        self.last_alert_time.clear()
        self.current_state = "NORMAL"
        self.state_changed.emit("NORMAL")
