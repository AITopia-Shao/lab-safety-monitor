"""
统一配置加载
src/config.py

读取项目根目录的 config.yaml，与内置默认值深度合并。
找不到文件或缺少 PyYAML 时，全部使用默认值，程序照常运行。
用法：
    from src.config import cfg
    cfg("rules.dry_burn_c")          -> 120
    cfg("camera.pano", 0)
"""
import os
import sys
import copy
import threading
from typing import Any

DEFAULTS = {
    "serial": {"port": "auto", "baudrate": 115200, "heartbeat_s": 1.0},
    "camera": {"pano": 0, "desk": 0, "width": 640, "height": 480, "fourcc": "MJPG"},
    "vision": {"prefer_format": ["rknn", "onnx", "pt"], "imgsz": 640, "conf": 0.35, "iou": 0.45, "device": "cpu"},
    "presence": {"absent_seconds": 15.0, "recover_seconds": 1.0},
    "voting": {"window_s": 3.0, "on_ratio": 0.6, "off_ratio": 0.2, "min_frames": 4},
    "sleep": {"head_y_ratio": 0.60, "still_seconds": 10.0, "max_motion": 0.03},
    "desk": {"edge_line": [[0.0, 0.92], [1.0, 0.92]], "edge_margin": 0.06},
    "rules": {
        "heating_on_c": 50.0,
        "dry_burn_c": 120.0,
        "rate_c_per_s": 2.0,
        "rate_min_c": 80.0,
        "unattended_heating_cut_s": 60.0,
        "smoke_raw": 1800,
        "humidity_high": 80.0,
        "ambient_high_c": 40.0,
        "fallback_ambient_cut_c": 55.0,
        "sim_latch_release_s": 10.0,
    },
    "events": {
        "reminder_s": 30.0,
        "db": "runtime/events.db",
        "snapshot_dir": "runtime/snapshots",
        "voice": {
            "ppe_goggles": 1, "ppe_coat": 2, "ppe_gloves": 3, "ppe_mask": 4, "ppe_headmask": 5,
            "eating": 6, "sleeping": 7, "edge_risk": 8, "unattended_heating": 9,
            "dry_burn": 10, "smoke": 11, "humidity_high": 12,
        },
    },
    "notify": {"webhook_url": "", "min_level": "WARNING"},
    "demo": {"allow_ipc_telemetry": True},
    "ui": {"wave_fps": None},
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_lock = threading.Lock()
_config = None
_config_path = None


def _candidate_paths():
    env_path = os.environ.get("LAB_SAFETY_CONFIG")
    if env_path:
        yield env_path
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False):
        yield os.path.join(os.path.dirname(sys.executable), "config.yaml")
    yield os.path.join(PROJECT_ROOT, "config.yaml")
    if meipass:
        yield os.path.join(meipass, "config.yaml")
    yield os.path.join(os.getcwd(), "config.yaml")


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load(force: bool = False) -> dict:
    global _config, _config_path
    with _lock:
        if _config is not None and not force:
            return _config
        merged = copy.deepcopy(DEFAULTS)
        _config_path = None
        for path in _candidate_paths():
            if path and os.path.isfile(path):
                try:
                    import yaml
                    with open(path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    _deep_merge(merged, data)
                    _config_path = path
                    print(f"[config] 已加载配置文件: {path}")
                except Exception as e:
                    print(f"[config] 配置文件 {path} 读取失败，使用默认值: {e}")
                break
        _config = merged
        return _config


def cfg(path: str, default: Any = None) -> Any:
    node = load()
    for key in path.split("."):
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return node


def resolve_path(p: str) -> str:
    """把配置里的相对路径解析为相对项目根目录（打包后相对 exe 目录）"""
    if os.path.isabs(p):
        return p
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else PROJECT_ROOT
    return os.path.join(base, p)


def config_path():
    load()
    return _config_path
