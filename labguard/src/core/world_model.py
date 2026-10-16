"""
热力学世界模型（板载轻量版）
src/core/world_model.py

作用：根据热源表面温度的历史，预测"接下来会怎样"——未来 120 秒的温度轨迹、多久后到达干烧阈值。
它只产生预测与风险分，供规则 R9（提前预警，不断电）、界面与 Agent 使用。

模型分两段（实验加热的真实物理过程）：
  1) 升温段：一阶热平衡 dT/dt = (T∞ - T) / τ。
     对最近一段数据做 dT/dt 关于 T 的线性回归，得到渐近温度 T∞ 和时间常数 τ，
     由解析解 T(t) = T∞ - (T∞ - T0)·e^(-t/τ) 外推轨迹与到达阈值的时间（ETA）。
     若 T∞ 低于干烧阈值，则判定"按当前趋势不会干烧"。拟合不稳定时退化为线性外推。
  2) 沸腾段：水在常压下会停在 95–103℃ 附近很久，直到烧干才突然升温，一阶模型无法预测这个拐点。
     因此单独检测"沸腾平台"，平台持续越久 → 液体越少 → 风险越高。
  物理约束：容器里还有液体时，温度到沸点就会停住。所以温度低于沸点时只预测"多久后沸腾"，
  不外推干烧 ETA（否则正常烧水也会被误报为"即将干烧"）；温度越过沸点仍在上升，才说明液体已烧干，
  这时的干烧 ETA 才有意义。

计算量：每秒一次、几十个点的最小二乘，适合香橙派。
"""
import math
import threading
import time
from collections import deque
from typing import Optional, Dict, Any, List, Tuple

from src.config import cfg


