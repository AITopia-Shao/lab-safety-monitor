# AGENTS.md：给 AI 助手的项目说明

> 读者是 AI 编程助手（Claude Code、Codex、Cursor、ChatGPT 等）。队员把仓库交给你、问你问题时，**先读完这份文件**，再按下文"去哪找"只打开相关的 1–3 个文件，不要通读整个仓库。
> 给人看的入门文档是 `docs/零基础上手指南.md`。提问的队员多数是零基础，回答请用大白话，少用术语。
> 本文件与代码同步维护；如果发现和代码不一致，以代码为准，并告诉提问者这里需要更新。

## 1. 项目一句话

化学实验室安全监测终端（第 22 届计算机设计大赛·嵌入式赛道，湖南涉外经济学院 3 队）。两个 USB 摄像头 + YOLO11 识别人员装备和台面器皿；ESP32 下位机采集温湿度 / 红外测温 / 烟雾并控制加热继电器；开发板（RK3588，Python）把识别结果和传感器数据交给规则判断，危险时让下位机断电。另有实验台小屏、网页大屏、安全助手（大模型）、温度预测、外部温湿度控制，以及可选的 V-JEPA 2 画面异常检测。

## 1.1 分层扩展（2026-10-22）

- `python run_app.py --hierarchy` 单独打开楼宇母系统；原 desktop/edge/headless 和它们的现有功能保持独立，不自动叠加运行。
- 拓扑图位于 `labguard/config/hierarchy.json`，文件位置和状态目录通过 `cfg("hierarchy.topology_file")`、`cfg("hierarchy.state_dir")` 读取，默认值同时定义于 `src/config.py` 和 `config.yaml`。
- `src/hierarchy/coordinator.py` 包含 MicroAgent、LaboratoryAgent、BuildingCoordinator；分层安全判断同样独立于 UI，不替换原 `SafetyEngine` 的 R1–R9。
- `src/hierarchy/vision.py` 提供多路输入与共享模型池；`runtime.py` 管理线程、独占状态目录和异步执行。真实执行使用既有 SerialComm CRC16/ACK 协议与显式目标端口，不得恢复早期五字节原型协议。
- `src/ui/hierarchy_window.py` 是独立新增界面，支持拓扑、演练、联动与回执。原界面样式不改。
- 演练事件不得操作真实串口；真实执行器实验室禁止配置模拟视觉/温湿度。分层事件由主入口提供的 sink 同步到统一 `events.log()`，JSONL 只是楼宇指令审计与恢复记录。
- 宏观掉线不得等于无人值守；各实验台证据隔离；指令按事件/目标/动作去重。无通电接口，人工确认与模拟记录清理不恢复供电。
- 新增测试：`test_hierarchy.py` 和 `test_hierarchy_gui.py`。完整指南见 `docs/分层系统升级说明与操作教程.md`。

## 2. 不可违反的设计约束（改代码前必读）

1. **软件只能断电，不能通电。** 代码里不存在任何"通电 / 解锁继电器"的接口；恢复供电只能现场长按控制盒复位键，下位机复查温度和烟雾后才通电。`tests/test_platform.py` 会检查网页路由里没有 power/on、unlock、reset、release 之类的接口。
2. **大模型不碰电闸。** 安全助手（`src/agent/`）的工具全部只读（`TOOLS` 里只有 get_status / get_recent_events / get_forecast / search_manual），测试会检查工具名里没有 power / relay / reset / unlock / cut。V-JEPA 2 和温度预测也只产生提醒，不参与断电。
3. **安全判断不依赖任何界面。** 判断在 `src/core/engine.py` 的 `SafetyEngine` 里完成；小屏、电脑大窗口、网页大屏都只是显示和操作入口。不要把判断逻辑写回界面里。
4. **下位机本地硬规则独立于开发板**：热源 ≥150℃、烟雾 ≥2500、30 秒收不到心跳且仍在加热 → 断电；上电时继电器默认断开。这些阈值写在固件里，网页上改不了。
5. **演示数据不能和真实数据混用**：接上下位机后 `/api/demo/start` 返回 409。
6. **API Key 只放环境变量**（默认 `DASHSCOPE_API_KEY`），网页上填的 Key 只存在内存里，不写文件、不回显。不要把 Key 写进 `config.yaml` 或提交到仓库。
7. **界面文案用大白话**，不要写"感知 → 预测 → 研判 → 执行"这类口号，也不要用"研判、闭环、态势、赋能"这类词。界面布局和配色是队长精心设计的，除非被要求，否则不要改。

