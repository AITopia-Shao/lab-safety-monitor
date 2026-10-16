"""
实验台小屏 Kiosk 界面（4–7 寸触摸屏，香橙派本机运行）
src/ui/kiosk.py

定位：实验员身边"随手看、随手点"的监控台，而不是中控大屏。
  · 一眼看懂：顶栏状态（颜色 + √/▲/■ 形状 + 文字三重编码）、热源大数字与干烧倒计时、四项防护装备；
  · 戴手套也能点：按钮 ≥ 64px（800×480 基准），只有 3 个页签 + 3 个常驻操作；
  · 紧急时整屏接管：断电锁定后红色全屏提示"排查后按下位机复位键"；
  · 分辨率自适应：以 800×480 为设计基准按短边等比缩放（480×320 ~ 1024×600），竖屏自动改为上下布局；
    config.yaml → ui.kiosk_size: [宽, 高] 可手动指定（PC 调试时常用）。
  · 性能：无动画；画面页不可见时不转换图像；状态 2 Hz 刷新。
风格与 PC 大窗口、Web 总后台共用同一套色值（T 类）与 Zpix 像素字体。
"""
import socket
import time
from collections import deque
from typing import Optional

import cv2

try:
    from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
    from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
                                   QStackedWidget, QScrollArea, QFrame, QApplication, QSizePolicy)
    from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QPolygonF
except ImportError:
    from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
    from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
                                 QStackedWidget, QScrollArea, QFrame, QApplication, QSizePolicy)
    from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QPolygonF

from src.config import cfg
from src.core.events import events
from src.ui.main_window import T, STATE_ICON, ink_on, Card, Chip, get_qss

