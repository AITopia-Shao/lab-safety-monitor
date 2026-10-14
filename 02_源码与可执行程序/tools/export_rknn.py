"""
把 models/*.pt 导出为香橙派 5（RK3588S）NPU 可用的 RKNN 模型。

运行环境：x86_64 Linux（Ubuntu 20.04/22.04，或 Windows 的 WSL2），Python 3.10
    pip install "ultralytics>=8.3.0"
    pip install rknn-toolkit2            # Rockchip 官方转换工具
    python tools/export_rknn.py

输出：models/ppe_best_rknn_model/、models/chem_best_rknn_model/
把这两个文件夹拷到香橙派的 models/ 目录下，程序会自动优先使用（config.yaml → vision.prefer_format）。

注意：导出尺寸固定为 imgsz，香橙派上推理时也会使用同样的尺寸。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMGSZ = int(sys.argv[1]) if len(sys.argv) > 1 else 640

from ultralytics import YOLO

for stem in ("ppe_best", "chem_best"):
    pt = os.path.join(ROOT, "models", f"{stem}.pt")
    if not os.path.isfile(pt):
        print(f"跳过：未找到 {pt}")
        continue
    print(f"导出 {pt} → RKNN (rk3588, imgsz={IMGSZ})")
    out = YOLO(pt).export(format="rknn", name="rk3588", imgsz=IMGSZ)
    print(f"完成：{out}")