## 3. 仓库结构

```
AGENTS.md            ← 本文件
README.md            给人看的项目首页
labguard/            全部代码（下文路径都相对这里）
docs/                比赛文档(docx/pdf)、零基础上手指南.md、上手指南图片/、开发记录/
slides/              答辩 PPT
local_private/       本地私有资料（演示视频、报名表），.gitignore 忽略，不在仓库里
```

`labguard/` 内部（行数为大致规模）：

| 路径 | 作用 |
|---|---|
| `run_app.py` (76) | 唯一入口。`--mode desktop`（默认：电脑大窗口 + 网页）/ `edge`（开发板：小屏全屏 + 网页）/ `headless`（只有网页）；`--no-web`；`--windowed`（小屏以窗口显示，调试用） |
| `config.yaml` (147) / `src/config.py` (191) | 所有阈值和设置。`cfg("rules.dry_burn_c")` 读取；`set_value()` 运行时修改并写入 `runtime/config_override.yaml`（覆盖 config.yaml 同名项）；环境变量 `LAB_SAFETY_CONFIG` 可指定其他配置文件 |
| `src/core/engine.py` (295) | **安全核心**。`SafetyEngine(QObject)`：持有识别线程、传感器线程、状态机、串口；每帧 / 每次遥测 / 每秒调用 `state_machine.evaluate()`；`snapshot()` 输出给所有界面的 JSON 状态；`cut_power()`、`mute()`、`call_admin()`；`attach_extensions()` 挂载 env / demo / jepa / agent |
| `src/core/state_machine.py` (437) | 规则 R1–R9、断电与锁定、现场复位同步、`manual_power_off()` |
| `src/core/world_model.py` (207) | 温度预测 `ThermalWorldModel`（见第 6 节） |
| `src/core/events.py` (381) | 事件中心 `events`（全局单例）：上升沿 / 下降沿、防抖、语音、灯、webhook、SQLite、截图、历史、监听者、复核与处理建议写回 |
| `src/core/telemetry.py` (46) | 全局遥测快照 `telemetry`（环境温湿度、热源温度、烟雾、来源） |
| `src/core/env_control.py` (142) | 排风扇 / 加湿片：`control_law()` 纯函数 + `EnvController` |
| `src/core/demo.py` (105) | 演示数据注入 `DemoInjector`（场景：normal / heating / dry_burn / smoke / humid / dry_air / hot / custom） |
| `src/core/jepa_link.py` (48) | 接收电脑端 V-JEPA 2 上报的异常度 |
| `src/hardware/sensor_worker.py` (180) | 遥测来源切换：下位机 → 演示数据 IPC 文件 → 自主模拟 |
| `src/agent/agent.py` (297) | 安全助手 `SafetyAgent`：自动给警告 / 紧急事件写处理建议、问答、实验安全报告；无大模型时用规则模板 `RULE_ADVICE` |
| `src/agent/llm_client.py` (173) | OpenAI 兼容客户端，线路 `agent.route`：auto（远程→本地）/ remote / local / off；只用标准库 urllib |
| `src/agent/rag.py` (76) | 安全手册检索：`knowledge/*.md` 按"## 标题"切条，中文二元组 + BM25 |
| `src/web/server.py` (433) + `src/web/static/` | 网页大屏（FastAPI + 原生 JS，无构建、无 CDN）。口令鉴权；接口见第 8 节 |
| `src/ui/kiosk.py` (593) | 实验台小屏（PyQt/PySide，800×480 为基准自适应） |
| `src/ui/main_window.py` (1590) | 电脑大窗口（项目最早的完整界面）；设计令牌类 `T`、`Card`、`Chip`、`load_pixel_font()` 被小屏复用 |
| `LabSafetyMonitor/dual_inference.py` (849) | 摄像头读取 + 两个 YOLO 模型推理 + 在场判断 + 时间投票 + 伏案 + 桌边线；产出 `alarms` 字典和带框画面 |
| `LabSafetyMonitor/serial_comm.py` (481) | 下位机串口协议（第 7 节）；单例 `SerialComm.get_shared()` |
| `firmware/lab_guard_esp32/lab_guard_esp32.ino` (343) | ESP32-S3 固件（Arduino）。**尚未在真机上编译验证** |
| `hub/jepa_worker.py` (247) | 电脑端 V-JEPA 2 画面异常检测（可选）；`--selftest` 不下载模型自检 |
| `knowledge/实验室安全手册.md` | 安全助手引用的手册条款，可按学校制度修改 |
| `deploy/orangepi/` | 开发板安装（setup.sh）、开机自启（start.sh，自动选 edge / headless）、串口 udev 规则 |
| `tools/` | `serial_probe.py` 串口调试、`export_rknn.py` 导出 NPU 模型、`gen_voice.py` 生成语音文件 |
| `tests/` | `test_backend.py`（41 项）、`test_platform.py`（39 项）、`fake_mcu.py`（Python 版下位机，协议与固件一致） |
| `models/` | `ppe_best.pt`（人员装备）、`chem_best.pt`（器皿）。有 `*_rknn_model/` 目录时优先用 NPU |
| `runtime/` | 运行时生成，不入库：`events.db`、`snapshots/`、`web_token.txt`、`config_override.yaml` |