STATE_COLOR = {"NORMAL": T.SAFE, "WARNING": T.WARN, "CRITICAL": T.DANGER}
STATE_SOFT = {"NORMAL": T.PAPER, "WARNING": T.WARN_SOFT, "CRITICAL": T.DANGER_SOFT}
LEVEL_TAG = {"NOTICE": "提醒", "WARNING": "▲ 警告", "CRITICAL": "■ 紧急", "INFO": "信息"}
PO_TEXT = {"pending": "断电指令已下发，等待下位机确认", "acked": "下位机已确认断电，继电器已断开",
           "failed_no_link": "串口未连接 —— 请立即人工切断加热电源！",
           "failed_no_ack": "下位机未应答 —— 请立即人工切断加热电源！"}


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class Spark(QWidget):
    """热源温度迷你曲线：实测（实线）+ 世界模型预测（虚线）+ 干烧阈值（红虚线）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hist = deque(maxlen=120)
        self.forecast = []
        self.threshold = 120.0

    def set_data(self, temp, forecast, threshold):
        if temp is None:
            self.hist.clear()
        else:
            self.hist.append(float(temp))
        self.forecast = [p[1] for p in (forecast or [])]
        self.threshold = float(threshold)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        vals = list(self.hist) + self.forecast + [self.threshold, 20.0]
        lo, hi = min(vals) - 5, max(vals) + 5
        y = lambda v: 3 + (h - 6) * (1 - (v - lo) / (hi - lo or 1))
        pen = QPen(QColor(T.DANGER), 1, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(0, int(y(self.threshold)), w, int(y(self.threshold)))
        split = w * 0.72

        def poly(data, x0, x1, dash):
            if len(data) < 2:
                return
            pts = QPolygonF([QPointF(x0 + i * (x1 - x0) / (len(data) - 1), y(v)) for i, v in enumerate(data)])
            p.setPen(QPen(QColor(T.PRIMARY), 2, Qt.PenStyle.DashLine if dash else Qt.PenStyle.SolidLine))
            p.drawPolyline(pts)

        poly(list(self.hist), 0, split, False)
        if self.hist:
            poly(self.forecast, split, w, True)
        p.end()


class HoldButton(QPushButton):
    """长按确认按钮：按住 hold_s 秒才触发，防误触"""

    def __init__(self, text, hold_s, on_fire, parent=None):
        super().__init__(text, parent)
        self._text, self._hold, self._fire = text, hold_s, on_fire
        self._t0 = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.pressed.connect(self._start)
        self.released.connect(self._cancel)

    def _start(self):
        self._t0 = time.time()
        self._timer.start(100)

    def _cancel(self):
        self._timer.stop()
        self._t0 = None
        self.setText(self._text)

    def _tick(self):
        if self._t0 is None:
            return
        left = self._hold - (time.time() - self._t0)
        if left <= 0:
            self._timer.stop()
            self._t0 = None
            self.setText(self._text)
            self._fire()
        else:
            self.setText(f"继续按住 {left:.1f}s")


class KioskWindow(QWidget):
    def __init__(self, engine, fullscreen: bool = True):
        super().__init__()
        self.engine = engine
        self.setWindowTitle("实验台安全终端")
        size = cfg("ui.kiosk_size", None)
        screen = QApplication.primaryScreen()
        if size and len(size) == 2:
            W, H = int(size[0]), int(size[1])
        elif fullscreen and screen is not None:
            g = screen.geometry()
            W, H = g.width(), g.height()
        else:
            W, H = 800, 480
        self.portrait = H > W
        base_w, base_h = (480, 800) if self.portrait else (800, 480)
        self.k = max(0.6, min(W / base_w, H / base_h))
        self.setStyleSheet(get_qss() + f"QWidget#kiosk {{ background: {T.BG}; }}")
        self.setObjectName("kiosk")
        self._show_page = 0
        self._cam = "pano"
        self._last_frame_ts = 0.0
        self._overlay_snooze = 0.0
        self._ev_key = None
        self._build()
        self.engine.frames_ready.connect(self._on_frames)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        if fullscreen and not size:
            self.showFullScreen()
            self.setCursor(Qt.CursorShape.BlankCursor) if cfg("ui.kiosk_hide_cursor", True) else None
        else:
            self.resize(W, H)
            self.show()
        self.refresh()

    # ------------------------------------------------------------------
    def px(self, n) -> int:
        return max(1, int(n * self.k))

    def _lbl(self, text="", size=16, color=T.INK, bold=True):
        l = QLabel(text)
        l.setStyleSheet(f"color: {color}; font-size: {self.px(size)}px; background: transparent;")
        return l

    def _btn(self, text, size=18, h=64):
        b = QPushButton(text)
        b.setMinimumHeight(self.px(h))
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        b.setStyleSheet(f"font-size: {self.px(size)}px;")
        return b

    def _build(self):
        root = QVBoxLayout(self)
        m = self.px(8)
        root.setContentsMargins(m, m, m, m)
        root.setSpacing(self.px(8))

        # ---- 顶栏 ----
        self.top = Card(offset=self.px(4), border_w=3, padding=(0, 0, 0, 0))
        tl = QHBoxLayout()
        tl.setContentsMargins(0, 0, self.px(8), 0)
        tl.setSpacing(self.px(10))
        self.badge = QLabel("√ SAFE")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setMinimumWidth(self.px(150))
        self.badge.setMinimumHeight(self.px(52))
        self.state_lbl = self._lbl("正常守护中", 20)
        self.state_sub = self._lbl("", 12, T.DIM)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(self.state_lbl)
        col.addWidget(self.state_sub)
        self.links = self._lbl("", 12, T.DIM)
        self.clock = self._lbl("", 18)
        self.btn_set = QPushButton("设置")
        self.btn_set.setFixedSize(self.px(72), self.px(52))
        self.btn_set.setStyleSheet(f"font-size: {self.px(16)}px; padding: 0;")
        self.btn_set.clicked.connect(lambda: self._goto(3))
        tl.addWidget(self.badge)
        tl.addLayout(col, 1)
        tl.addWidget(self.links)
        tl.addWidget(self.clock)
        tl.addWidget(self.btn_set)
        self.top.body.addLayout(tl)
        root.addWidget(self.top)

        # ---- 页面 ----
        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_guard())
        self.stack.addWidget(self._page_cam())
        self.stack.addWidget(self._page_events())
        self.stack.addWidget(self._page_settings())
        root.addWidget(self.stack, 1)

        # ---- 底栏：3 个页签 + 3 个常驻操作 ----
        bar = QHBoxLayout() if not self.portrait else QGridLayout()
        self.tab_btns = []
        for i, name in enumerate(["守护", "画面", "事件"]):
            b = self._btn(name)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, i=i: self._goto(i))
            self.tab_btns.append(b)
        self.btn_mute = self._btn("静音 60s", 16)
        self.btn_mute.clicked.connect(lambda: self.engine.mute(60))
        self.btn_call = self._btn("呼叫管理员", 16)
        self.btn_call.clicked.connect(self._call)
        self.btn_cut = HoldButton("长按 2s 断电", 2.0, lambda: self.engine.cut_power("实验台小屏"))
        self.btn_cut.setMinimumHeight(self.px(64))
        self.btn_cut.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_cut.setStyleSheet(f"QPushButton {{ font-size: {self.px(16)}px; background: {T.DANGER}; color: {T.PAPER};"
                                   f" border: 2px solid {T.DANGER_DEEP}; }}")
        items = self.tab_btns + [self.btn_mute, self.btn_call, self.btn_cut]
        if self.portrait:
            for i, b in enumerate(items):
                bar.addWidget(b, i // 3, i % 3)
        else:
            for b in items:
                bar.addWidget(b)
        bar.setSpacing(self.px(6))
        root.addLayout(bar)
        self._goto(0)

        # ---- 紧急整屏接管 ----
        self.overlay = QFrame(self)
        self.overlay.setStyleSheet(f"QFrame {{ background: {T.DANGER}; }} QLabel {{ color: {T.PAPER}; background: transparent; }}")
        ol = QVBoxLayout(self.overlay)
        ol.setContentsMargins(self.px(24), self.px(20), self.px(24), self.px(20))
        ol.setSpacing(self.px(10))
        self.ov_title = self._lbl("■ 已断电锁定", 40, T.PAPER)
        self.ov_reason = self._lbl("", 20, T.PAPER)
        self.ov_reason.setWordWrap(True)
        self.ov_po = self._lbl("", 16, T.PAPER)
        self.ov_po.setWordWrap(True)
        self.ov_ai = self._lbl("", 14, T.PAPER)
        self.ov_ai.setWordWrap(True)
        self.ov_hint = self._lbl("排查危险后，到现场长按控制盒【复位键】恢复供电\n（任何软件、远程操作都不能恢复供电）", 18, T.PAPER)
        self.ov_hint.setWordWrap(True)
        row = QHBoxLayout()
        b1 = self._btn("静音 30s", 18)
        b1.clicked.connect(lambda: self.engine.mute(30))
        b2 = self._btn("查看详情（30 秒）", 18)
        b2.clicked.connect(self._snooze_overlay)
        for b in (b1, b2):
            b.setStyleSheet(f"QPushButton {{ font-size: {self.px(18)}px; background: {T.PAPER}; color: {T.DANGER_DEEP};"
                            f" border: 3px solid {T.DANGER_DEEP}; }}")
            row.addWidget(b)
        for w in (self.ov_title, self.ov_reason, self.ov_po, self.ov_ai):
            ol.addWidget(w)
        ol.addStretch(1)
        ol.addWidget(self.ov_hint)
        ol.addLayout(row)
        self.overlay.hide()

    def _page_guard(self):
        page = QWidget()
        lay = QHBoxLayout(page) if not self.portrait else QVBoxLayout(page)
        lay.setContentsMargins(0, 0, self.px(4), self.px(4))
        lay.setSpacing(self.px(10))

        self.heat = Card(offset=self.px(4), border_w=3, padding=(self.px(12), self.px(8), self.px(12), self.px(8)))
        self.heat.body.setSpacing(self.px(2))
        self.heat_hdr = self._lbl("热源表面温度", 13, T.DIM)
        vrow = QHBoxLayout()
        self.heat_val = self._lbl("--.-", 72)
        self.heat_unit = self._lbl("℃", 24)
        self.heat_tag = Chip("未接入", T.INK, T.PRIMARY_SOFT, size=self.px(15))
        vrow.addWidget(self.heat_val)
        vrow.addWidget(self.heat_unit, 0, Qt.AlignmentFlag.AlignBottom)
        vrow.addStretch(1)
        vrow.addWidget(self.heat_tag, 0, Qt.AlignmentFlag.AlignTop)
        self.spark = Spark()
        self.spark.setMinimumHeight(self.px(56))
        self.heat_eta = self._lbl("—", 16)
        self.heat_eta.setWordWrap(True)
        self.heat.body.addWidget(self.heat_hdr)
        self.heat.body.addLayout(vrow)
        self.heat.body.addWidget(self.spark, 1)
        self.heat.body.addWidget(self.heat_eta)
        lay.addWidget(self.heat, 3)

        right = QVBoxLayout()
        right.setSpacing(self.px(8))
        ppe_card = Card(offset=self.px(4), padding=(self.px(10), self.px(6), self.px(10), self.px(6)))
        self.presence = self._lbl("人员：—", 16)
        ppe_card.body.addWidget(self.presence)
        grid = QGridLayout()
        grid.setSpacing(self.px(6))
        self.ppe_lbls = []
        for i in range(4):
            l = QLabel("")
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l.setMinimumHeight(self.px(40))
            grid.addWidget(l, i // 2, i % 2)
            self.ppe_lbls.append(l)
        ppe_card.body.addLayout(grid)
        right.addWidget(ppe_card, 3)

        env = Card(offset=self.px(4), padding=(self.px(10), self.px(6), self.px(10), self.px(6)))
        self.env_lbl = self._lbl("环境 --℃ · 湿度 --% · 烟雾 --", 14)
        self.env_lbl.setWordWrap(True)
        self.act_lbl = self._lbl("排风 — · 加湿 —", 13, T.DIM)
        env.body.addWidget(self.env_lbl)
        env.body.addWidget(self.act_lbl)
        right.addWidget(env, 2)
        lay.addLayout(right, 2)
        return page

    def _page_cam(self):
        page = Card(offset=self.px(4), padding=(0, 0, 0, 0))
        page.body.setSpacing(0)
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {T.PRIMARY};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(self.px(10), self.px(4), self.px(8), self.px(4))
        self.cam_title = self._lbl("Camera A · 全景（点画面切换）", 15, T.PAPER)
        self.cam_chip = Chip("等待画面", T.PRIMARY, T.PAPER, size=self.px(13))
        hl.addWidget(self.cam_title, 1)
        hl.addWidget(self.cam_chip)
        self.cam_view = QLabel("NO SIGNAL")
        self.cam_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cam_view.setStyleSheet(f"background: {T.SCREEN}; color: {T.PRIMARY_MID}; font-size: {self.px(16)}px;")
        self.cam_view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.cam_view.mousePressEvent = lambda _e: self._toggle_cam()
        page.body.addWidget(hdr)
        page.body.addWidget(self.cam_view, 1)
        return page

    def _page_events(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setStyleSheet(f"background: {T.BG};")
        self.ev_lay = QVBoxLayout(inner)
        self.ev_lay.setSpacing(self.px(8))
        self.ev_lay.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll)
        return page

    def _page_settings(self):
        page = Card(offset=self.px(4), padding=(self.px(16), self.px(12), self.px(16), self.px(12)))
        self.set_lbl = self._lbl("", 18)
        self.set_lbl.setWordWrap(True)
        self.set_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        page.body.addWidget(self._lbl("▌总后台访问", 18, T.PRIMARY))
        page.body.addWidget(self.set_lbl)
        page.body.addStretch(1)
        back = self._btn("返回守护页", 18)
        back.clicked.connect(lambda: self._goto(0))
        page.body.addWidget(back)
        return page

    # ------------------------------------------------------------------
    def _goto(self, i):
        self._show_page = i
        self.stack.setCurrentIndex(i)
        for j, b in enumerate(self.tab_btns):
            b.setChecked(j == i)
        if i == 3:
            from src.web.server import web_token, web_port
            port = web_port()
            self.set_lbl.setText(f"浏览器打开：\nhttp://{_local_ip()}:{port}\n\n口令：{web_token()}\n\n"
                                 f"大屏可查看两路画面、调整参数，使用安全助手、温度预测和环境控制。")

    def _toggle_cam(self):
        self._cam = "desk" if self._cam == "pano" else "pano"
        self.cam_title.setText("Camera B · 台面（点画面切换）" if self._cam == "desk" else "Camera A · 全景（点画面切换）")
        self._last_frame_ts = 0

    def _call(self):
        self.engine.call_admin("实验员")
        self.btn_call.setText("已呼叫 √")
        QTimer.singleShot(5000, lambda: self.btn_call.setText("呼叫管理员"))

    def _snooze_overlay(self):
        self._overlay_snooze = time.time() + 30
        self.overlay.hide()

    def resizeEvent(self, e):
        self.overlay.setGeometry(self.rect())
        super().resizeEvent(e)

    def _on_frames(self, pano, desk, *_):
        if self._show_page != 1:
            return
        now = time.time()
        if now - self._last_frame_ts < 0.1:   # 小屏画面 10 fps 足够
            return
        self._last_frame_ts = now
        img = pano if self._cam == "pano" else desk
        if img is None:
            return
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        q = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        pm = QPixmap.fromImage(q).scaled(self.cam_view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.FastTransformation)
        self.cam_view.setPixmap(pm)

    # ------------------------------------------------------------------
    def refresh(self):
        s = self.engine.snapshot()
        state = s["state"]
        color = STATE_COLOR.get(state, T.SAFE)
        self.badge.setText(f"{STATE_ICON.get(state, '')} {({'NORMAL': 'SAFE'}).get(state, state)}")
        self.badge.setStyleSheet(f"background: {color}; color: {ink_on(color)}; font-size: {self.px(24)}px;")
        self.top.set_colors(bg=STATE_SOFT.get(state), border=color if state != "NORMAL" else T.PRIMARY,
                            shadow=color if state != "NORMAL" else T.PRIMARY)
        txt = {"NORMAL": T.INK, "WARNING": T.WARN_TEXT, "CRITICAL": T.DANGER_DEEP}[state]
        self.state_lbl.setText(s["state_text"])
        self.state_lbl.setStyleSheet(f"color: {txt}; font-size: {self.px(20)}px;")
        act = s["active_events"]
        self.state_sub.setText(" · ".join(e["title"] for e in act[:2]) if act else "无活动告警")
        self.state_sub.setStyleSheet(f"color: {txt}; font-size: {self.px(12)}px;")
        mcu = "●" if s["link"]["mcu"] else "○"
        cam = "●" if s["vision"]["ok"] else "○"
        self.links.setText(f"{mcu}下位机 {cam}视觉")
        self.clock.setText(time.strftime("%H:%M"))
        mute_left = int(s["mute_until"] - time.time()) if s["mute_until"] else 0
        self.btn_mute.setText(f"静音中 {mute_left}s" if mute_left > 0 else "静音 60s")

        self._refresh_guard(s)
        if self._show_page == 2:
            self._refresh_events()

        # 紧急整屏接管
        if s["latched"] and time.time() > self._overlay_snooze:
            self.ov_reason.setText(f"原因：{s['latch_reason']}")
            demo = s["telemetry"].get("source") != "hardware" and not s["link"]["mcu"]
            self.ov_po.setText("未接入下位机：软件已模拟断电锁定（演示模式）" if demo
                               else PO_TEXT.get(s["power_off_state"], "断电处理中"))
            self.ov_ai.setText(self._advice(events.history(8, "WARNING")))
            self.overlay.setGeometry(self.rect())
            self.overlay.show()
            self.overlay.raise_()
        elif not s["latched"]:
            self.overlay.hide()

    def _refresh_guard(self, s):
        t = s["telemetry"]
        wm = s["world_model"]
        r = s["rules"]
        obj = t.get("obj_temp")
        thr = r["dry_burn_c"]
        self.heat_hdr.setText(f"热源表面温度 · 干烧阈值 {thr:.0f}℃")
        self.heat_val.setText("--.-" if obj is None else f"{obj:.1f}")
        lv, tag, eta = "ok", "√ 常温", "未在加热 · 无干烧风险"
        if obj is None:
            tag, eta = "未接入", "未接入热源测温"
        elif obj >= thr:
            lv, tag, eta = "bad", "■ 干烧", "已超过干烧阈值"
        elif obj >= r["heating_on_c"]:
            tag = "▲ " + (wm.get("phase_text") or "加热中")
            if wm.get("eta_s") is not None:
                eta = f"越过沸点仍在升温 · 约 {wm['eta_s']:.0f} 秒后干烧"
                if wm["eta_s"] <= r["predict_eta_warn_s"]:
                    lv = "warn"
            elif wm.get("eta_boil_s") is not None:
                eta = f"预计约 {wm['eta_boil_s']:.0f} 秒后沸腾"
            elif wm.get("phase") == "boiling":
                eta = f"沸腾 {wm['plateau_s']:.0f} 秒 · 请留意液面"
            elif 90 <= obj < 104:
                eta = "接近沸点"
            elif wm.get("t_inf") is not None and wm["t_inf"] < 90:
                eta = f"将稳定在约 {wm['t_inf']:.0f}℃"
            else:
                eta = "温度预测采样中…"
        border = {"ok": T.PRIMARY, "warn": T.WARN, "bad": T.DANGER}[lv]
        bg = {"ok": T.PAPER, "warn": T.WARN_SOFT, "bad": T.DANGER_SOFT}[lv]
        self.heat.set_colors(bg=bg, border=border, shadow=border)
        if obj is None:
            self.heat_tag.set(tag, T.INK, T.PRIMARY_SOFT)
        else:
            tbg = {"ok": T.SAFE_SOFT, "warn": T.WARN, "bad": T.DANGER}[lv]
            self.heat_tag.set(tag, {"ok": T.SAFE, "warn": T.WARN_INK, "bad": T.PAPER}[lv], tbg)
        self.heat_eta.setText(eta)
        self.heat_eta.setStyleSheet(f"color: {({'ok': T.INK, 'warn': T.WARN_TEXT, 'bad': T.DANGER_DEEP})[lv]};"
                                    f" font-size: {self.px(16)}px;")
        self.spark.set_data(obj, wm.get("forecast"), thr)

        v = s["vision"]
        if v["person_present"] is None:
            pres = "人员：画面无效"
        elif v["unattended"]:
            uh = next((e for e in s["active_events"] if e["kind"] == "unattended_heating"), None)
            left = uh["message"].split("，")[-1] if uh and "后将自动断电" in uh["message"] else ""
            pres = "▲ 无人值守" + (f" · 加热中，{left}" if left else "")
        else:
            pres = "√ 人员在岗"
        self.presence.setText(pres)
        icon = {"ok": "√", "bad": "■", "unknown": "?"}
        for lbl, item in zip(self.ppe_lbls, v["ppe"]):
            st = item["status"]
            fg, bgc, bd = {"ok": (T.SAFE, T.PAPER, T.SAFE), "bad": (T.PAPER, T.DANGER, T.DANGER_DEEP),
                           "unknown": (T.DIM, T.PRIMARY_SOFT, T.PRIMARY_MID)}[st]
            lbl.setText(f"{icon[st]} {item['name']}")
            lbl.setStyleSheet(f"color: {fg}; background: {bgc}; border: 2px solid {bd}; font-size: {self.px(16)}px;")

        def f(x, d, u):
            return "--" if x is None else f"{x:.{d}f}{u}"
        smoke = t.get("smoke")
        self.env_lbl.setText(f"环境 {f(t.get('temp'), 1, '℃')} · 湿度 {f(t.get('humi'), 0, '%')} · "
                             f"烟雾 {smoke if smoke is not None else '--'}")
        env = s.get("env") or {}
        if env:
            self.act_lbl.setText(f"排风 {env.get('fan_pwm', 0)}% · 加湿 {'开' if env.get('humid_on') else '关'}"
                                 f" · {'手动' if env.get('mode') == 'manual' else '自动'}")

    @staticmethod
    def _advice(hist) -> str:
        """从事件里取第一条处理建议的"处置"一行"""
        for e in hist:
            note = e.get("agent_note") or ""
            line = next((l for l in note.split("\n") if l.startswith("处置")), "")
            if line:
                return "建议：" + line.split("：", 1)[-1]
        return ""

    def _refresh_events(self):
        hist = [e for e in events.history(10, "NOTICE")]
        key = tuple((e["uid"], bool(e.get("agent_note")), e.get("review")) for e in hist)
        if key == self._ev_key:
            return
        self._ev_key = key
        while self.ev_lay.count() > 1:
            w = self.ev_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for e in hist:
            color = {"WARNING": T.WARN, "CRITICAL": T.DANGER}.get(e["level"], T.PRIMARY)
            card = Card(offset=self.px(3), border=color, shadow=color,
                        padding=(self.px(10), self.px(6), self.px(10), self.px(6)))
            card.body.setSpacing(self.px(2))
            card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)   # 卡片多时滚动，而不是被压扁
            head = QHBoxLayout()
            tag = Chip(LEVEL_TAG.get(e["level"], e["level"]), ink_on(color), color, size=self.px(13))
            head.addWidget(tag)
            head.addWidget(self._lbl(e["title"], 17), 1)
            head.addWidget(self._lbl(e["time_str"], 13, T.DIM))
            card.body.addLayout(head)
            msg = self._lbl(e["message"], 13, T.DIM)
            msg.setWordWrap(True)
            card.body.addWidget(msg)
            note = e.get("agent_note")
            if note:
                ai = self._lbl(self._advice([e]) or ("建议：" + note.split("\n")[0]), 14, T.INK)
                ai.setWordWrap(True)
                ai.setStyleSheet(ai.styleSheet() + f"background: {T.PRIMARY_SOFT}; padding: 4px;")
                card.body.addWidget(ai)
            # 换行标签在显示前算不出准确高度，这里按字数估算最小高度，卡片多时滚动而不是被压扁
            per_line = max(10, (self.width() - self.px(70)) // self.px(13))
            lines = -(-len(e["message"]) // per_line)
            h = self.px(28) + self.px(34) + lines * self.px(19)
            if note:
                h += -(-len(ai.text()) // max(10, per_line * 13 // 14)) * self.px(21) + self.px(12)
            card.setMinimumHeight(h)
            self.ev_lay.insertWidget(self.ev_lay.count() - 1, card)
