"""验证真实依赖、Qt 与随项目提供的模型；不打开摄像头或串口。"""
import os
from pathlib import Path
import sys


def main():
    import cv2
    import numpy as np
    import serial
    import torch
    import torchvision
    from ultralytics import YOLO
    from PyQt6.QtCore import QT_VERSION_STR

    # 首次安装成功后，每次启动只检查实际导入，避免离线启动时访问包索引。
    print(f"依赖正常：PyTorch {torch.__version__} / OpenCV {cv2.__version__} / Qt {QT_VERSION_STR}")
    if "--imports-only" in sys.argv:
        return

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtWidgets import QApplication

    app = QApplication([])

    torch.set_num_threads(2)
    root = Path(__file__).resolve().parent.parent
    for name in ("ppe_best.pt", "chem_best.pt"):
        model = YOLO(str(root / "models" / name))
        results = model.predict(np.zeros((64, 64, 3), dtype=np.uint8), imgsz=64, device="cpu", verbose=False)
        if len(results) != 1:
            raise RuntimeError(f"模型推理未返回预期结果：{name}")
        print(f"真实模型推理通过：{name}（{len(model.names)} 类）")

    # 构造真正的主窗口，检查项目代码与 Qt 绑定是否兼容；仅暂停设备线程。
    import tempfile

    sys.path.insert(0, str(root))
    from src.ui.hierarchy_window import HierarchyWindow
    with tempfile.TemporaryDirectory() as state_dir:
        window = HierarchyWindow(state_dir=state_dir, autostart=False)
        window.show()
        app.processEvents()
        window.close()
        app.processEvents()
    print("分层母系统窗口自检通过（不显示窗口、不启动设备线程、不写实际运行状态）")


if __name__ == "__main__":
    main()