class ThermalWorldModel:
    HORIZON_S = 120.0

    def __init__(self):
        self._lock = threading.Lock()
        self._hist: deque = deque(maxlen=400)   # (t, T)
        self._plateau_since: Optional[float] = None
        self._last: Dict[str, Any] = self._empty()

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {"phase": "no_sensor", "temp": None, "slope": None, "t_inf": None, "tau": None,
                "eta_s": None, "eta_boil_s": None, "will_dry_burn": False, "plateau_s": 0.0, "risk": 0.0,
                "forecast": [], "model": None, "ts": time.time()}

    def reset(self):
        with self._lock:
            self._hist.clear()
            self._plateau_since = None
            self._last = self._empty()

    # ------------------------------------------------------------------
    def update(self, obj_temp: Optional[float], now: Optional[float] = None,
               unattended: bool = False) -> Dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            if obj_temp is None:
                self._hist.clear()
                self._plateau_since = None
                self._last = self._empty()
                return dict(self._last)
            if self._hist and now - self._hist[-1][0] < 0.4:
                return dict(self._last)
            self._hist.append((now, float(obj_temp)))
            self._last = self._predict(now, float(obj_temp), unattended)
            return dict(self._last)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(self._last)
            if self._hist:
                t_end = self._hist[-1][0]
                out["history"] = [[round(t - t_end, 1), round(v, 1)] for t, v in self._hist if t_end - t <= 120]
            else:
                out["history"] = []
            return out

    # ------------------------------------------------------------------
    def _window(self, now: float, span: float) -> List[Tuple[float, float]]:
        return [(t, v) for t, v in self._hist if now - t <= span]

    @staticmethod
    def _linfit(xs, ys) -> Optional[Tuple[float, float]]:
        n = len(xs)
        if n < 3:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx < 1e-9:
            return None
        k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        return k, my - k * mx

    def _predict(self, now: float, temp: float, unattended: bool) -> Dict[str, Any]:
        dry_c = float(cfg("rules.dry_burn_c", 120.0))
        heat_on = float(cfg("rules.heating_on_c", 50.0))
        boil_lo = float(cfg("world_model.boil_low_c", 92.0))
        boil_hi = float(cfg("world_model.boil_high_c", 104.0))
        boil_risk_s = float(cfg("world_model.boil_full_risk_s", 600.0))
        out = self._empty()
        out["temp"] = round(temp, 1)

        win = self._window(now, float(cfg("world_model.fit_window_s", 60.0)))
        if len(win) < 6 or win[-1][0] - win[0][0] < 8.0:
            # 新接入或长时间没有数据后重新采样，不把失联时长算作连续沸腾。
            self._plateau_since = None
            if temp >= dry_c:
                out.update({"phase": "overheat", "risk": 1.0,
                            "eta_s": 0.0, "will_dry_burn": True})
            else:
                out["phase"] = "warming_up" if temp >= heat_on else "idle"
            return out

        # 斜率：最近窗口线性拟合（℃/s）
        t0 = win[0][0]
        fit = self._linfit([t - t0 for t, _ in win], [v for _, v in win])
        slope = fit[0] if fit else 0.0
        out["slope"] = round(slope, 3)

        # 沸腾平台检测：用最近 15 秒的斜率（60 秒窗口里还混着前面的升温段）
        short = [(t, v) for t, v in win if now - t <= 15.0]
        sf = self._linfit([t - short[0][0] for t, _ in short], [v for _, v in short]) if len(short) >= 4 else None
        slope_short = sf[0] if sf else slope
        if boil_lo <= temp <= boil_hi and abs(slope_short) < 0.05:
            if self._plateau_since is None:
                self._plateau_since = now
        elif (temp < boil_lo - 3 or temp > boil_hi + 2
              or slope_short < -0.05 or slope_short > 0.15):
            self._plateau_since = None
        plateau_s = (now - self._plateau_since) if self._plateau_since is not None else 0.0
        out["plateau_s"] = round(plateau_s, 1)
        # 越过沸点后是"液体烧干后的新一段升温"，60 秒窗口里混着前面的沸腾平台，只用最近 15 秒外推
        beyond_boil = temp >= boil_hi
        if beyond_boil:
            slope = slope_short
            out["slope"] = round(slope, 3)

        # 一阶模型：dT/dt = α + β·T（β = -1/τ，T∞ = -α/β）。用 2 秒分箱后的差分求导，抑制噪声
        bins: Dict[int, List[float]] = {}
        for t, v in win:
            bins.setdefault(int((t - t0) // 2), []).append(v)
        pts = sorted((k * 2.0 + 1.0, sum(v) / len(v)) for k, v in bins.items())
        d_t, d_v = [], []
        for (ta, va), (tb, vb) in zip(pts, pts[1:]):
            d_t.append((va + vb) / 2.0)
            d_v.append((vb - va) / (tb - ta))
        model = "linear"
        t_inf = tau = None
        if len(d_t) >= 4 and slope > 0.02 and not beyond_boil:
            lf = self._linfit(d_t, d_v)
            if lf is not None:
                beta, alpha = lf
                if beta < -1e-4:
                    t_inf_c = -alpha / beta
                    tau_c = -1.0 / beta
                    if temp - 5 < t_inf_c < 600 and 5 < tau_c < 3600:
                        t_inf, tau, model = t_inf_c, tau_c, "first_order"

        # 轨迹与 ETA
        forecast = []
        eta = None
        # 还低于沸点时液体仍在：预测轨迹最多到沸点就停住（与"只预测沸腾时间"保持一致）
        cap = (boil_lo + boil_hi) / 2 if temp < boil_hi else None
        for k in range(0, int(self.HORIZON_S) + 1, 10):
            if model == "first_order":
                v = t_inf - (t_inf - temp) * math.exp(-k / tau)
            else:
                v = temp + slope * k
            if cap is not None:
                v = min(v, max(cap, temp))
            forecast.append([k, round(v, 1)])
        def time_to(target):
            if temp >= target:
                return 0.0
            if model == "first_order":
                return -tau * math.log((t_inf - target) / (t_inf - temp)) if t_inf > target else None
            return (target - temp) / slope if slope > 0.02 else None

        if temp < boil_hi:
            # 还低于沸点：液体仍在，先到沸点会停住 —— 只给"多久后沸腾"，不给干烧 ETA
            eta_boil = time_to(boil_lo) if temp < boil_lo else None
            out["eta_boil_s"] = round(eta_boil, 1) if eta_boil is not None else None
        else:
            eta = time_to(dry_c)
        out.update({"t_inf": round(t_inf, 1) if t_inf is not None else None,
                    "tau": round(tau, 1) if tau is not None else None,
                    "eta_s": round(eta, 1) if eta is not None else None,
                    "will_dry_burn": eta is not None,
                    "forecast": forecast, "model": model})

        # 阶段与风险分（0~1）
        if temp >= dry_c:
            phase, risk = "overheat", 1.0
        elif plateau_s > 0:
            phase = "boiling"
            risk = min(1.0, plateau_s / boil_risk_s)
        elif slope > 0.02 and temp >= heat_on * 0.6:
            phase = "heating"
            risk = 0.0 if eta is None else max(0.0, min(1.0, 1.0 - eta / (2 * self.HORIZON_S)))
        elif slope < -0.05:
            phase, risk = "cooling", 0.0
        else:
            phase, risk = ("steady" if temp >= heat_on else "idle"), 0.0
        if unattended and risk > 0:
            risk = min(1.0, risk * 1.5)
        out["phase"] = phase
        out["risk"] = round(risk, 2)
        return out


PHASE_TEXT = {
    "no_sensor": "未接入热源测温", "idle": "未加热", "warming_up": "采样中",
    "heating": "升温中", "boiling": "沸腾平台", "steady": "恒温", "cooling": "降温中",
    "overheat": "已超干烧阈值",
}
