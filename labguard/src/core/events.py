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
import uuid
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
        # 界面事件流：事件开始 / 升级 / 一次性告警时推入，界面定时拉取生成事件卡片
        self._feed = deque(maxlen=200)
        self._feed_seq = 0
        # 事件历史（供 Web 总后台 / 小屏 / Agent 查询）：uid -> 事件摘要（不含画面）
        self._history = deque(maxlen=300)
        self._by_uid: Dict[str, Dict[str, Any]] = {}
        # 事件监听者 fn(rec)：事件开始 / 升级 / 一次性事件时调用；必须立即返回（只做入队）
        self._listeners = []
        # 刚解除的事件：kind -> (解除时间, 事件)。识别结果时有时无时，避免同一件事反复刷屏
        self._ended: Dict[str, tuple] = {}
        # 语音静音截止时间（小屏「静音」按钮）；只影响语音，不影响灯光、告警与断电
        self.mute_until = 0.0

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
            if active and cur is None and kind in self._ended:
                t_end, old = self._ended.pop(kind)
                if now - t_end < float(cfg("events.retrigger_s", 10.0)) and                         LEVEL_RANK.get(level, 0) <= LEVEL_RANK.get(old["level"], 0):
                    old["message"] = message
                    self._active[kind] = old
                    rec = self._by_uid.get(old.get("uid"))
                    if rec is not None:
                        rec["ended"] = False
                    return None
            if active and cur is None:
                ev = {
                    "kind": kind, "level": level, "title": title, "message": message,
                    "details": details or {}, "since": now, "last_remind": now,
                    "time_str": datetime.fromtimestamp(now).strftime("%H:%M:%S"),
                    "uid": uuid.uuid4().hex[:12],
                }
                self._active[kind] = ev
                self._feed_push(ev, snapshot)
                self._push_line(f"[{ev['time_str']}][{LEVEL_TAG.get(level, level)}] {title}：{message}")
                self._enqueue_io("start", ev, snapshot)
                self._act_on_start(ev)
                self._refresh_light()
                return dict(ev)
            if active and cur is not None:
                # 等级升级（如 WARNING → CRITICAL）视为新的事件开始
                if LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(cur["level"], 0):
                    cur.update({"level": level, "title": title, "message": message,
                                "details": details or {}, "last_remind": now,
                                "uid": uuid.uuid4().hex[:12],
                                "time_str": datetime.fromtimestamp(now).strftime("%H:%M:%S")})
                    self._feed_push(cur, snapshot)
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
                self._ended[kind] = (now, cur)
                dur = now - cur["since"]
                rec = self._by_uid.get(cur.get("uid"))
                if rec is not None:
                    rec["ended"] = True
                    rec["duration"] = round(dur, 1)
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
              "time_str": datetime.fromtimestamp(now).strftime("%H:%M:%S"),
              "uid": uuid.uuid4().hex[:12]}
        with self._lock:
            self._feed_push(ev, snapshot)
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

    def drain_feed(self, after_seq: int):
        """返回序号大于 after_seq 的事件（含当时的标注画面），供界面生成事件卡片"""
        with self._lock:
            return [(seq, dict(ev)) for seq, ev in self._feed if seq > after_seq]

    def set_review(self, uid: str, correct: bool):
        """把人工评审结果（1=正确，0=误报）写回事件库"""
        if uid:
            with self._lock:
                if uid in self._by_uid:
                    self._by_uid[uid]["review"] = 1 if correct else 0
            self._enqueue_raw(("review", uid, 1 if correct else 0))

    def set_agent_note(self, uid: str, text: str):
        """Agent 研判结果写回事件（内存 + 事件库），小屏与总后台随即可见"""
        if not uid:
            return
        with self._lock:
            if uid in self._by_uid:
                self._by_uid[uid]["agent_note"] = text
        self._enqueue_raw(("agent_note", uid, text))

    def add_listener(self, fn):
        if fn not in self._listeners:
            self._listeners.append(fn)

    def history(self, n: int = 50, min_level: str = "INFO"):
        """最近的事件（新的在前），不含画面"""
        rank = LEVEL_RANK.get(min_level, 0)
        with self._lock:
            out = [dict(e) for e in reversed(self._history) if LEVEL_RANK.get(e["level"], 0) >= rank]
        return out[:n]

    def get_event(self, uid: str) -> Optional[dict]:
        with self._lock:
            e = self._by_uid.get(uid)
            return dict(e) if e else None

    def stats(self) -> Dict[str, int]:
        """事件统计（不含 INFO）：总数、各等级数量、已复核数、复核为正确的数量"""
        with self._lock:
            out = {"total": 0, "CRITICAL": 0, "WARNING": 0, "NOTICE": 0, "reviewed": 0, "correct": 0}
            for e in self._history:
                if e["level"] == "INFO":
                    continue
                out["total"] += 1
                out[e["level"]] = out.get(e["level"], 0) + 1
                if e.get("review") is not None:
                    out["reviewed"] += 1
                    out["correct"] += int(e["review"])
            return out

    def _feed_push(self, ev: dict, snapshot: bool):
        self._feed_seq += 1
        item = dict(ev)
        item["frames"] = self._latest_frames if snapshot else (None, None)
        self._feed.append((self._feed_seq, item))
        rec = {k: ev.get(k) for k in ("uid", "kind", "level", "title", "message", "time_str", "since")}
        rec["details"] = dict(ev.get("details") or {})
        rec.update({"seq": self._feed_seq, "snapshot": None, "agent_note": None,
                    "review": None, "ended": False})
        if len(self._history) == self._history.maxlen:
            self._by_uid.pop(self._history[0]["uid"], None)
        self._history.append(rec)
        self._by_uid[rec["uid"]] = rec
        for fn in list(self._listeners):
            try:
                fn(dict(rec))
            except Exception as e:
                print(f"[Event] 监听者异常: {e}")

    # ------------------------------------------------------------------
    # 干预动作
    # ------------------------------------------------------------------
    def _act_on_start(self, ev: dict):
        self._play_voice(ev["kind"])
        self._notify_webhook(ev)

    def _play_voice(self, kind: str):
        if time.time() < self.mute_until:
            return
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
                    review INTEGER DEFAULT NULL, uid TEXT
                )"""
            )
            cols = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            if "uid" not in cols:
                db.execute("ALTER TABLE events ADD COLUMN uid TEXT")
            if "agent_note" not in cols:
                db.execute("ALTER TABLE events ADD COLUMN agent_note TEXT")
            db.commit()
        except Exception as e:
            print(f"[Event] 事件库初始化失败（仅影响留痕，不影响告警）: {e}")
            db = None

        while True:
            item = self._io_q.get()
            try:
                if item[0] == "review":
                    if db is not None:
                        db.execute("UPDATE events SET review=? WHERE uid=?", (item[2], item[1]))
                        db.commit()
                elif item[0] == "agent_note":
                    if db is not None:
                        db.execute("UPDATE events SET agent_note=? WHERE uid=?", (item[2], item[1]))
                        db.commit()
                elif item[0] == "webhook":
                    _, url, content = item
                    body = json.dumps({"msgtype": "text", "text": {"content": content}}).encode("utf-8")
                    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=5).read()
                elif item[0] == "event":
                    _, phase, ev, frames = item
                    snap_path = self._save_snapshot(ev, frames)
                    if snap_path and ev.get("uid"):
                        with self._lock:
                            rec = self._by_uid.get(ev["uid"])
                            if rec is not None and not rec.get("snapshot"):
                                rec["snapshot"] = snap_path
                    if db is not None:
                        db.execute(
                            "INSERT INTO events (ts, time_str, phase, kind, level, title, message, duration, snapshot, details, uid)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (time.time(), ev.get("time_str"), phase, ev.get("kind"), ev.get("level"),
                             ev.get("title"), ev.get("message"), ev.get("duration"), snap_path,
                             json.dumps(ev.get("details", {}), ensure_ascii=False, default=str),
                             ev.get("uid") if phase != "end" else None),
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
