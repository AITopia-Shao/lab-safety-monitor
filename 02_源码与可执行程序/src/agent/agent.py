"""
安全研判 Agent（认知层）
src/agent/agent.py

定位："只建议、不执行"。Agent 能读取系统状态、事件、世界模型预测、安全手册，
输出原因分析、处置建议与手册条款引用；它没有任何电源、继电器、解锁类工具。
断电只由规则（状态机）和下位机硬件执行 —— AI 不碰电闸。

三种触发：
  1. 自动研判：新的 WARNING / CRITICAL 事件 → 后台生成"研判卡"，写回事件（小屏与总后台可见）
  2. 对话问答：总后台提问
  3. 实验安全报告：一键汇总本次运行的事件与处置

线路：远程 API / 本地主机模型 / 规则模板（断网兜底），见 llm_client.py。
"""
import base64
import json
import os
import queue
import threading
import time
from collections import deque
from typing import Dict, Any, List, Optional, Callable

from src.config import cfg
from src.core.events import events, LEVEL_RANK
from src.agent.llm_client import LLMRouter, LLMError
from src.agent.rag import Manual

SYSTEM_PROMPT = (
    "你是高危化学实验室的安全研判助手，部署在实验台边缘终端上。"
    "你可以调用只读工具查看系统状态、事件、温度预测和安全手册。"
    "你没有任何控制电源或设备的能力：断电由规则和硬件自动执行，恢复供电只能由人员在现场按复位键。"
    "回答要求：中文，简洁、具体、可执行；先给结论，再给处置步骤（不超过 3 步）；"
    "引用手册时写明条款编号；不要编造传感器没有给出的数据。"
)

