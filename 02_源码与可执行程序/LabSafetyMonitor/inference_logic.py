import math
import time
import cv2
import numpy as np

# 假设已有 YOLOv8 的封装类，此处提供Mock类替代实际加载过程，方便脱机运行演示
# 在实际比赛中替换为 from ultralytics import YOLO
class MockYOLO:
    def __init__(self, model_path):
        pass
    def __call__(self, img):
        return []

class InferenceLogic:
    """
    核心推理与事前干预逻辑模块
    """
    def __init__(self):
        # 初始化模型 (实际部署时填入具体.pt模型路径)
        self.obj_model = MockYOLO("models/yolov8n.pt")
        self.pose_model = MockYOLO("models/yolov8n-pose.pt")
        
        # 坠落预警阈值（像素距离）
        self.FALL_WARNING_THRESH = 50.0 
        
        # 无人值守逻辑状态
        self.person_last_seen_time = time.time()
        self.UNATTENDED_THRESH_SEC = 30.0  # 30秒报警
        
    def calculate_pixel_distance(self, pt1: tuple[int, int], pt2: tuple[int, int]) -> float:
        """
        计算两点间的欧式像素距离 (数学逻辑部分)
        distance = sqrt((x2 - x1)^2 + (y2 - y1)^2)
        """
        return math.sqrt((pt1[0] - pt2[0])**2 + (pt1[1] - pt2[1])**2)
        
    def process_frames(self, panorama_img: np.ndarray, desktop_img: np.ndarray):
        """
        处理双目协同的一组帧，返回处理后的渲染图像与报警状态字典。
        """
        alarms = {
            "static_danger": False,  # 静态危险源 (打火机、未盖盖子试剂瓶等)
            "fall_warning": False,   # 空间危险状态 (坠落预警)
            "unattended": False,     # 无人值守
            "sop_violation": False   # SOP违规操作
        }
        
        # 1. 处理全景画面：目标检测与无人值守逻辑
        # TODO: 实际运行需使用 self.obj_model(panorama_img) 解析检测框及类别
        # Mock 逻辑：假设未检测到人
        person_detected_in_panorama = False # 模拟标志位
        current_time = time.time()
        
        if person_detected_in_panorama:
            self.person_last_seen_time = current_time
        else:
            # 判断无人值守超时
            if (current_time - self.person_last_seen_time) > self.UNATTENDED_THRESH_SEC:
                alarms["unattended"] = True
                
        # 2. 处理桌面微观画面：坠落预警与静态危险源
        # Mock 逻辑：计算烧杯中心点到实验台边缘的距离
        # 假设 bbox_beaker 为烧杯 bounding box [x1, y1, x2, y2]
        # 假设 edge_line 为桌面边缘的一条线或最近点，这里简化为一个固定点代替
        beaker_center = (320, 240) 
        edge_point = (320, 280)
        
        dist = self.calculate_pixel_distance(beaker_center, edge_point)
        if dist < self.FALL_WARNING_THRESH:
            alarms["fall_warning"] = True
            
        # 3. 规范核验：提取手腕、手肘等关键点
        # TODO: self.pose_model(desktop_img) 提取关键点并计算角度
        # ...
        
        # 将报警信息渲染在图像上(此处仅为示例)
        res_pano = panorama_img.copy()
        res_desk = desktop_img.copy()
        
        if alarms["unattended"]:
            cv2.putText(res_pano, "WARNING: Unattended!", (50, 50), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                        
        if alarms["fall_warning"]:
            cv2.putText(res_desk, "WARNING: Fall Risk!", (50, 50), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

        return res_pano, res_desk, alarms
