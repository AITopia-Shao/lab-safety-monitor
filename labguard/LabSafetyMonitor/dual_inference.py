"""
双摄协同视觉推理核心（生产者-消费者）
LabSafetyMonitor/dual_inference.py

面向香橙派 5（RK3588S）的部署优化：
  · 模型按 config.vision.prefer_format 依次寻找：RKNN（NPU）> ONNX > PyTorch .pt
  · USB 摄像头走 V4L2 + MJPG，缓冲区设为 1 帧；按 /dev/video* 扫描真实采集设备
  · 帧队列"丢旧保新"，推理永远处理最新画面，延迟不随负载累积
  · 所有几何判断使用归一化坐标，与摄像头分辨率无关

判定逻辑：
  · 人员在场：Person 模型（PPE）的任意一类装备检测都视为"有人"
  · 无人值守：连续 presence.absent_seconds 秒没有任何人员装备；画面无效时不做判断
  · 违规（未戴护目镜等、饮食、器皿靠近桌边）：时间窗口投票，窗口内命中比例达标才确认
  · 疑似伏案：面部装备框（口罩 / 护目镜 / 头罩）中心持续偏低且几乎静止
对外接口与旧版一致：get_results() 返回
  (标注全景图, 标注台面图, alarms 字典, 违规名称列表, 当前帧, 总帧数, 日志文本)
"""
import os
import sys
import cv2
import glob
import time
import queue
import threading
from collections import deque
from datetime import datetime
from typing import Tuple, Dict, Any, Optional, List
import numpy as np

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.config import cfg
from src.core.events import events
from src.core.telemetry import telemetry

