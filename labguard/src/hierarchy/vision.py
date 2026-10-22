"""任意数量视觉通道；相同权重共享模型，推理加锁避免 YOLO 并发状态冲突。"""
from pathlib import Path
import threading
import time
import cv2
import numpy as np
from .config import resolve_path
from .coordinator import Observation


class ModelPool:
    def __init__(self, device='cpu'):
        self.device = device
        self.models = {}
        self.lock = threading.Lock()

    def predict(self, config, frame):
        path = str(resolve_path(config['model']).resolve())
        with self.lock:
            if path not in self.models:
                import torch
                from ultralytics import YOLO
                torch.set_num_threads(2)
                self.models[path] = (YOLO(path), threading.Lock())
            model, model_lock = self.models[path]
        with model_lock:
            results = model.predict(frame, device=self.device, conf=config['confidence'], imgsz=640, verbose=False)
            labels = []
            annotated = frame.copy()
            for result in results:
                for box in result.boxes:
                    name = result.names[int(box.cls[0])]
                    score = float(box.conf[0])
                    labels.append(name)
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    cv2.rectangle(annotated, (x1, y1), (x2, y2), (70, 210, 100), 2)
                    cv2.putText(annotated, f'{name} {score:.2f}', (x1, max(20, y1 - 6)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (70, 210, 100), 1)
            return labels, annotated


class VisionWorker(threading.Thread):
    def __init__(self, config, role, pool, receive, simulation):
        super().__init__(daemon=True, name='vision-' + config['id'])
        self.config, self.role, self.pool = config, role, pool
        self.receive, self.simulation = receive, simulation
        self.stopping = threading.Event()
        self.capture = None

    def stop(self):
        self.stopping.set()

    def simulated(self):
        scenario = self.simulation(self.config['id'])
        labels = (['Lab Coat'] if scenario.get('occupied', True) else []) if self.role == 'macro' else (
            ['heater'] if scenario.get('heating', False) else ['Beaker'])
        if scenario.get('fire'):
            labels.append('fire')
        image = np.full((360, 640, 3), (35, 29, 21), np.uint8)
        for x in range(0, 640, 40):
            cv2.line(image, (x, 0), (x, 360), (58, 48, 35), 1)
        color = (60, 80, 255) if scenario.get('fire') else (200, 210, 90)
        cv2.rectangle(image, (18, 18), (622, 342), color, 2)
        for y, text in [(50, 'SIMULATION - NOT LIVE INFERENCE'), (90, self.config['id']),
                        (135, 'MACRO / ROOM' if self.role == 'macro' else 'MICRO / BENCH'),
                        (185, ', '.join(labels) or 'NO PERSON EVIDENCE'),
                        (295, time.strftime('%Y-%m-%d %H:%M:%S'))]:
            cv2.putText(image, text, (32, y), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 1, cv2.LINE_AA)
        return labels, image

    def open_capture(self):
        kind, source = self.config['source_kind'], self.config['source']
        if kind == 'image':
            image = cv2.imread(str(resolve_path(source)))
            if image is None:
                raise ValueError('无法读取图片')
            return image
        if kind == 'video':
            source = str(resolve_path(source))
        if kind == 'stream':
            self.capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG,
                                           [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000,
                                            cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000])
        else:
            # 指定摄像头打开失败时不自动换成另一台，保证视角与实验台身份一致。
            self.capture = cv2.VideoCapture(source)
        if not self.capture.isOpened():
            raise ValueError('视频源不可用')
        return None

    def run(self):
        try:
            while not self.stopping.is_set():
                try:
                    if self.config['source_kind'] == 'simulation':
                        labels, frame = self.simulated()
                        self.receive(Observation(self.config['id'], time.time(), labels, 'SIMULATION', '场景注入，未运行视觉模型', frame))
                        self.stopping.wait(1 / self.config['fps'])
                        continue
                    static = self.open_capture()
                    while not self.stopping.is_set():
                        started = time.time()
                        if static is not None:
                            frame = static.copy()
                        else:
                            success, frame = self.capture.read()
                            if not success and self.config['source_kind'] == 'video':
                                self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                success, frame = self.capture.read()
                            if not success:
                                raise ValueError('读取画面失败')
                        # 限制预览尺寸，也避免原始高分辨率帧占用过多内存。
                        scale = min(1.0, 960 / max(frame.shape[:2]))
                        if scale < 1:
                            frame = cv2.resize(frame, None, fx=scale, fy=scale)
                        labels, annotated = self.pool.predict(self.config, frame)
                        if self.stopping.is_set():
                            break
                        self.receive(Observation(self.config['id'], started, labels, 'LIVE', '真实模型推理', annotated))
                        self.stopping.wait(max(0, 1 / self.config['fps'] - (time.time() - started)))
                except Exception as exc:
                    self.receive(Observation(self.config['id'], time.time(), [], 'ERROR',
                                             f'{type(exc).__name__}：视频读取或模型推理失败，5 秒后重试'))
                    self.stopping.wait(5)
                finally:
                    if self.capture is not None:
                        self.capture.release()
                        self.capture = None
        finally:
            if self.capture is not None:
                self.capture.release()