## 4. 数据流

```
摄像头 ─→ dual_inference（YOLO×2、投票）─→ alarms{unattended, chem_items, ppe_violation, ...} ─┐
ESP32 ─串口→ serial_comm → sensor_worker → telemetry{temp, humi, obj_temp, smoke}  ───────────┤
                                                                                                ↓
                                   SafetyEngine._evaluate() → SafetyStateMachine.evaluate()
                                     ├→ world_model.update()（温度预测）
                                     ├→ events.update(kind, active, ...)（事件、语音、灯、截图、数据库）
                                     └→ 需要断电：serial.send_power_off() → ESP32 断继电器并回 ACK
                                                                                                ↓
        SafetyEngine.snapshot() ──→ 小屏 kiosk（0.5 s 刷新）/ 电脑大窗口（Qt 信号）/ 网页（WebSocket 2 Hz）
        events 监听者 ──→ SafetyAgent 后台线程 → 大模型或规则模板 → events.set_agent_note()
```

## 5. 安全规则（`state_machine.py`，阈值在 `config.yaml → rules`）

| 编号 | 条件（默认值） | 结果 |
|---|---|---|
| R1 干烧 | 热源 ≥ `dry_burn_c` 120℃ | CRITICAL，断电 |
| R2 升温异常 | ≥ `rate_min_c` 80℃ 且 ≥ `rate_c_per_s` 2℃/s | WARNING |
| R3 加热无人 | 热源 ≥ `heating_on_c` 50℃ 且无人；持续 `unattended_heating_cut_s` 60 s | WARNING → CRITICAL 断电 |
| R4 兜底 | 无热源测温 + 可加热器皿 + 无人 + 环境 > `fallback_ambient_cut_c` 55℃ | CRITICAL，断电 |
| R5 烟雾 | ≥ `smoke_raw` 1800 | CRITICAL，断电 |
| R6 高湿 | > `humidity_high` 80% | WARNING |
| R7 环境高温 | > `ambient_high_c` 40℃ | WARNING |
| R8 量器被加热 | 加热中且台面有容量瓶 / 量筒等 | NOTICE |
| R9 预测干烧 | 越过沸点仍升温且预计 ≤ `predict_eta_warn_s` 90 s 到 120℃；或沸腾 ≥ `boil_unattended_warn_s` 300 s 且无人 | WARNING，**不断电** |

