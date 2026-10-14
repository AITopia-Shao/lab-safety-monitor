# 基于双摄协同的实验室安全事前干预监测系统

> 湖南涉外经济学院 3 队 · 嵌入式开发赛道
> 本 README 面向团队成员，描述当前项目状态、模块分工、运行方式与嵌入式物理化计划。

---

## 一、项目简介

针对高校实验室"事后追责、单目视觉局限、软件报警与硬件强电切断断层"三大痛点，构建**双摄协同 + 跨模态边缘状态机 + 硬件级物理断电**的事前干预监测系统。

- **宏观通道**：YOLOv8 检测口罩/实验服/饮食违规/人员姿态/无人值守
- **微观通道**：YOLOv8 识别烧杯/试管/酒精灯等加热容器
- **跨模态决策**：微观加热设备 + 宏观无人值守 + 温度 > 55℃ → STM32 继电器物理断电
- **NFR 指标**：双模型并发 ≥30 FPS（GPU）/ ≥12 FPS（CPU）；干烧→断电 ≤100 ms

---

## 二、目录结构

```
基于双摄协同的实验室安全事前干预监测系统/
├── 01_项目文档/                       # 7 篇 docx + 综合说明书 docx/pdf
├── 02_源码与可执行程序/                # 源码 + 模型 + 字体
│   ├── run_app.py                     # 主入口
│   ├── env_controller.py              # 离线环境仿真控制器（IPC 注入温湿度）
│   ├── requirements.txt
│   ├── environment.yml                 # Conda 环境（lab_safety, Python 3.10）
│   ├── LabSafetyMonitor.spec           # PyInstaller 打包脚本（主程序）
│   ├── EnvController.spec              # PyInstaller 打包脚本（仿真器）
│   ├── build_windows.bat              # Windows 一键打包
│   ├── LabSafetyMonitor/              # 推理与硬件通信层
│   │   ├── dual_inference.py           # ★ 双摄 YOLO 并发推理（生产者-消费者）
│   │   ├── camera_manager.py           # 双目视频拉流与时间戳对齐
│   │   ├── serial_comm.py              # STM32 串口通信与断电指令
│   │   ├── main_gui.py                 # 兼容入口（实际转发到 src/ui/main_window.py）
│   │   ├── inference_logic.py          # ⚠ 早期 Mock 版，已被 dual_inference 取代，待删
│   │   ├── Project_Summary.md          # 历史项目摘要
│   │   ├── Zpix.ttf / NerdFontSymbols.ttf   # 像素字体资源
│   ├── src/
│   │   ├── bootstrap.py                # 自动注入 venv/conda site-packages 到 sys.path
│   │   ├── core/state_machine.py       # ★ 跨模态边缘状态机（干烧/高湿判决）
│   │   ├── hardware/sensor_worker.py   # 串口温湿度后台线程（无硬件自动降级模拟）
│   │   ├── ui/main_window.py           # ★ 主窗口（水色海浪像素粗野主义 UI）
│   ├── models/
│   │   ├── ppe_best.pt                  # PPE/人员模型（~5.5 MB）
│   │   └── chem_best.pt                # 化学容器模型（~5.5 MB）
├── 03_答辩汇报PPT/                      # 16:9 答辩幻灯片
├── 一键启动_Linux.sh / 一键启动_Windows.bat   # 顶层完整启动脚本（含 Conda 自动建环境）
├── 湖南涉外经济学院-3队-嵌入式开发-系统设计说明书.pdf
├── 湖南涉外经济学院-3队-嵌入式软件类-报名表.pdf
├── 湖南涉外经济学院-3队-嵌入式-演示视频.mp4
└── README.md                           # 本文件
```

---

## 三、快速启动

### 1. 源码方式（推荐开发调试）

```bash
# Linux 一键启动（自动识别 .venv / venv / Conda lab_safety 环境）
./一键启动_Linux.sh

# 或手动启动
pip install -r 02_源码与可执行程序/requirements.txt
cd 02_源码与可执行程序
python run_app.py
```

### 2. 启动独立环境仿真器

用于无真实硬件时演示干烧断电闭环：

```bash
python 02_源码与可执行程序/env_controller.py
```

