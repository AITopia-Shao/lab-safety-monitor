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
import tempfile
from typing import Any

DEFAULTS = {
    "hierarchy": {"topology_file": "config/hierarchy.json", "state_dir": "runtime/hierarchy"},
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
        "predict_eta_warn_s": 90.0,
        "boil_unattended_warn_s": 300.0,
        "item_hold_s": 3.0,
    },
    "events": {
        "reminder_s": 30.0,
        "retrigger_s": 10.0,
        "db": "runtime/events.db",
        "snapshot_dir": "runtime/snapshots",
        "voice": {
            "ppe_goggles": 1, "ppe_coat": 2, "ppe_gloves": 3, "ppe_mask": 4, "ppe_headmask": 5,
            "eating": 6, "sleeping": 7, "edge_risk": 8, "unattended_heating": 9,
            "dry_burn": 10, "smoke": 11, "humidity_high": 12, "dry_burn_predict": 13,
        },
    },
    "notify": {"webhook_url": "", "min_level": "WARNING"},
    "demo": {"allow_ipc_telemetry": True},
    "ui": {"wave_fps": None, "kiosk_size": None},
    "world_model": {"fit_window_s": 60.0, "boil_low_c": 92.0, "boil_high_c": 104.0, "boil_full_risk_s": 600.0},
    "web": {"enabled": True, "host": "0.0.0.0", "port": 8080, "token": "", "stream_fps": 6, "stream_width": 640},
    "agent": {
        "route": "auto",
        "auto_review": True,
        "min_level": "WARNING",
        "timeout_s": 20.0,
        "send_images_remote": False,
        "remote": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                   "api_key_env": "DASHSCOPE_API_KEY", "model": "qwen-plus", "vision_model": "qwen-vl-plus"},
        "local": {"base_url": "http://127.0.0.1:11434/v1", "api_key_env": "", "model": "qwen2.5:7b",
                  "vision_model": ""},
    },
    "env": {"mode": "auto", "t_set_c": 26.0, "h_low": 40.0, "h_high": 70.0, "manual_timeout_s": 600.0},
    "jepa": {"enabled": False, "surprise_threshold": 0.0},
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
        # 运行时修改（Web 总后台调参）保存在 runtime/config_override.yaml，重启后仍生效
        ov = _read_overrides()
        if ov:
            _deep_merge(merged, ov)
        _config = merged
        return _config


def _override_path() -> str:
    return resolve_path(os.path.join("runtime", "config_override.yaml"))


def _read_overrides() -> dict:
    path = _override_path()
    if not os.path.isfile(path):
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("配置内容必须是对象")
        return data
    except Exception as e:
        print(f"[config] 运行时覆盖文件读取失败，已忽略: {e}")
        return {}


def set_value(path: str, value: Any, persist: bool = True):
    """运行时修改一项配置（调用方负责校验取值范围），并写入覆盖文件"""
    load()
    keys = path.split(".")
    # 内存和覆盖文件用同一把锁，避免网页、Agent 等线程同时保存时丢失修改。
    with _lock:
        node = _config
        for k in keys[:-1]:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
        if not persist:
            return
        ov = _read_overrides()
        node = ov
        for k in keys[:-1]:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
        temp_path = None
        try:
            import yaml
            path_ = _override_path()
            os.makedirs(os.path.dirname(path_), exist_ok=True)
            # 先完整写入同目录临时文件，再替换；写入失败时保留上一次的有效配置。
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path_),
                                             prefix=".config_override_", suffix=".tmp", delete=False) as f:
                temp_path = f.name
                yaml.safe_dump(ov, f, allow_unicode=True, sort_keys=False)
            os.replace(temp_path, path_)
        except Exception as e:
            print(f"[config] 运行时覆盖文件写入失败（本次修改仅内存生效）: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)


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
