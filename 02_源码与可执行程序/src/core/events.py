"""
事件中心：分级干预 + 事件留痕
src/core/events.py

视觉违规与状态机告警都通过 events.update(kind, active, ...) 上报，事件中心负责：
  1. 上升沿（开始）/下降沿（解除）检测，避免同一事件每帧重复刷屏；
  2. 分级干预：NOTICE → 语音提醒 + 黄灯；WARNING → 黄灯 + 群通知；CRITICAL → 红灯（断电由状态机执行）；
  3. 事件持续存在时，按 reminder_s 周期重复语音提醒；
  4. 写入 SQLite 事件库，并保存事件发生瞬间的双路画面截图（后台线程，不阻塞推理）；
  5. 维护最近事件列表，供终端日志区滚动显示。
"""
import os
import json
import time
import queue
import sqlite3
import threading
import urllib.request
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any

from src.config import cfg, resolve_path

LEVEL_RANK = {"INFO": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3}
LEVEL_LIGHT = {"INFO": 0, "NOTICE": 1, "WARNING": 1, "CRITICAL": 2}
LEVEL_TAG = {"INFO": "信息", "NOTICE": "提醒", "WARNING": "警告", "CRITICAL": "紧急"}


def _serial():
    try:
        from serial_comm import SerialComm
        return SerialComm.get_shared()
    except Exception:
        return None