仿真器通过 `/tmp/lab_safety_env_telemetry.json` 以 10 Hz 注入温湿度到主程序，主程序的 `SensorWorker` 检测到该文件最近修改时间 < 2.5 s 时自动切入"外部模拟控制模式"。

### 3. 离线无摄像头演示

主程序无摄像头时会自动调用 `DualInferenceSystem._generate_simulated_frames()` 生成仿真带时间戳的测试画面，保证路演零崩溃。也可点界面左下角"导入宏观/微观视频"加载预录素材。

---

## 四、模块说明与分工

| 模块 | 文件 | 职责 | 负责人 |
|---|---|---|---|
| 主入口 | `run_app.py` | Qt 应用启动、像素字体加载 | — |
| 双摄推理 | `LabSafetyMonitor/dual_inference.py` | 生产者-消费者队列 + 双 YOLO 并发 + 姿态/无人值守判定 | — |
| 视频拉流 | `LabSafetyMonitor/camera_manager.py` | 双路 OpenCV 拉流与时间戳对齐 | — |
| 状态机 | `src/core/state_machine.py` | 跨模态判决 → 调用 `send_power_off()` | — |
| 串口通信 | `LabSafetyMonitor/serial_comm.py` | STM32 Hex 协议帧下发 | — |
| 传感器 | `src/hardware/sensor_worker.py` | 串口温湿度轮询 + IPC 注入 + 降级模拟 | — |
| 主 UI | `src/ui/main_window.py` | 主窗口、WaveBackground、TelemetryCard、ReviewCard、ScoreBoard | — |
| 仿真器 | `env_controller.py` | 独立 GUI，预设场景 + 阶梯温升 + IPC 广播 | — |
| 环境引导 | `src/bootstrap.py` | 自动注入 sys.path | — |

> 「负责人」列空着，由队长分配后填入。

---

## 五、架构与数据流

```
双摄 → frame_queue → [PPE 模型 + Chem 模型 并发] → result_queue → UI 渲染
                          ↓
                    SafetyStateMachine.evaluate()
                          ↓
              微观加热 + 无人值守 + T>55℃ → SerialComm.send_power_off()
                          ↓
                    STM32 → 继电器 → 强电切断
```

- 推理线程与 UI 线程通过 Qt Signal 解耦，UI 保持 60 FPS
- PyTorch CPU 线程数限制为 2（`dual_inference.py` 中 `torch.set_num_threads(2)`），避免抢占 UI
- 状态机断电指令有 3 s 冷却防抖，防继电器电弧

---

## 六、关键参数与协议

### 6.1 状态机阈值（`state_machine.py`）

| 阈值 | 默认值 | 触发动作 |
|---|---|---|
| `HIGH_TEMP_THRESHOLD` | 55.0 ℃ | 进入 CRITICAL，下发断电 |
| `HIGH_HUMI_THRESHOLD` | 80.0 % | 进入 WARNING，环境告警 |
| `power_off_cooldown` | 3.0 s | 断电指令防抖冷却 |
| 无人值守超时 | 5.0 s（`dual_inference.py`） | 进入 unattended 状态 |
| 伏案/睡眠计时 | 3.0 s | 触发 sleeping 告警 |

### 6.2 STM32 串口协议（`serial_comm.py`）

- 波特率 115200，数据位 8，停止位 1，无校验
- 帧格式：`0xAA [cmd] [data] [checksum] 0x55`
- 断电指令：cmd=0x01，data=0x00
- 校验和：payload 累加和对 256 取模
- 实际下发字节：`AA 01 00 01 55`

### 6.3 传感器 ASCII 解析（`sensor_worker.py`）

支持格式：
- `TEMP:27.6,HUMI:69.2`
- `T:27.6,H:69.2`
- `{"temp": 27.6, "humi": 69.2}`
- `27.6, 69.2`

---

## 七、嵌入式物理化计划（进行中）

当前代码是桌面 PySide6 应用，"嵌入式"部分为软件 stub。正在物理化为基于 RK3588 的边缘设备。

### 7.1 硬件方案

