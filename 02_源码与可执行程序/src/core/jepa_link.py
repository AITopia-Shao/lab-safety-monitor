"""
视觉世界模型（V-JEPA 2）结果接收端
src/core/jepa_link.py

V-JEPA 2 体量较大（ViT-L 约 3 亿参数），不在香橙派上运行：由主机侧 hub/jepa_worker.py
从总后台拉取台面画面片段、计算"预期违背惊奇度"，再 POST 回 /api/jepa/score。
这里只负责保存最近的分数、超过阈值时记一条 NOTICE 事件，交给 Agent 研判。
它不参与任何断电决策。
"""
import time
import threading
from collections import deque
from typing import Dict, Any

from src.config import cfg
from src.core.events import events


class JepaLink:
    def __init__(self):
        self._lock = threading.Lock()
        self.history = deque(maxlen=120)   # (ts, score)
        self.last_note = ""
        self.model = ""
        self._last_event = 0.0

    def push(self, score: float, note: str = "", model: str = ""):
        now = time.time()
        with self._lock:
            self.history.append((now, round(score, 4)))
            self.last_note = note
            self.model = model or self.model
        thr = float(cfg("jepa.surprise_threshold", 0.0) or 0.0)
        if thr > 0 and score >= thr and now - self._last_event > 30:
            self._last_event = now
            events.log("画面异常（V-JEPA 2）",
                       f"台面画面异常度 {score:.1f} ≥ 阈值 {thr:.1f}（偏离正常画面的标准差倍数），画面变化不符合预期" +
                       (f"：{note}" if note else ""), level="NOTICE", kind="jepa_surprise", snapshot=True)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            last = self.history[-1] if self.history else None
            online = bool(last and time.time() - last[0] < 60)
            return {"online": online, "model": self.model,
                    "score": last[1] if last else None,
                    "threshold": float(cfg("jepa.surprise_threshold", 0.0) or 0.0),
                    "note": self.last_note,
                    "history": [[round(t), s] for t, s in list(self.history)[-60:]]}