- 视觉事件（`dual_inference.py`）：`ppe_goggles / ppe_coat / ppe_gloves / ppe_mask / ppe_headmask / eating / sleeping / edge_risk`，经 `voting`（3 秒窗口内 ≥60% 帧命中）确认。
- 人员在场：PPE 模型检测到任意一类装备即视为有人（模型里没有 person 类）；连续 `presence.absent_seconds` 15 s 无检测才算无人值守。
- 防抖：器皿识别结果保留 `rules.item_hold_s` 3 s；同类事件解除后 `events.retrigger_s` 10 s 内再出现算同一次。
- 锁定：断电后保持 CRITICAL，直到下位机上报复位（事件码 5，或锁定标志由真变假）。本次运行未接入过下位机的纯演示模式，危险解除 `sim_latch_release_s` 10 s 后自动解锁；真实下位机失联后保持锁定，不会退回演示自动解锁。
- 等级：INFO < NOTICE < WARNING < CRITICAL。

## 6. 温度预测（`world_model.py`）

- 每秒用最近 60 s 的热源温度拟合一阶热平衡 dT/dt=(T∞−T)/τ（拟合失败退化为线性）。
- **温度低于沸点（`boil_high_c` 104℃）时只给"多久后沸腾"（`eta_boil_s`），不给干烧 ETA**，预测曲线最多画到沸点——容器里有水时温度会停在 100℃ 附近。
- 越过沸点仍在升温 = 液体已烧干：改用最近 15 s 线性外推，给出 `eta_s`（到 `dry_burn_c` 的秒数）。
- 沸腾平台：92–104℃ 且最近 15 s 斜率 < 0.05℃/s，`plateau_s` 累计时长，风险分随时长上升；无人值守时风险 ×1.5。
- 快照字段：`phase`（no_sensor / idle / warming_up / heating / boiling / steady / cooling / overheat）、`temp`、`slope`、`t_inf`、`tau`、`eta_s`、`eta_boil_s`、`plateau_s`、`risk`、`forecast`（未来 120 s，每 10 s 一个点）、`history`（最近 120 s 实测）。

## 7. 串口协议（`serial_comm.py`，固件与 `tests/fake_mcu.py` 必须一致）

帧：`AA 55 | LEN | SEQ | CMD | PAYLOAD | CRC16-CCITT(小端，覆盖 LEN..PAYLOAD)`，115200 波特。

| 方向 | CMD | 内容 |
|---|---|---|
| 上→下 | 0x01 HEARTBEAT | [灯光等级 0/1/2]，每秒一次 |
| 上→下 | 0x02 POWER_OFF | [原因]；需要 ACK，0.25 s 超时重发，最多 3 次；结果见 `power_off_state` |
| 上→下 | 0x03 PLAY_VOICE | [曲目号]（DFPlayer，`events.voice` 映射，1–13） |
| 上→下 | 0x04 SET_ENV | `<BhBB` 模式(0 自动/1 手动)、温度设定×10、湿度下限、湿度上限 |
| 上→下 | 0x05 SET_ACT | [风扇 PWM 0–100, 加湿 0/1]，手动指令 10 分钟有效 |
| 下→上 | 0x10 TELEMETRY | `<hHhHBB` 温度×10、湿度×10、热源×10、烟雾原始值、flags、断电原因；无效值 0x7FFF / 0xFFFF |
| 下→上 | 0x11 ACK | [被确认的 SEQ, CMD, 结果] |
| 下→上 | 0x12 EVENT | `<Bh` 事件码（1 超温断电、2 烟雾断电、3 看门狗断电、4 执行上位机断电、5 复位成功、6 复位被拒、7 启动）+ 数值 |
| 下→上 | 0x13 ENV_STATUS | [风扇 PWM, 加湿, 模式] |