TOOLS = [
    {"type": "function", "function": {
        "name": "get_status", "description": "获取系统当前状态：安全等级、遥测、视觉、断电锁定、世界模型预测",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_recent_events", "description": "获取最近的安全事件列表（新的在前）",
        "parameters": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "条数，默认 10"}}}}},
    {"type": "function", "function": {
        "name": "get_forecast", "description": "获取热力学世界模型的温度预测：阶段、干烧 ETA、未来 120 秒轨迹",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "search_manual", "description": "在实验室安全手册中检索相关条款",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词，如 干烧 处置"}}, "required": ["query"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# 规则模板：没有可用大模型时的兜底研判
RULE_ADVICE = {
    "dry_burn": ("热源温度超过干烧阈值，容器内液体可能已蒸干", ["系统已断电，保持断电状态", "等待器皿自然冷却，勿用冷水浇淋", "确认无冒烟后到现场按复位键"]),
    "dry_burn_predict": ("热源越过沸点仍在升温，即将达到干烧温度", ["立即降低加热功率或补充液体", "回到实验台查看液面", "如无法处理，先关闭热源"]),
    "temp_rate": ("热源升温过快，可能液体将尽或功率过大", ["降低加热功率", "检查容器液面", "必要时关闭热源"]),
    "unattended_heating": ("加热过程中无人值守", ["立即返回实验台", "如需离开，先关闭热源", "超时系统将自动断电"]),
    "dry_burn_fallback": ("未接热源测温，按环境温度兜底判断疑似干烧", ["系统已断电", "到现场确认器皿状态", "建议接入红外测温"]),
    "smoke": ("检测到烟雾或可燃气体超标", ["系统已断电，开启排风", "停止明火、疏散人员", "确认无火情后排查原因"]),
    "humidity_high": ("环境湿度过高", ["开启排风或除湿", "检查吸湿性试剂是否密封", "精密仪器加防潮措施"]),
    "ambient_high": ("环境温度过高", ["加强通风", "检查是否有设备异常发热", "挥发性试剂移入通风橱"]),
    "non_heatable_heating": ("加热时台面出现不可加热的量器", ["确认量器未被加热", "需加热的溶液转入烧杯或烧瓶", "量器远离热源"]),
    "edge_risk": ("器皿靠近台面边缘", ["将器皿移至台面内侧 10 厘米以上", "整理台面通道"]),
    "eating": ("实验区出现饮食行为", ["立即停止饮食", "离开实验区后洗手"]),
    "sleeping": ("人员疑似伏案或长时间低头静止", ["呼叫确认人员状态", "若在加热，先关闭热源"]),
}
PPE_ADVICE = ("人员防护装备不完整", ["立即补戴缺失的防护装备", "补戴前暂停操作"])

# 不单独生成建议的事件：链路 / 静音等系统消息，以及"执行断电""断电锁定"这类伴随事件（建议写在引发断电的那条事件上）
SKIP_KINDS = {"mute", "link", "mcu_event", "power_reset", "vision_offline", "unattended", "config", "demo",
              "power_off", "power_latched", "power_off_ack", "power_off_fail", "model", "system"}


class SafetyAgent:
    def __init__(self, engine=None):
        self.engine = engine
        self.llm = LLMRouter()
        self.manual = Manual()
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=8)
        self._last_kind_ts: Dict[str, float] = {}
        self._log = deque(maxlen=50)
        self.reviews_done = 0
        self.busy = False
        self._worker = threading.Thread(target=self._loop, name="SafetyAgent", daemon=True)
        self._worker.start()
        events.add_listener(self._on_event)

    # ------------------------------------------------------------------
    # 工具（全部只读）
    # ------------------------------------------------------------------
    def _status_brief(self) -> Dict[str, Any]:
        if self.engine is None:
            return {}
        s = self.engine.snapshot()
        return {"state": s["state"], "state_text": s["state_text"], "latched": s["latched"],
                "latch_reason": s["latch_reason"], "telemetry": s["telemetry"],
                "vision": {k: s["vision"][k] for k in ("person_present", "unattended", "chem_items", "violations")},
                "world_model": {k: s["world_model"].get(k) for k in ("phase_text", "temp", "eta_s", "t_inf",
                                                                      "plateau_s", "risk")},
                "active_events": s["active_events"]}

    def _call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        if name not in TOOL_NAMES:
            return {"error": "没有这个工具"}
        if name == "get_status":
            return self._status_brief()
        if name == "get_recent_events":
            n = max(1, min(int(args.get("n", 10) or 10), 30))
            return [{k: e.get(k) for k in ("time_str", "level", "title", "message", "agent_note")}
                    for e in events.history(n, "NOTICE")]
        if name == "get_forecast":
            return self.engine.state_machine.world_model.snapshot() if self.engine else {}
        if name == "search_manual":
            return [{"条款": d["title"], "内容": d["text"]} for d in self.manual.search(str(args.get("query", "")), 3)]
        return {}

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        st = self.llm.status()
        st.update({"busy": self.busy, "queued": self._q.qsize(), "reviews_done": self.reviews_done,
                   "auto_review": bool(cfg("agent.auto_review", True)),
                   "send_images_remote": bool(cfg("agent.send_images_remote", False)),
                   "manual_entries": len(self.manual.docs), "log": list(self._log)[-8:]})
        return st

    def ask(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in (history or [])[-6:]:
            if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
                msgs.append({"role": h["role"], "content": h["content"][:1000]})
        msgs.append({"role": "user", "content": question[:1000]})
        return self._run(msgs, fallback=lambda: self._rule_answer(question))

    def review_event(self, uid: str) -> Optional[str]:
        ev = events.get_event(uid)
        if ev is None:
            return None
        ctx = {"事件": {k: ev.get(k) for k in ("time_str", "level", "title", "message", "details")},
               "当前状态": self._status_brief(),
               "相关手册条款": [{"条款": d["title"], "内容": d["text"]}
                          for d in self.manual.search(ev["title"] + " " + ev.get("message", ""), 2)]}
        prompt = ("请对下面这条安全事件做研判，输出 3 行：\n"
                  "原因：（一句话）\n处置：（1-3 步，用；分隔）\n依据：（手册条款编号与标题）\n\n"
                  + json.dumps(ctx, ensure_ascii=False, default=str))
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        res = self._run(msgs, fallback=lambda: self._rule_review(ev), use_tools=False,
                        image_b64=self._event_image(ev))
        text = res["text"].strip()
        tag = res["provider_label"]
        note = f"{text}\n— {tag}"
        events.set_agent_note(uid, note)
        self.reviews_done += 1
        self._log.append(f"{time.strftime('%H:%M:%S')} 生成建议「{ev['title']}」· {tag}")
        return note

    def report(self) -> Dict[str, Any]:
        hist = events.history(60, "NOTICE")
        st = events.stats()
        ctx = {"统计": st, "当前状态": self._status_brief(),
               "事件": [{k: e.get(k) for k in ("time_str", "level", "title", "message", "agent_note", "review")}
                      for e in hist]}
        prompt = ("请根据以下数据写一份《实验安全报告》，结构：一、总体结论；二、主要风险事件（按严重程度）；"
                  "三、处置情况；四、改进建议（引用手册条款）。300 字以内。\n\n"
                  + json.dumps(ctx, ensure_ascii=False, default=str))
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        return self._run(msgs, fallback=lambda: self._rule_report(hist, st), use_tools=False, max_tokens=900)

    def configure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """总后台修改线路配置；API Key 只存内存，不写文件、不回显"""
        from src.config import set_value
        if data.get("route") in ("auto", "remote", "local", "off"):
            set_value("agent.route", data["route"])
        for name in ("remote", "local"):
            sub = data.get(name) or {}
            for key in ("base_url", "model", "vision_model"):
                if isinstance(sub.get(key), str):
                    set_value(f"agent.{name}.{key}", sub[key].strip()[:200])
            if isinstance(sub.get("api_key"), str) and sub["api_key"].strip():
                self.llm.providers[name].runtime_key = sub["api_key"].strip()
        for key in ("auto_review", "send_images_remote"):
            if isinstance(data.get(key), bool):
                set_value(f"agent.{key}", data[key])
        return self.status()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _run(self, msgs, fallback: Callable[[], str], use_tools: bool = True,
             image_b64: Optional[str] = None, max_tokens: int = 600) -> Dict[str, Any]:
        self.busy = True
        tool_trace = []
        try:
            for _ in range(4):
                msg, p = self.llm.chat(msgs, tools=TOOLS if use_tools else None,
                                       image_b64=image_b64, max_tokens=max_tokens)
                calls = msg.get("tool_calls") or []
                if not calls:
                    return {"text": (msg.get("content") or "").strip() or "（模型未返回内容）",
                            "provider": p.name, "provider_label": p.label(), "tools": tool_trace}
                msgs = msgs + [{"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}]
                for c in calls:
                    fn = c.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    result = self._call_tool(fn.get("name", ""), args)
                    tool_trace.append(fn.get("name", ""))
                    msgs.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                 "content": json.dumps(result, ensure_ascii=False, default=str)[:3000]})
            raise LLMError("工具调用轮数过多")
        except LLMError as e:
            self._log.append(f"{time.strftime('%H:%M:%S')} 大模型不可用（{self._short_err(str(e))}），已用规则模板")
            return {"text": fallback(), "provider": "rules", "provider_label": "规则模板（离线兜底）",
                    "tools": tool_trace, "error": str(e)[:200]}
        finally:
            self.busy = False

    @staticmethod
    def _short_err(err: str) -> str:
        parts = []
        if "remote: 未配置" in err:
            parts.append("远程未配置 Key")
        elif "remote:" in err:
            parts.append("远程连接失败")
        if "local:" in err:
            parts.append("本地连不上")
        return "、".join(parts) or err[:40]

    def _event_image(self, ev) -> Optional[str]:
        path = ev.get("snapshot")
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
        except Exception:
            return None

    def _advice_for(self, kind: str):
        if kind and kind.startswith("ppe_"):
            return PPE_ADVICE
        return RULE_ADVICE.get(kind)

    def _rule_review(self, ev) -> str:
        cause, steps = self._advice_for(ev.get("kind")) or (ev.get("message") or ev.get("title"), ["到现场确认情况"])
        refs = self.manual.search(ev["title"] + " " + ev.get("message", ""), 1)
        ref = f"《手册》{refs[0]['title']}" if refs else "—"
        return f"原因：{cause}\n处置：{'；'.join(steps)}\n依据：{ref}"

    def _rule_answer(self, question: str) -> str:
        refs = self.manual.search(question, 2)
        s = self._status_brief()
        head = f"当前状态：{s.get('state_text', '未知')}。" if s else ""
        if not refs:
            return head + "（大模型不可用，规则模板未找到相关手册条款）"
        return head + "相关手册条款：\n" + "\n".join(f"· {d['title']}：{d['text'][:120]}" for d in refs)

    def _rule_report(self, hist, st) -> str:
        lines = [f"一、总体结论：本次运行共记录安全事件 {st['total']} 起（紧急 {st['CRITICAL']}、警告 {st['WARNING']}、"
                 f"提醒 {st['NOTICE']}），已复核 {st['reviewed']} 起。"]
        top = sorted(hist, key=lambda e: -LEVEL_RANK.get(e["level"], 0))[:5]
        if top:
            lines.append("二、主要风险事件：" + "；".join(f"{e['time_str']} {e['title']}" for e in top))
        lines.append("三、处置情况：紧急事件均由规则与下位机自动断电，恢复供电需现场复位。")
        lines.append("四、改进建议：加热全程有人值守（手册 1.1）；规范佩戴防护装备（手册 2.1–2.4）。")
        return "\n".join(lines)

    def _on_event(self, rec: Dict[str, Any]):
        """事件中心回调（在事件线程里执行，只做入队）"""
        if not bool(cfg("agent.auto_review", True)) or rec.get("kind") in SKIP_KINDS:
            return
        if LEVEL_RANK.get(rec.get("level"), 0) < LEVEL_RANK.get(cfg("agent.min_level", "WARNING"), 2):
            return
        now = time.time()
        if now - self._last_kind_ts.get(rec.get("kind"), 0) < 10:
            return
        self._last_kind_ts[rec.get("kind")] = now
        try:
            self._q.put_nowait(rec["uid"])
        except queue.Full:
            pass

    def _loop(self):
        while True:
            uid = self._q.get()
            time.sleep(1.5)   # 等事件截图写盘
            try:
                self.review_event(uid)
            except Exception as e:
                self._log.append(f"{time.strftime('%H:%M:%S')} 生成建议失败：{e}")