class EventCenter:
    def __init__(self):
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._recent = deque(maxlen=60)
        self._latest_frames = (None, None)
        self._io_q: "queue.Queue[tuple]" = queue.Queue(maxsize=200)
        self._worker: Optional[threading.Thread] = None
        self._light_level = -1
        self.persist_enabled = True

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_latest_frames(self, pano, desk):
        """推理线程每帧调用：记录最新的标注画面，供事件截图使用"""
        self._latest_frames = (pano, desk)

    def update(self, kind: str, active: bool, level: str = "NOTICE", title: str = "",
               message: str = "", details: Optional[dict] = None, snapshot: bool = True) -> Optional[dict]:
        """
        上报某类事件当前是否成立。只在状态变化时产生日志与动作。
        返回值：事件刚开始时返回事件字典，否则返回 None。
        """
        now = time.time()
        with self._lock:
            cur = self._active.get(kind)
            if active and cur is None:
                ev = {
                    "kind": kind, "level": level, "title": title, "message": message,
                    "details": details or {}, "since": now, "last_remind": now,
                    "time_str": datetime.fromtimestamp(now).strftime("%H:%M:%S"),
                }
                self._active[kind] = ev
                self._push_line(f"[{ev['time_str']}][{LEVEL_TAG.get(level, level)}] {title}：{message}")
                self._enqueue_io("start", ev, snapshot)
                self._act_on_start(ev)
                self._refresh_light()
                return dict(ev)
            if active and cur is not None:
                # 等级升级（如 WARNING → CRITICAL）视为新的事件开始
                if LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(cur["level"], 0):
                    cur.update({"level": level, "title": title, "message": message,
                                "details": details or {}, "last_remind": now})
                    ts = datetime.fromtimestamp(now).strftime("%H:%M:%S")
                    self._push_line(f"[{ts}][{LEVEL_TAG.get(level, level)}] {title}（升级）：{message}")
                    self._enqueue_io("escalate", cur, snapshot)
                    self._act_on_start(cur)
                    self._refresh_light()
                    return dict(cur)
                cur["message"] = message
                reminder_s = float(cfg("events.reminder_s", 30.0))
                if reminder_s > 0 and now - cur["last_remind"] >= reminder_s:
                    cur["last_remind"] = now
                    self._play_voice(kind)
                return None
            if not active and cur is not None:
                del self._active[kind]
                dur = now - cur["since"]
                ts = datetime.fromtimestamp(now).strftime("%H:%M:%S")
                self._push_line(f"[{ts}][解除] {cur['title']}（持续 {dur:.0f} 秒）")
                self._enqueue_io("end", dict(cur, duration=dur), False)
                self._refresh_light()
            return None

    def log(self, title: str, message: str = "", level: str = "INFO", kind: str = "system",
            snapshot: bool = False, details: Optional[dict] = None):
        """记录一次性事件（如断电指令结果、下位机上报、链路变化）"""
        now = time.time()
        ev = {"kind": kind, "level": level, "title": title, "message": message,
              "details": details or {}, "since": now,
              "time_str": datetime.fromtimestamp(now).strftime("%H:%M:%S")}
        with self._lock:
            text = f"[{ev['time_str']}][{LEVEL_TAG.get(level, level)}] {title}"
            if message:
                text += f"：{message}"
            self._push_line(text)
            self._enqueue_io("once", ev, snapshot)
            if LEVEL_RANK.get(level, 0) >= LEVEL_RANK["WARNING"]:
                self._notify_webhook(ev)

    def is_active(self, kind: str) -> bool:
        with self._lock:
            return kind in self._active

    def active_events(self):
        with self._lock:
            return [dict(v) for v in self._active.values()]

    def recent_lines(self, n: int = 8):
        with self._lock:
            return list(self._recent)[-n:]

    # ------------------------------------------------------------------
    # 干预动作
    # ------------------------------------------------------------------
    def _act_on_start(self, ev: dict):
        self._play_voice(ev["kind"])
        self._notify_webhook(ev)

    def _play_voice(self, kind: str):
        track = (cfg("events.voice", {}) or {}).get(kind)
        link = _serial()
        if track and link is not None:
            link.play_voice(int(track))

    def _refresh_light(self):
        level = 0
        for ev in self._active.values():
            level = max(level, LEVEL_LIGHT.get(ev["level"], 0))
        if level != self._light_level:
            self._light_level = level
            link = _serial()
            if link is not None:
                link.set_alert_level(level)

    def _notify_webhook(self, ev: dict):
        url = (cfg("notify.webhook_url", "") or "").strip()
        min_level = cfg("notify.min_level", "WARNING")
        if not url or LEVEL_RANK.get(ev["level"], 0) < LEVEL_RANK.get(min_level, 2):
            return
        content = f"【实验室安全{LEVEL_TAG.get(ev['level'], '')}】{ev['time_str']} {ev['title']}\n{ev['message']}"
        self._enqueue_raw(("webhook", url, content))

    # ------------------------------------------------------------------
    # 日志与持久化
    # ------------------------------------------------------------------
    def _push_line(self, text: str):
        self._recent.append(text)
        print(f"[Event] {text}")

    def _enqueue_io(self, phase: str, ev: dict, snapshot: bool):
        if not self.persist_enabled:
            return
        frames = self._latest_frames if snapshot else (None, None)
        self._enqueue_raw(("event", phase, dict(ev), frames))

    def _enqueue_raw(self, item: tuple):
        if self._worker is None:
            self._worker = threading.Thread(target=self._io_loop, name="EventIO", daemon=True)
            self._worker.start()
        try:
            self._io_q.put_nowait(item)
        except queue.Full:
            pass

    def _io_loop(self):
        db = None
        try:
            db_path = resolve_path(cfg("events.db", "runtime/events.db"))
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            db = sqlite3.connect(db_path)
            db.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, time_str TEXT, phase TEXT, kind TEXT, level TEXT,
                    title TEXT, message TEXT, duration REAL, snapshot TEXT, details TEXT,
                    review INTEGER DEFAULT NULL
                )"""
            )
            db.commit()
        except Exception as e:
            print(f"[Event] 事件库初始化失败（仅影响留痕，不影响告警）: {e}")
            db = None

        while True:
            item = self._io_q.get()
            try:
                if item[0] == "webhook":
                    _, url, content = item
                    body = json.dumps({"msgtype": "text", "text": {"content": content}}).encode("utf-8")
                    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=5).read()
                elif item[0] == "event":
                    _, phase, ev, frames = item
                    snap_path = self._save_snapshot(ev, frames)
                    if db is not None:
                        db.execute(
                            "INSERT INTO events (ts, time_str, phase, kind, level, title, message, duration, snapshot, details)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (time.time(), ev.get("time_str"), phase, ev.get("kind"), ev.get("level"),
                             ev.get("title"), ev.get("message"), ev.get("duration"), snap_path,
                             json.dumps(ev.get("details", {}), ensure_ascii=False, default=str)),
                        )
                        db.commit()
            except Exception as e:
                print(f"[Event] 后台写入失败: {e}")

    def _save_snapshot(self, ev: dict, frames) -> Optional[str]:
        pano, desk = frames
        if pano is None and desk is None:
            return None
        try:
            import cv2
            import numpy as np
            imgs = [f for f in (pano, desk) if f is not None and getattr(f, "size", 0) > 0]
            if not imgs:
                return None
            h = min(i.shape[0] for i in imgs)
            imgs = [cv2.resize(i, (int(i.shape[1] * h / i.shape[0]), h)) for i in imgs]
            combined = np.hstack(imgs)
            folder = resolve_path(cfg("events.snapshot_dir", "runtime/snapshots"))
            day = datetime.now().strftime("%Y%m%d")
            os.makedirs(os.path.join(folder, day), exist_ok=True)
            name = f"{datetime.now().strftime('%H%M%S_%f')[:-3]}_{ev.get('kind', 'event')}.jpg"
            path = os.path.join(folder, day, name)
            ok, buf = cv2.imencode(".jpg", combined, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                # 用 Python 写文件，兼容 Windows 中文路径（cv2.imwrite 不支持）
                with open(path, "wb") as f:
                    f.write(buf.tobytes())
                return path
        except Exception as e:
            print(f"[Event] 截图保存失败: {e}")
        return None


events = EventCenter()