flags：bit0 继电器闭合、bit1 锁定、bit2 心跳正常、bit3 SHT30 正常、bit4 MLX90614 正常、bit5 MQ-2 预热完成、bit6 本地危险。
ESP32-S3 引脚：SDA 8、SCL 9、MQ-2 1、继电器 10、LED 红/黄/绿 11/12/13、蜂鸣器 14、复位键 4、DFPlayer RX 18 / TX 17、风扇 PWM 15、加湿 16。

## 8. 网页大屏接口（`src/web/server.py`，默认端口 8080）

- 端口被占用（如 Dify / Docker）时自动顺延到 8081、8082……；实际网址打印在启动窗口里，并写入 `runtime/web_url.txt`。`web_port()` 返回实际端口。

- 除 `/`、`/static/*`、`/font/*`、`/api/ping` 外都要口令：请求头 `X-Token` 或 `?token=`。口令为 `web.token`，留空则自动生成并写入 `runtime/web_token.txt`。
- 只读：`GET /api/snapshot`、`WS /ws`（2 Hz 推送 snapshot + 最近事件）、`GET /api/events`、`GET /api/events/{uid}/snapshot.jpg`、`GET /stream/{pano|desk}.mjpg`、`GET /api/frame/{pano|desk}.jpg`、`GET /api/config`、`/api/models`、`/api/media`、`/api/agent`、`/api/env`、`/api/demo`。
- 写：`PATCH /api/config`（仅 `TUNABLE` 白名单 + 范围校验）、`POST /api/model`、`/api/camera`、`/api/power/cut`（需 `{"confirm": true}`，**只能断电**）、`/api/mute`、`/api/events/{uid}/review`、`/api/manual_snapshot`、`/api/agent/config|test|ask|report`、`/api/events/{uid}/agent`、`/api/env/setpoint|manual`、`/api/demo/start|stop`、`/api/jepa/score`。
- 网页 URL 参数：`?token=XXXXXX` 直接登录，`?still=1` 用单张图片轮询代替 MJPEG，`#tab=ov|params|agent|wm|env|review` 打开指定页签。

## 9. 运行与测试

```bash
cd labguard
conda env create -f environment.yml       # 推荐：Python 3.12，电脑和开发板共用；之后 conda activate lab_safety
pip install -r requirements.txt          # 或者不用 conda：Python 3.10–3.12
pip install -r requirements-dev.txt      # 跑测试时补装 httpx
python run_app.py                          # 电脑大窗口 + 网页（网址见启动窗口，默认 http://127.0.0.1:8080）
python run_app.py --mode edge --windowed   # 在电脑上看小屏
python tests/test_backend.py               # 41 项，无需硬件
python tests/test_platform.py              # 39 项，无需硬件和大模型
python -m unittest discover -s tests -p 'test_*_regressions.py' -v  # 40 项回归
python tests/smoke_app.py                  # 三种启动模式；生成测试图片，不打开真实硬件
python hub/jepa_worker.py --selftest       # V-JEPA 2 流程自检（需 requirements-host.txt）
```

- 测试会在临时目录建数据库，不会动 `runtime/`。改完代码两个测试都要显示"全部通过"。
- 回归测试里的浏览器交互检查需要 Node.js；`smoke_app.py` 使用 Qt offscreen 和本机回环端口，检查桌面、小屏、无界面模式的模型推理、网页鉴权、JPEG 与端口顺延。
- 没有摄像头 / 下位机时：网页「参数」页播放演示视频（放在 `labguard/media/` 或仓库根的 `local_private/`）；「环境控制 → 演示数据」注入场景。
- 开发板：`deploy/orangepi/setup.sh` 安装（默认装 Miniforge 并用 `environment.yml` 建 conda 环境；`LABGUARD_USE_VENV=1` 走系统 Python + venv），`start.sh` 自启；NPU 模型用 `tools/export_rknn.py` 在 x86 上导出。
- 闪退排查：Python 报错和底层崩溃都记在 `runtime/crash.log`。PySide6 6.12 在 Python 3.10 / 3.11 上会闪退（界面画几秒就崩），`requirements.txt` 已限制 `<6.12`，`run_app.py` 启动时也会检查。
- `.sh` 必须是 LF 换行、`.bat` 用 CRLF（`.gitattributes` 已规定），否则开发板上的脚本会报 `$'\r': command not found`。