| 品项 | 型号 | 预算 |
|---|---|---|
| 主控 | Orange Pi 5 / Rock 5B 4GB（RK3588S，6 TOPS NPU） | ¥550–650 |
| 存储 | SanDisk Extreme 64GB A2 V30 TF 卡 | ¥80 |
| 电源 | 5V/4A Type-C PD | ¥40 |
| 散热 | 4010 风扇 + 导热硅脂 | ¥20 |
| 摄像头 ×2 | USB UVC 1080P 免驱 | ¥100 |
| STM32 | F103C8T6 蓝色药丸 | ¥15 |
| 继电器 | 5V 光耦隔离单路 | ¥8 |
| 温湿度 | SHT31 / DHT22 | ¥15–20 |
| USB-TTL | CH340 | ¥8 |
| 杜邦线 + 面包板 | — | ¥30 |
| **合计** | | **~¥870** |

### 7.2 移植待办

- [ ] **模型转换**：`yolo export model=ppe_best.pt format=rknn`（在 x86 用 `rknn-toolkit2`）
- [ ] **推理重写**：`dual_inference.py` 用 `rknnlite.API` 替换 ultralytics 调用
- [ ] **STM32 固件**：实现 `0xAA..0x55` 协议、SHT31 I²C 采集、继电器 GPIO 控制、串口上报
- [ ] **强电演示回路**：220V 插座 → 继电器常开端 → 加热器（首次用 12V 电烙铁验证）
- [ ] **RK3588 系统部署**：装 Linux + rknn-toolkit2 runtime + Qt 依赖
- [ ] **触摸屏 / VNC**：UI 远程访问方案

### 7.3 已知代码移植点

| 位置 | 改造方向 |
|---|---|
| `dual_inference.py:154` `YOLO(ppe_path)` | 换 `rknnlite.API` 加载 `.rknn` |
| `dual_inference.py:393,453` `self.ppe_model(...)` | 换 RKNN 推理 API |
| `dual_inference.py:78` `torch.set_num_threads(2)` | RK3588 上无意义，删 |
| `serial_comm.py:10` `port='/dev/ttyUSB0'` | 改 RK3588 实际串口节点 |
| `sensor_worker.py:54` `port="/dev/ttyUSB0"` | 同上 |

---

## 八、清理记录（2026-10-14）

从 ~18 GB 瘦身到 243 MB：

- 删除两个提交压缩包（3.7G + 3.6G）
- 删除 PC 已编译产物（4.1G Linux ELF + 1.2G Windows dist + 189M build 中间产物）
- 删除整个"可安装软件"冗余目录（6.6G）
- 删除顶层重复的 `environment.yml`、`PPT文件.pptx`、Office 临时锁文件
- 删除 02_ 内薄包装启动脚本（保留顶层完整版）
- 清理所有 `__pycache__/` 和 `.venv` 软链接
- 同步"可安装软件"目录下更新的 `dual_inference.py`（新增摄像头后端自动探测）到 02_

⚠ **PC 路线已编译产物已删除，不可逆回退**。如需 PC 演示，重新 `pyinstaller LabSafetyMonitor.spec` 打包。

---

## 九、已知遗留问题

1. `LabSafetyMonitor/inference_logic.py` 是早期 Mock 版本，已被 `dual_inference.py` 完全取代，可删除。
2. `LabSafetyMonitor/main_gui.py` 是兼容入口，建议统一用 `run_app.py`。
3. `dual_inference.py` 默认 `device="cpu"`（环境变量 `INFERENCE_DEVICE` 可改），RK3588 上要走 RKNN 路径不能直接用。
4. `serial_comm.py` 的 `_receive_loop` 假设帧固定 8 字节，与协议文档 6 字节定义有出入，需统一。
5. `env_controller.py` 中阶梯温升目标 72℃、步长 1.2℃/500ms 是为答辩演示设计，非真实工况。

---

## 十、参考文档

- `01_项目文档/` — 7 篇 docx 设计文档（需求/概设/详设/存储/界面/手册/答辩）
- `01_项目文档/湖南涉外经济学院3号+嵌入式开发+系统设计说明书.pdf` — 综合合订本
- `湖南涉外经济学院-3队-嵌入式-演示视频.mp4` — 演示视频
- `LabSafetyMonitor/Project_Summary.md` — 历史项目摘要
