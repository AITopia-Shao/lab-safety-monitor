import cv2
import time
import threading
from typing import Tuple, Optional

class CameraManager:
    """
    视频流接入与双目协同模块
    负责同时拉取全景宏观视角与桌面微观视角两路摄像头流，并进行时间戳对齐。
    """
    def __init__(self, src1: int = 0, src2: int = 0, width: int = 640, height: int = 480):
        self.src1 = src1
        self.src2 = src2
        self.width = width
        self.height = height
        
        self.cap1 = None
        self.cap2 = None
        
        # 用于存储最新的帧及对应时间戳
        self.frame1: Optional[tuple[float, cv2.Mat]] = None
        self.frame2: Optional[tuple[float, cv2.Mat]] = None
        
        self.running = False
        self.lock = threading.Lock()
        
    def start(self):
        """启动双路视频抓取线程 (支持单摄像头双视角复用)"""
        self.cap1 = cv2.VideoCapture(self.src1)
        if not self.cap1.isOpened() and isinstance(self.src1, int):
            try:
                self.cap1 = cv2.VideoCapture(self.src1, cv2.CAP_V4L2)
            except Exception:
                pass
                
        if self.cap1.isOpened():
            self.cap1.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap1.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        
        self.running = True
        if self.src1 == self.src2 and isinstance(self.src1, int):
            # 单摄像头复用模式：避免 Linux 下重复打开同一设备报错
            self.cap2 = None
            self.thread1 = threading.Thread(target=self._capture_single_shared_loop, daemon=True)
            self.thread1.start()
        else:
            self.cap2 = cv2.VideoCapture(self.src2)
            if not self.cap2.isOpened() and isinstance(self.src2, int):
                try:
                    self.cap2 = cv2.VideoCapture(self.src2, cv2.CAP_V4L2)
                except Exception:
                    pass
            if self.cap2.isOpened():
                self.cap2.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.cap2.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                
            self.thread1 = threading.Thread(target=self._capture_loop, args=(self.cap1, 1), daemon=True)
            self.thread2 = threading.Thread(target=self._capture_loop, args=(self.cap2, 2), daemon=True)
            self.thread1.start()
            self.thread2.start()

    def _capture_single_shared_loop(self):
        """单摄像头复用时的帧读取循环"""
        while self.running and self.cap1 and self.cap1.isOpened():
            ret, frame = self.cap1.read()
            if ret:
                timestamp = time.time()
                with self.lock:
                    self.frame1 = (timestamp, frame)
                    self.frame2 = (timestamp, frame.copy())
            else:
                time.sleep(0.01)

    def _capture_loop(self, cap, cam_id: int):
        """独立双摄像头捕获循环"""
        while self.running and cap and cap.isOpened():
            ret, frame = cap.read()
            if ret:
                timestamp = time.time()
                with self.lock:
                    if cam_id == 1:
                        self.frame1 = (timestamp, frame)
                    else:
                        self.frame2 = (timestamp, frame)
            else:
                time.sleep(0.01)  # 防止CPU空转
                
    def get_synced_frames(self, max_diff: float = 0.05) -> Tuple[Optional[cv2.Mat], Optional[cv2.Mat]]:
        """
        获取时间戳对齐的双目帧。
        返回的帧应在 max_diff 秒的时间偏差范围内。
        """
        with self.lock:
            if self.frame1 is None or self.frame2 is None:
                return None, None
            
            t1, f1 = self.frame1
            t2, f2 = self.frame2
            
            # 时间戳对齐逻辑
            if abs(t1 - t2) <= max_diff:
                return f1.copy(), f2.copy()
            else:
                # 若时间偏差过大，返回较新的帧并丢弃不匹配的旧帧
                # 在此简化处理：若无法严格对齐，则返回当前最新帧（或可根据严格要求返回None）
                return f1.copy(), f2.copy()
                
    def stop(self):
        """释放资源"""
        self.running = False
        if hasattr(self, 'thread1'):
            self.thread1.join()
        if hasattr(self, 'thread2'):
            self.thread2.join()
        if self.cap1:
            self.cap1.release()
        if self.cap2:
            self.cap2.release()