try:
    import torch
    # 给界面线程留出 CPU；RKNN（NPU）推理时不使用 torch
    torch.set_num_threads(max(2, min(4, (os.cpu_count() or 4) // 2)))
except Exception:
    torch = None

VISION_IMPORT_ERROR = ""
try:
    from ultralytics import YOLO
except Exception as e:
    VISION_IMPORT_ERROR = str(e)
    YOLO = None
    print(f"[dual_inference] ultralytics 加载失败（{e}），视觉模块将以离线状态运行")


class _OfflineModel:
    """模型不可用时的占位对象：不产生任何检测结果，系统会明确标记"视觉离线"。"""

    def __init__(self, names=None):
        self.names = names or {}

    def __call__(self, *args, **kwargs):
        return []


# 自定义摄像头模块（可选，通过环境变量指定目录）
try:
    custom_cam_path = os.environ.get("CUSTOM_CAMERA_MODULE_PATH", "")
    if custom_cam_path and os.path.isdir(custom_cam_path) and custom_cam_path not in sys.path:
        sys.path.insert(0, custom_cam_path)
    from Modules.get_camera_image import CameraImage
    HAS_CUSTOM_CAMERA = bool(custom_cam_path)
except Exception:
    HAS_CUSTOM_CAMERA = False


class CustomCameraWrapper:
    """包装用户自定义的 CameraImage 类，使其拥有类似 cv2.VideoCapture 的接口"""

    def __init__(self, src):
        self.cam_image = CameraImage(video=src)
        self.is_opened = True

    def isOpened(self):
        return self.is_opened

    def read(self):
        try:
            _, frame = self.cam_image.get_cam_image()
            if frame is not None:
                return True, frame
            return False, None
        except Exception as e:
            print(f"读取自定义摄像头失败: {e}")
            return False, None

    def release(self):
        if hasattr(self.cam_image, "release"):
            self.cam_image.release()
        self.is_opened = False

    def set(self, propId, value):
        pass

    def get(self, propId):
        return 0


# ---------------------------------------------------------------------------
# 类别语义映射（与 models/ppe_best.pt、models/chem_best.pt 的类别名对应）
# ---------------------------------------------------------------------------
PPE_VIOLATION_KINDS = {
    "no googles": ("ppe_goggles", "未佩戴护目镜"),
    "no goggles": ("ppe_goggles", "未佩戴护目镜"),
    "no lab coat": ("ppe_coat", "未穿实验服"),
    "no gloves": ("ppe_gloves", "未佩戴手套"),
    "no mask": ("ppe_mask", "未佩戴口罩"),
    "no head mask": ("ppe_headmask", "未佩戴头罩"),
    "eating": ("eating", "实验区饮食"),
    "drinking": ("eating", "实验区饮食"),
}
HEAD_CLASSES = {"mask", "no mask", "googles", "no googles", "goggles", "no goggles", "head mask", "no head mask"}
VISUAL_KIND_TITLES = {
    "ppe_goggles": "未佩戴护目镜", "ppe_coat": "未穿实验服", "ppe_gloves": "未佩戴手套",
    "ppe_mask": "未佩戴口罩", "ppe_headmask": "未佩戴头罩", "eating": "实验区饮食",
    "edge_risk": "器皿靠近台面边缘", "sleeping": "疑似伏案睡觉",
}
VISUAL_KIND_LEVEL = {"eating": "WARNING"}


def is_live_source(src) -> bool:
    if isinstance(src, int):
        return True
    if isinstance(src, str):
        s = src.lower()
        return s.startswith("/dev/") or s.startswith(("rtsp://", "http://", "https://"))
    return False


def put_latest(q: queue.Queue, item):
    """队列满时丢弃最旧的元素，保证消费者拿到的总是最新数据"""
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass


class TemporalVoter:
    """时间窗口投票：窗口内命中比例 ≥ on_ratio 确认，≤ off_ratio 解除"""

    def __init__(self):
        self.window = float(cfg("voting.window_s", 3.0))
        self.on_ratio = float(cfg("voting.on_ratio", 0.6))
        self.off_ratio = float(cfg("voting.off_ratio", 0.2))
        self.min_frames = int(cfg("voting.min_frames", 4))
        self.hist: Dict[str, deque] = {}
        self.active: Dict[str, bool] = {}

    def update(self, now: float, hits: set, kinds) -> Dict[str, bool]:
        for kind in kinds:
            d = self.hist.setdefault(kind, deque())
            d.append((now, kind in hits))
            while d and now - d[0][0] > self.window:
                d.popleft()
            if len(d) < self.min_frames:
                continue
            ratio = sum(1 for _, h in d if h) / len(d)
            if not self.active.get(kind) and ratio >= self.on_ratio:
                self.active[kind] = True
            elif self.active.get(kind) and ratio <= self.off_ratio:
                self.active[kind] = False
        return dict(self.active)

    def reset(self, kinds=None):
        for kind in list(kinds or self.hist.keys()):
            self.hist.pop(kind, None)
            self.active[kind] = False


class DualInferenceSystem:
    def __init__(self, src1: Any = 0, src2: Any = 0, width: int = 640, height: int = 480):
        self.width = int(cfg("camera.width", width))
        self.height = int(cfg("camera.height", height))
        self.fourcc = str(cfg("camera.fourcc", "MJPG") or "")

        # 默认参数 0 表示"使用配置文件中的摄像头"
        self.src1 = self._map_default(src1, "pano")
        self.src2 = self._map_default(src2, "desk")

        self.camera_mode = "dual"  # 'test', 'pano_only', 'desk_only', 'dual'
        self.enable_ppe = True
        self.enable_chem = True
        self.conf_thres = float(cfg("vision.conf", 0.35))
        self.iou_thres = float(cfg("vision.iou", 0.45))
        self.img_size = int(cfg("vision.imgsz", 640))
        self.device = os.environ.get("INFERENCE_DEVICE", cfg("vision.device", "cpu"))

        self.media_source1 = self.src1
        self.media_source2 = self.src2
        self.force_reload_media = False
        self.is_paused = False
        self.seek_frame_index = -1
        self.current_frame = 0
        self.total_frames = 0

        # 时序状态
        self.person_last_seen = time.time()
        self._present_since: Optional[float] = None
        self.is_unattended = False
        self._head_track: deque = deque()
        self.is_head_down = False
        self.head_down_start = 0.0
        self.voter = TemporalVoter()

        # 模型加载
        self.model_info: Dict[str, str] = {}
        self._fixed_imgsz: Optional[int] = None
        self.vision_errors: List[str] = []
        self.ppe_model, self.ppe_class_names = self._load_model(["ppe_best", "best"], "人员装备模型", [
            os.path.join("person", "model_code", "train", "weights", "best.pt"),
            os.path.join("pic", "person_train", "weights", "best.pt"),
        ])
        self.chem_model, self.chem_class_names = self._load_model(["chem_best"], "器皿模型", [
            os.path.join("pic", "chem_model", "weights", "best.pt"),
            os.path.join("chem", "runs", "detect", "runs", "chem_model", "weights", "best.pt"),
        ])
        self.vision_ok = not self.vision_errors
        if not self.vision_ok:
            events.update("vision_offline", True, "WARNING", "视觉模块离线",
                          "；".join(self.vision_errors), snapshot=False)

        self.frame_queue: "queue.Queue" = queue.Queue(maxsize=2)
        self.result_queue: "queue.Queue" = queue.Queue(maxsize=2)

        self.running = False
        self.producer_thread = None
        self.consumer_thread = None

    # ------------------------------------------------------------------
    # 模型
    # ------------------------------------------------------------------
    @staticmethod
    def _map_default(src, which):
        if isinstance(src, int) and src == 0:
            val = cfg(f"camera.{which}", 0)
            if isinstance(val, str) and val.strip().isdigit():
                return int(val.strip())
            return val
        return src

    def _find_model(self, stems: List[str], legacy: List[str]) -> Optional[str]:
        meipass = getattr(sys, "_MEIPASS", None)
        roots = [r for r in [meipass, _project_root, os.path.dirname(_project_root), os.getcwd()] if r and os.path.isdir(r)]
        formats = cfg("vision.prefer_format", ["rknn", "onnx", "pt"]) or ["pt"]
        for fmt in formats:
            for root in roots:
                for stem in stems:
                    if fmt == "rknn":
                        cand = os.path.join(root, "models", f"{stem}_rknn_model")
                        if os.path.isdir(cand):
                            return cand
                    elif fmt == "onnx":
                        cand = os.path.join(root, "models", f"{stem}.onnx")
                        if os.path.isfile(cand):
                            return cand
                    elif fmt == "pt":
                        cand = os.path.join(root, "models", f"{stem}.pt")
                        if os.path.isfile(cand):
                            return cand
                        for rel in legacy:
                            cand = os.path.join(root, rel)
                            if os.path.isfile(cand):
                                return cand
        return None

    def _load_model(self, stems, label, legacy):
        if YOLO is None:
            self.vision_errors.append(f"{label}：ultralytics 未安装（{VISION_IMPORT_ERROR}）")
            return _OfflineModel(), {}
        path = self._find_model(stems, legacy)
        if path is None:
            self.vision_errors.append(f"{label}：models/ 下未找到模型文件")
            return _OfflineModel(), {}
        try:
            model = YOLO(path, task="detect")
            self.model_info[label] = path
            if path.endswith("_rknn_model"):
                # RKNN 模型的输入尺寸在导出时已固定，推理时必须使用相同尺寸
                self._fixed_imgsz = int(cfg("vision.imgsz", 640))
            print(f"[DualInference] 加载{label}: {path}")
            return model, model.names
        except Exception as e:
            self.vision_errors.append(f"{label}加载失败：{e}")
            return _OfflineModel(), {}

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------
    def start(self):
        self.running = True
        self.producer_thread = threading.Thread(target=self._producer_loop, name="Producer", daemon=True)
        self.consumer_thread = threading.Thread(target=self._consumer_loop, name="Consumer", daemon=True)
        self.producer_thread.start()
        self.consumer_thread.start()

    def load_media(self, src1: Any = None, src2: Any = None, force_reload: bool = False):
        """动态切换媒体源（int / 设备路径 / 文件 / URL）；参数 0 表示配置文件中的默认摄像头"""
        if src1 is not None:
            self.media_source1 = self._map_default(src1, "pano")
        if src2 is not None:
            self.media_source2 = self._map_default(src2, "desk")
        self.force_reload_media = force_reload
        self.is_paused = False
        self.seek_frame_index = -1
        self._reset_temporal_state()

    def set_pause(self, pause: bool):
        self.is_paused = pause

    def seek(self, frame_index: int):
        self.seek_frame_index = frame_index
        self._reset_temporal_state()

    def set_config(self, camera_mode: str, enable_ppe: bool, enable_chem: bool,
                   conf: float = 0.25, iou: float = 0.45, img_size: int = 640):
        if enable_ppe != self.enable_ppe or enable_chem != self.enable_chem:
            self._reset_temporal_state()
        self.camera_mode = camera_mode
        self.enable_ppe = enable_ppe
        self.enable_chem = enable_chem
        self.conf_thres = min(0.95, max(0.05, float(conf)))   # 置信度为 0 时画面会被检测框淹没
        self.iou_thres = min(0.95, max(0.05, float(iou)))
        self.img_size = int(img_size) // 32 * 32 or 640

    def reload_model(self, slot: str, path: str) -> Tuple[bool, str]:
        """
        运行时替换模型。slot: "ppe"（人员装备）或 "chem"（器皿）。
        返回 (是否成功, 说明)。注意：.pt 文件本质是 pickle，只加载自己训练的可信模型。
        """
        if YOLO is None:
            return False, f"ultralytics 不可用：{VISION_IMPORT_ERROR}"
        if not os.path.exists(path):
            return False, f"文件不存在：{path}"
        try:
            model = YOLO(path, task="detect")
            names = model.names
        except Exception as e:
            return False, f"模型加载失败：{e}"
        label = "人员装备模型" if slot == "ppe" else "器皿模型"
        if slot == "ppe":
            self.ppe_model, self.ppe_class_names = model, names
        else:
            self.chem_model, self.chem_class_names = model, names
        self._fixed_imgsz = int(cfg("vision.imgsz", 640)) if path.rstrip("/\\").endswith("_rknn_model") else None
        self.model_info[label] = path
        self.vision_errors = [e for e in self.vision_errors if not e.startswith(label)]
        self.vision_ok = not self.vision_errors
        events.update("vision_offline", not self.vision_ok, "WARNING", "视觉模块离线", "；".join(self.vision_errors))
        self._reset_temporal_state()
        names_list = list(names.values()) if isinstance(names, dict) else list(names)
        base = os.path.basename(path.rstrip("/\\"))  # f-string 表达式里不能写反斜杠（Python 3.10）
        events.log("模型已切换", f"{label} ← {base}（{len(names_list)} 类）", kind="model")
        return True, f"{label}已切换为：{path}\n类别（{len(names_list)}）：{', '.join(map(str, names_list[:20]))}"

    def stop(self):
        self.running = False
        if self.producer_thread:
            self.producer_thread.join(timeout=1.0)
        if self.consumer_thread:
            self.consumer_thread.join(timeout=1.0)

    def get_results(self) -> Optional[Tuple[np.ndarray, np.ndarray, Dict, List[str], int, int, str]]:
        try:
            return self.result_queue.get_nowait()
        except queue.Empty:
            return None

    def _reset_temporal_state(self):
        self.person_last_seen = time.time()
        self._present_since = None
        self.is_unattended = False
        self._head_track.clear()
        self.is_head_down = False
        self.voter.reset()

    # ------------------------------------------------------------------
    # 采集（生产者）
    # ------------------------------------------------------------------
    def _generate_simulated_frames(self) -> Tuple[np.ndarray, np.ndarray]:
        """无物理摄像头时生成仿真演示帧"""
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        frames = []
        for title, bg, line, border, tip in (
            ("[PANORAMIC MACRO VIEW - NO CAMERA]", (35, 30, 25), (50, 45, 40), (192, 107, 92),
             "Tip: check camera.pano in config.yaml"),
            ("[DESK MICRO VIEW - NO CAMERA]", (25, 30, 35), (40, 45, 50), (92, 167, 192),
             "Tip: check camera.desk in config.yaml"),
        ):
            f = np.full((self.height, self.width, 3), bg, dtype=np.uint8)
            for y in range(0, self.height, 40):
                cv2.line(f, (0, y), (self.width, y), line, 1)
            for x in range(0, self.width, 40):
                cv2.line(f, (x, 0), (x, self.height), line, 1)
            cv2.rectangle(f, (20, 20), (self.width - 20, self.height - 20), border, 2)
            cv2.putText(f, title, (35, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
            cv2.putText(f, "STATUS: WAITING FOR VIDEO SOURCE", (35, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (80, 180, 255), 1)
            cv2.putText(f, f"TIMESTAMP: {time_str}", (35, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 200, 200), 1)
            cv2.putText(f, tip, (35, self.height - 50), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (180, 200, 255), 1)
            frames.append(f)
        time.sleep(0.05)
        return frames[0], frames[1]

    def _scan_candidates(self) -> List[Any]:
        if os.name == "nt":
            return list(range(4))
        devs = glob.glob("/dev/video*")
        devs.sort(key=lambda p: int("".join(ch for ch in p if ch.isdigit()) or 0))
        return devs[:32]

    def _open_live(self, src, exclude=()) -> Optional[Any]:
        """打开摄像头或网络流；指定设备失败时扫描其他可用的采集设备"""
        if HAS_CUSTOM_CAMERA and not isinstance(src, str):
            try:
                return CustomCameraWrapper(src)
            except Exception as e:
                print(f"自定义摄像头 {src} 加载失败，回退到 OpenCV: {e}")

        if isinstance(src, str) and src.lower().startswith(("rtsp://", "http://", "https://")):
            cap = cv2.VideoCapture(src)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                print(f"[DualInference] 网络视频流已连接: {src}")
                return cap
            cap.release()
            return None

        backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if os.name == "nt" else [cv2.CAP_V4L2, cv2.CAP_ANY]

        def try_open(dev):
            for b in backends:
                try:
                    c = cv2.VideoCapture(dev, b)
                    if not c.isOpened():
                        c.release()
                        continue
                    if self.fourcc and len(self.fourcc) == 4:
                        c.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
                    c.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                    c.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                    c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    ok, _ = c.read()
                    if ok:
                        print(f"[DualInference] 摄像头已激活: {dev} (backend {b})")
                        return c
                    c.release()
                except Exception:
                    pass
            return None

        cap = try_open(src)
        if cap is None and isinstance(src, int):
            for cand in self._scan_candidates():
                if cand == src or cand in exclude:
                    continue
                cap = try_open(cand)
                if cap is not None:
                    self._scanned_device = cand
                    break
        return cap

    def _open_source(self, src, exclude=()):
        """返回 (cap, static_img, fps)"""
        if src is None:
            return None, None, 0.0
        if isinstance(src, str) and src.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
            img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR) if os.path.exists(src) else None
            return None, (self._fit(img) if img is not None else None), 0.0
        if is_live_source(src):
            return self._open_live(src, exclude), None, 0.0
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            print(f"[DualInference] 无法打开视频文件: {src}")
            return None, None, 0.0
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        return cap, None, (fps if 1.0 < fps < 120.0 else 25.0)

    def _fit(self, frame):
        """大分辨率画面按比例缩小（最长边不超过 960），降低香橙派上的拷贝与显示开销"""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        m = max(h, w)
        if m > 960:
            s = 960.0 / m
            frame = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        return frame

    def _producer_loop(self):
        cap1 = cap2 = None
        static1 = static2 = None
        fps1 = 0.0
        cur1 = cur2 = object()
        last_retry = 0.0
        fails = [0, 0]
        self._scanned_device = None

        def release(c):
            if c is not None:
                try:
                    c.release()
                except Exception:
                    pass

        def shared_live():
            return cur1 == cur2 and is_live_source(cur1)

        def reopen():
            nonlocal cap1, cap2, static1, static2, fps1
            release(cap1)
            release(cap2)
            self._scanned_device = None
            cap1, static1, fps1 = self._open_source(cur1)
            if shared_live():
                cap2, static2 = None, None
            else:
                exclude = (self._scanned_device,) if self._scanned_device is not None else ()
                cap2, static2, _ = self._open_source(cur2, exclude)

        while self.running:
            t_loop = time.time()
            if self.force_reload_media or cur1 != self.media_source1 or cur2 != self.media_source2:
                self.force_reload_media = False
                cur1, cur2 = self.media_source1, self.media_source2
                reopen()
                last_retry = time.time()

            # 实时源掉线后每 5 秒重试一次（香橙派上 USB 摄像头热插拔后会重新枚举）
            live_missing = (is_live_source(cur1) and (cap1 is None or not cap1.isOpened())) or \
                           (is_live_source(cur2) and not shared_live() and (cap2 is None or not cap2.isOpened()))
            if live_missing and time.time() - last_retry > 5.0:
                last_retry = time.time()
                reopen()

            if self.seek_frame_index != -1:
                if cap1 is not None and not is_live_source(cur1):
                    cap1.set(cv2.CAP_PROP_POS_FRAMES, self.seek_frame_index)
                if cap2 is not None and not is_live_source(cur2):
                    cap2.set(cv2.CAP_PROP_POS_FRAMES, self.seek_frame_index)
                self.seek_frame_index = -1

            if self.is_paused:
                time.sleep(0.1)
                continue

            def read(cap, static, src, slot):
                if static is not None:
                    return True, static.copy()
                if cap is not None and cap.isOpened():
                    ok, f = cap.read()
                    if not ok and not is_live_source(src):
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # 视频循环播放
                        ok, f = cap.read()
                    if ok and f is not None:
                        fails[slot] = 0
                        return True, self._fit(f)
                    # USB 摄像头被拔出后 isOpened() 仍可能为 True：连续失败 30 次视为掉线
                    fails[slot] += 1
                    if is_live_source(src) and fails[slot] >= 30:
                        fails[slot] = 0
                        print(f"[DualInference] 摄像头 {src} 连续读帧失败，等待重连")
                        release(cap)
                        return False, "lost"
                    time.sleep(0.01)
                return False, None

            ret1, frame1 = read(cap1, static1, cur1, 0)
            if isinstance(frame1, str):
                cap1, frame1 = None, None
            if shared_live():
                ret2, frame2 = ret1, (frame1.copy() if ret1 else None)
            else:
                ret2, frame2 = read(cap2, static2, cur2, 1)
                if isinstance(frame2, str):
                    cap2, frame2 = None, None

            if cap1 is not None and not is_live_source(cur1) and static1 is None:
                self.current_frame = int(cap1.get(cv2.CAP_PROP_POS_FRAMES))
                self.total_frames = int(cap1.get(cv2.CAP_PROP_FRAME_COUNT))
            else:
                self.current_frame = 0
                self.total_frames = 0

            meta = {"pano_valid": ret1, "desk_valid": ret2, "simulated": False}

            if self.camera_mode == "test" and ret1:
                frame2, ret2, meta["desk_valid"] = frame1.copy(), True, True
            elif self.camera_mode == "pano_only" and ret1:
                frame2, ret2, meta["desk_valid"] = np.zeros_like(frame1), True, False
            elif self.camera_mode == "desk_only" and ret2:
                frame1, ret1, meta["pano_valid"] = np.zeros_like(frame2), True, False

            if ret1 and not ret2:
                frame2 = np.zeros_like(frame1)
            elif ret2 and not ret1:
                frame1 = np.zeros_like(frame2)
            elif not ret1 and not ret2:
                frame1, frame2 = self._generate_simulated_frames()
                meta["simulated"] = True

            put_latest(self.frame_queue, (frame1, frame2, meta))

            # 本地视频按原始帧率播放；实时源由摄像头自身节奏控制
            if fps1 > 0 and not is_live_source(cur1):
                remain = 1.0 / fps1 - (time.time() - t_loop)
                if remain > 0:
                    time.sleep(remain)

        release(cap1)
        release(cap2)

    # ------------------------------------------------------------------
    # 推理（消费者）
    # ------------------------------------------------------------------
    def _infer(self, model, frame):
        return model(frame, verbose=False, conf=self.conf_thres, iou=self.iou_thres,
                     imgsz=self._fixed_imgsz or self.img_size, device=self.device)

    @staticmethod
    def _boxes(results, names):
        out = []
        if not results:
            return out
        for box in results[0].boxes:
            cls_id = int(box.cls[0])
            name = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else str(names[cls_id])
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
            out.append((name, float(box.conf[0]), x1, y1, x2, y2))
        return out

    def _edge_distance(self, px: float, py: float) -> float:
        """点到桌边线的有向距离（归一化坐标）：正值在台面内侧，负值已越过桌边"""
        (x1, y1), (x2, y2) = cfg("desk.edge_line", [[0.0, 0.92], [1.0, 0.92]])
        dx, dy = x2 - x1, y2 - y1
        length = (dx * dx + dy * dy) ** 0.5 or 1.0
        d = ((px - x1) * dy - (py - y1) * dx) / length
        d_center = ((0.5 - x1) * dy - (0.5 - y1) * dx) / length
        return d if d_center >= 0 else -d

    def _update_presence(self, now: float, valid: bool, evidence: bool):
        absent_s = float(cfg("presence.absent_seconds", 15.0))
        recover_s = float(cfg("presence.recover_seconds", 1.0))
        if not valid:
            # 画面无效（关闭模型 / 黑屏 / 无摄像头 / 视觉离线）：不判断无人值守
            self.person_last_seen = now
            self._present_since = None
            self.is_unattended = False
            return
        if evidence:
            self.person_last_seen = now
            if self._present_since is None:
                self._present_since = now
            if self.is_unattended and now - self._present_since >= recover_s:
                self.is_unattended = False
        else:
            self._present_since = None
            if now - self.person_last_seen > absent_s:
                self.is_unattended = True

    def _update_sleep(self, now: float, head_pts: List[Tuple[float, float]]) -> bool:
        ratio = float(cfg("sleep.head_y_ratio", 0.6))
        still_s = float(cfg("sleep.still_seconds", 10.0))
        max_motion = float(cfg("sleep.max_motion", 0.03))
        if not head_pts:
            self._head_track.clear()
            self.is_head_down = False
            return False
        hx = sum(p[0] for p in head_pts) / len(head_pts)
        hy = sum(p[1] for p in head_pts) / len(head_pts)
        if hy < ratio:
            self._head_track.clear()
            self.is_head_down = False
            return False
        if not self.is_head_down:
            self.is_head_down = True
            self.head_down_start = now
        self._head_track.append((now, hx, hy))
        while self._head_track and now - self._head_track[0][0] > still_s:
            self._head_track.popleft()
        xs = [p[1] for p in self._head_track]
        ys = [p[2] for p in self._head_track]
        motion = max(max(xs) - min(xs), max(ys) - min(ys))
        return (now - self.head_down_start) >= still_s and motion <= max_motion

    def _consumer_loop(self):
        last_frame_t = time.time()
        fps = 0.0
        while self.running:
            try:
                frame1, frame2, meta = self.frame_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                now = time.time()
                dt = now - last_frame_t
                last_frame_t = now
                if dt > 0:
                    fps = 0.9 * fps + 0.1 * (1.0 / dt) if fps else 1.0 / dt
                self._process(frame1, frame2, meta, now, fps)
            except Exception as e:
                print(f"推理错误: {e}")
                time.sleep(0.1)

    def _process(self, frame1, frame2, meta, now, fps):
        alarms = {"ppe_violation": False, "eating_drinking": False, "falling_warning": False,
                  "sleeping": False, "unattended": False, "chem_items": [],
                  "vision_ok": self.vision_ok, "person_present": None}
        ann1 = frame1.copy()
        ann2 = frame2.copy()
        h1, w1 = frame1.shape[:2]
        h2, w2 = frame2.shape[:2]
        log_lines = ["====== 当前画面识别状态 ======"]

        hits = set()
        head_pts = []
        evidence = False
        pano_ok = self.vision_ok and self.enable_ppe and meta["pano_valid"] and not meta["simulated"]
        desk_ok = self.vision_ok and self.enable_chem and meta["desk_valid"] and not meta["simulated"]

        # ---------- 1. 全景：人员装备模型 ----------
        if pano_ok:
            dets = self._boxes(self._infer(self.ppe_model, frame1), self.ppe_class_names)
            evidence = len(dets) > 0
            for name, conf, x1, y1, x2, y2 in dets:
                low = name.strip().lower()
                kind = PPE_VIOLATION_KINDS.get(low)
                if kind is None and low.startswith("no "):
                    kind = (f"ppe_{low[3:].replace(' ', '_')}", f"未佩戴{name[3:]}")
                if kind:
                    hits.add(kind[0])
                    VISUAL_KIND_TITLES.setdefault(kind[0], kind[1])
                if low in HEAD_CLASSES:
                    head_pts.append((((x1 + x2) / 2) / w1, ((y1 + y2) / 2) / h1))
                color = (0, 0, 255) if kind else (0, 200, 0)
                cv2.rectangle(ann1, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                cv2.putText(ann1, f"{name} {conf:.2f}", (int(x1), max(15, int(y1) - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            log_lines.append("[人员模型]: " + (", ".join(f"{d[0]}({d[1]:.2f})" for d in dets) if dets else "未检测到人员装备"))
        elif self.enable_ppe:
            log_lines.append("[人员模型]: 画面无效，暂停判断")

        self._update_presence(now, pano_ok, evidence)
        alarms["unattended"] = self.is_unattended
        alarms["person_present"] = (not self.is_unattended) if pano_ok else None
        sleeping = self._update_sleep(now, head_pts) if pano_ok else False

        # ---------- 2. 台面：器皿模型 + 桌边线 ----------
        if desk_ok:
            dets = self._boxes(self._infer(self.chem_model, frame2), self.chem_class_names)
            margin = float(cfg("desk.edge_margin", 0.06))
            for name, conf, x1, y1, x2, y2 in dets:
                alarms["chem_items"].append(name)
                d = self._edge_distance(((x1 + x2) / 2) / w2, y2 / h2)
                near = d < margin
                if near:
                    hits.add("edge_risk")
                color = (0, 165, 255) if near else (255, 0, 0)
                label = f"EDGE {name}" if near else f"{name} {conf:.2f}"
                cv2.rectangle(ann2, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                cv2.putText(ann2, label, (int(x1), max(15, int(y1) - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            (ex1, ey1), (ex2, ey2) = cfg("desk.edge_line", [[0.0, 0.92], [1.0, 0.92]])
            cv2.line(ann2, (int(ex1 * w2), int(ey1 * h2)), (int(ex2 * w2), int(ey2 * h2)), (0, 215, 255), 1, cv2.LINE_AA)
            log_lines.append("[物品模型]: " + (", ".join(f"{d[0]}({d[1]:.2f})" for d in dets) if dets else "未检测到器皿"))
        elif self.enable_chem:
            log_lines.append("[物品模型]: 画面无效，暂停判断")

        # ---------- 3. 时间投票 → 确认违规 ----------
        voted_kinds = set(PPE_VIOLATION_KINDS[k][0] for k in PPE_VIOLATION_KINDS) | {"edge_risk"} | \
            {k for k in hits if k.startswith("ppe_")}
        if not pano_ok:
            stale = {k for k in voted_kinds if k != "edge_risk"}
            self.voter.reset(stale)
            voted_kinds -= stale
        if not desk_ok:
            self.voter.reset(["edge_risk"])
            voted_kinds.discard("edge_risk")
        active = self.voter.update(now, hits, voted_kinds)
        confirmed = {k for k, v in active.items() if v and k in voted_kinds}
        if sleeping:
            confirmed.add("sleeping")

        alarms["ppe_violation"] = any(k.startswith("ppe_") for k in confirmed)
        alarms["eating_drinking"] = "eating" in confirmed
        alarms["falling_warning"] = "edge_risk" in confirmed
        alarms["sleeping"] = "sleeping" in confirmed
        violation_types = [VISUAL_KIND_TITLES.get(k, k) for k in sorted(confirmed)]
        if self.is_unattended:
            violation_types.append("无人值守")

        # ---------- 4. 画面叠加 ----------
        y = 30
        if not self.vision_ok:
            cv2.putText(ann1, "VISION OFFLINE - CHECK MODELS", (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            y += 34
        if self.is_unattended:
            cv2.putText(ann1, "UNATTENDED LAB", (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            y += 34
        if alarms["sleeping"]:
            cv2.putText(ann1, "HEAD DOWN / SLEEPING?", (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)

        # ---------- 5. 事件上报 ----------
        events.set_latest_frames(ann1, ann2)
        for kind in set(VISUAL_KIND_TITLES.keys()):
            on = kind in confirmed
            level = VISUAL_KIND_LEVEL.get(kind, "NOTICE")
            title = VISUAL_KIND_TITLES[kind]
            events.update(kind, on, level, title, "请立即纠正" if kind != "edge_risk" else "请将器皿移至台面内侧")
        events.update("unattended", self.is_unattended, "INFO", "实验台无人",
                      f"已连续 {float(cfg('presence.absent_seconds', 15.0)):.0f} 秒未检测到人员", snapshot=False)

        # ---------- 6. 日志 ----------
        log_lines.append(self._status_line(fps))
        recent = events.recent_lines(8)
        if recent:
            log_lines.append("====== 最近事件 ======\n" + "\n".join(reversed(recent)))
        log_str = "\n\n".join(log_lines)

        put_latest(self.result_queue, (ann1, ann2, alarms, violation_types,
                                       self.current_frame, self.total_frames, log_str))

    def _status_line(self, fps: float) -> str:
        parts = [f"视觉: {'正常' if self.vision_ok else '离线'} {fps:.1f}FPS"]
        try:
            from serial_comm import SerialComm
            link = SerialComm.get_shared()
            parts.append(link.status_text() if link is not None else "下位机未启用")
        except Exception:
            pass
        snap = telemetry.snapshot()
        src_text = {"hardware": "实测", "ipc": "模拟器注入", "simulated": "自主模拟"}.get(snap.get("source"), "无")
        obj = snap.get("obj_temp")
        parts.append(f"遥测: {src_text}")
        parts.append(f"热源: {obj:.1f}℃" if obj is not None else "热源测温: 未接入")
        if snap.get("smoke") is not None:
            parts.append(f"烟雾: {snap['smoke']}")
        return "[系统] " + " | ".join(parts)