## 10. 常见问题去哪找

| 想知道 / 想改 | 看哪里 |
|---|---|
| 某个阈值、端口、摄像头编号 | `config.yaml`（注释齐全）；默认值在 `src/config.py` 的 `DEFAULTS` |
| 为什么断电 / 为什么报警 | `src/core/state_machine.py` 的 `evaluate()`，事件标题对应规则 |
| 新增一条安全规则 | `state_machine.py`：在 `evaluate()` 里 `add(kind, level, title, message, cut=...)`，并把 kind 加进 `HAZARD_KINDS`；语音曲目加到 `config.yaml → events.voice`；在 `tests/` 补测试 |
| 识别类别、违规映射 | `dual_inference.py` 顶部 `PPE_VIOLATION_KINDS`、`VISUAL_KIND_TITLES` |
| 小屏某个显示 | `src/ui/kiosk.py` 的 `_refresh_guard()` / `_refresh_events()` |
| 网页某个显示 | `src/web/static/app.js` 的 `render*()` 函数；样式 `app.css`；结构 `index.html` |
| 网页数据从哪来 | `SafetyEngine.snapshot()`（`engine.py`） |
| 安全助手说了什么、为什么 | `src/agent/agent.py`：`SYSTEM_PROMPT`、`TOOLS`、`RULE_ADVICE`；线路在 `llm_client.py` |
| 换大模型厂商 | `config.yaml → agent.remote`（OpenAI 兼容即可：base_url、model、api_key_env） |
| 固件引脚、本地规则 | `firmware/lab_guard_esp32/lab_guard_esp32.ino` 顶部常量 |
| 采购清单、硬件接线 | `docs/开发记录/修改说明与落地计划.md` 第 3、4 节 |
| 早期代码审查结论 | `docs/开发记录/代码审查与改进方案.md` |
| 评委视角的问题清单和瘦身计划（待执行） | `docs/开发记录/评委视角审查与瘦身计划.md` |
| 旧版本代码 | git 标签 `v1-prototype`（最初原型）、`v2-before-restructure`（整理目录前） |

## 11. 代码约定

- 注释和界面文字用中文，标识符用英文；新代码跟随所在文件的写法。
- Qt 兼容层：优先 PySide6，回退 PyQt6（`try: from PySide6... except ImportError: from PyQt6...`）。
- 跨线程只通过加锁的快照或 Qt 信号；网页线程不直接碰 numpy 画面，只取 `latest_jpeg()` 编码好的字节。
- 读配置一律用 `cfg("a.b", 默认值)`；新配置项同时加到 `src/config.py` 的 `DEFAULTS` 和 `config.yaml`（带中文注释）。
- 事件统一走 `events.update()`（持续性状态）或 `events.log()`（一次性），不要自己写数据库或播语音。
- 香橙派性能：不要在界面里加动画或高频重绘；小屏画面最多 10 fps；网页画面只在有人看时编码。

## 12. 当前状态

- 已完成并测试通过：安全核心、规则 R1–R9、温度预测、事件中心、网页大屏、实验台小屏、安全助手（三条线路）、外部温湿度控制（上位机 + 固件 + 协议）、演示数据、V-JEPA 2 电脑端流程。
- 未验证：ESP32 固件未在真机编译；开发板（RK3588）还没到手，NPU 推理和开机自启未实测；V-JEPA 2 只用随机小模型跑通流程，正式模型（`facebook/vjepa2-vitl-fpc64-256`，约 1.3GB）的实际检测效果未验证。
- 截图和演示视频是电脑屏幕录像，不是真实实验台画面。
