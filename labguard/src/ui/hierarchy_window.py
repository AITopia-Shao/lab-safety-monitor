"""楼宇母系统工作台：拓扑配置、多微观预览、演练和联动回执。"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
        QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QSplitter, QTabWidget, QScrollArea, QGroupBox,
        QFormLayout, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QDialog, QDialogButtonBox,
        QFileDialog, QMessageBox, QPlainTextEdit, QTableWidget, QTableWidgetItem, QHeaderView)
except ImportError:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QImage, QPixmap
    from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
        QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QSplitter, QTabWidget, QScrollArea, QGroupBox,
        QFormLayout, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QDialog, QDialogButtonBox,
        QFileDialog, QMessageBox, QPlainTextEdit, QTableWidget, QTableWidgetItem, QHeaderView)

from src.hierarchy.config import ROOT, DEFAULT_CONFIG, load_config, save_config, validate_config, make_lab, make_bench
from src.hierarchy.runtime import HierarchyRuntime

STATUS = {'ACKNOWLEDGED': '下位机已确认断电指令', 'NONE': '未请求断电', 'PENDING': '指令等待执行', 'SIMULATED': '模拟执行完成',
          'SENT': '串口已发送 · 未收到硬件回执', 'FAILED': '执行失败 · 请核查', 'UNKNOWN': '执行结果未知',
          'SOFTWARE_ALARM': '软件报警已激活', 'LIVE': '真实模型', 'SIMULATION': '模拟演练',
          'WAITING': '等待数据', 'ERROR': '通道异常'}
LEVEL = {'NORMAL': '正常', 'WARNING': '需关注 / 数据不完整', 'CRITICAL': '紧急事件 / 联动报警'}


def button(text, callback):
    result = QPushButton(text)
    result.clicked.connect(callback)
    return result


class ChannelDialog(QDialog):
    def __init__(self, camera, telemetry=None, actuator=None, parent=None, node_name=None):
        super().__init__(parent)
        self.setWindowTitle('配置独立视觉通道')
        self.resize(700, 700)
        self.camera, self.telemetry, self.actuator = deepcopy(camera), deepcopy(telemetry), deepcopy(actuator)
        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)
        self.node_name = QLineEdit(node_name or camera['name'])
        form.addRow('实验台名称' if telemetry else '实验室名称', self.node_name)
        self.name = QLineEdit(camera['name'])
        form.addRow('通道名称', self.name)
        self.kind = QComboBox()
        for title, key in [('模拟演练', 'simulation'), ('USB 摄像头', 'camera'), ('本地视频', 'video'),
                           ('静态图片', 'image'), ('网络 RTSP/HTTP', 'stream')]:
            self.kind.addItem(title, key)
        self.kind.setCurrentIndex(self.kind.findData(camera['source_kind']))
        form.addRow('视频来源', self.kind)
        self.source = QLineEdit(str(camera['source']))
        self.source.setPlaceholderText('摄像头填 0、1…；其他来源填文件路径或网络地址')
        source_row = QHBoxLayout()
        source_row.addWidget(self.source)
        source_row.addWidget(button('选择素材', self.choose_media))
        form.addRow('来源地址', source_row)
        self.model = QLineEdit(camera['model'])
        model_row = QHBoxLayout()
        model_row.addWidget(self.model)
        model_row.addWidget(button('选择模型', self.choose_model))
        form.addRow('该通道的模型', model_row)
        self.confidence = QDoubleSpinBox()
        self.confidence.setRange(0.01, 1)
        self.confidence.setSingleStep(0.05)
        self.confidence.setValue(camera['confidence'])
        form.addRow('置信度阈值', self.confidence)
        self.fps = QDoubleSpinBox()
        self.fps.setRange(0.2, 30)
        self.fps.setValue(camera['fps'])
        form.addRow('推理频率上限（次/秒）', self.fps)
        self.labels = {}
        for key, title in [('person_labels', '人员存在证据类别'), ('heating_labels', '加热设备类别'), ('fire_labels', '火焰/烟雾类别')]:
            self.labels[key] = QLineEdit(', '.join(camera[key]))
            form.addRow(title + '（逗号分隔）', self.labels[key])
        hint = QLabel('类别映射须与该模型的实际输出一致。现有两份权重不识别火焰、烟雾或加热设备；\n需要专用模型，或使用明确标注的模拟演练。仅检测到烧杯不等于正在加热。')
        hint.setWordWrap(True)
        outer.addWidget(hint)
        if telemetry:
            self.sensor_kind = QComboBox()
            for title, key in [('手动模拟', 'simulation'), ('兼容旧温湿度仿真器', 'legacy_ipc'), ('真实串口传感器', 'serial')]:
                self.sensor_kind.addItem(title, key)
            self.sensor_kind.setCurrentIndex(self.sensor_kind.findData(telemetry['kind']))
            self.sensor_port = QLineEdit(telemetry['port'])
            form.addRow('此实验台温湿度来源', self.sensor_kind)
            form.addRow('此实验台传感器串口', self.sensor_port)
        if actuator:
            self.actuator_mode = QComboBox()
            self.actuator_mode.addItem('模拟执行（默认）', 'simulation')
            self.actuator_mode.addItem('真实串口执行（CRC16 协议与 ACK）', 'serial')
            self.actuator_mode.setCurrentIndex(self.actuator_mode.findData(actuator['mode']))
            self.actuator_port = QLineEdit(actuator['port'])
            form.addRow('此实验室断电执行方式', self.actuator_mode)
            form.addRow('此实验室执行器串口', self.actuator_port)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def choose_media(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择画面素材', str(ROOT), '视频/图片 (*.mp4 *.avi *.mov *.mkv *.png *.jpg *.jpeg *.bmp);;所有文件 (*)')
        if path:
            self.source.setText(path)
            self.kind.setCurrentIndex(self.kind.findData('image' if Path(path).suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp'} else 'video'))

    def choose_model(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 YOLO 检测模型', str(ROOT / 'models'), 'PyTorch 模型 (*.pt)')
        if path:
            self.model.setText(path)

    def result_config(self):
        camera = deepcopy(self.camera)
        source = self.source.text().strip()
        if self.kind.currentData() == 'camera':
            source = int(source)
        camera.update(name=self.name.text().strip(), source_kind=self.kind.currentData(), source=source,
                      model=self.model.text().strip(), confidence=self.confidence.value(), fps=self.fps.value())
        for key, edit in self.labels.items():
            camera[key] = [x.strip() for x in edit.text().replace('，', ',').split(',') if x.strip()]
        telemetry, actuator = deepcopy(self.telemetry), deepcopy(self.actuator)
        if telemetry:
            telemetry.update(kind=self.sensor_kind.currentData(), port=self.sensor_port.text().strip())
        if actuator:
            actuator.update(mode=self.actuator_mode.currentData(), port=self.actuator_port.text().strip())
        return camera, telemetry, actuator


class HierarchyWindow(QMainWindow):
    def __init__(self, config_path=DEFAULT_CONFIG, state_dir=None, autostart=True, event_sink=None):
        super().__init__()
        self.setWindowTitle('分层视觉干预监测系统 · 实验楼母系统')
        self.resize(1440, 900)
        self.event_sink = event_sink
        self.config_path = Path(config_path)
        self.state_dir = Path(state_dir) if state_dir else ROOT / 'runtime' / 'hierarchy'
        self.config = load_config(self.config_path)
        self.runtime = HierarchyRuntime(self.config, self.state_dir, event_sink=self.event_sink)
        self.selected_lab = self.config['labs'][0]['id']
        self.last_seq = 0
        self.previews = {}
        self.bench_labels = {}
        self.setStyleSheet('''
            QMainWindow, QWidget { font-family: "PingFang SC", "Microsoft YaHei", sans-serif; font-size: 13px; }
            QMainWindow { background: #eef2f7; }
            QGroupBox { border: 1px solid #cbd5e1; border-radius: 8px; margin-top: 15px; padding: 12px 8px 6px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; font-weight: bold; color: #0f4c81; }
            QPushButton { padding: 7px 12px; border: 1px solid #aabbd0; border-radius: 5px; background: #fff; }
            QPushButton:hover { background: #e0edf9; }
            QPlainTextEdit, QTreeWidget, QTableWidget { background: white; border: 1px solid #cbd5e1; }
        ''')
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        self.banner = QLabel()
        self.banner.setMinimumHeight(48)
        outer.addWidget(self.banner)
        split = QSplitter()
        outer.addWidget(split, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel('母系统 → 实验室 → 宏观 / 微观')
        self.tree.currentItemChanged.connect(self.selection_changed)
        left_layout.addWidget(self.tree, 1)
        for title, callback in [('新增实验室', self.add_lab), ('新增微观实验台', self.add_bench),
                                ('配置选中通道/模型', self.edit_channel), ('删除选中节点', self.remove_node)]:
            left_layout.addWidget(button(title, callback))
        split.addWidget(left)
        self.tabs = QTabWidget()
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 1160])
        self.monitor_scroll = QScrollArea()
        self.monitor_scroll.setWidgetResizable(True)
        self.tabs.addTab(self.monitor_scroll, '实验室监控与演练')
        event_tab = QWidget()
        event_layout = QVBoxLayout(event_tab)
        controls = QHBoxLayout()
        controls.addWidget(button('确认已解除的告警（不恢复供电）', self.acknowledge))
        controls.addWidget(button('复位模拟断电记录', self.reset_simulation))
        controls.addWidget(button('导出事件、证据与回执', self.export_records))
        event_layout.addLayout(controls)
        self.events_table = self.table(['时间', '来源实验室 / 节点', '事件', '证据来源', '危险条件', '人工确认'])
        self.commands_table = self.table(['目标实验室', '来源实验室', '动作', '执行结果', '详细回执'])
        event_layout.addWidget(QLabel('事件：双击一行查看各实验台证据'))
        event_layout.addWidget(self.events_table, 1)
        self.events_table.cellDoubleClicked.connect(self.show_evidence)
        event_layout.addWidget(QLabel('母系统指令与目标实验室回执：模拟结果、软件报警、串口发送分别记录；真实断电采用仓库现有 CRC16 协议，单独记录下位机 ACK。'))
        event_layout.addWidget(self.commands_table, 1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        event_layout.addWidget(self.log, 1)
        self.tabs.addTab(event_tab, '楼宇事件与跨实验室联动')
        config_tab = QWidget()
        config_layout = QVBoxLayout(config_tab)
        config_layout.addWidget(QLabel('可配置联动目标、动作、阈值、每个通道的权重/视频源与独立串口。保存时会校验拓扑并重新加载。'))
        self.config_edit = QPlainTextEdit()
        config_layout.addWidget(self.config_edit, 1)
        config_buttons = QHBoxLayout()
        config_buttons.addWidget(button('刷新为当前配置', self.refresh_config))
        config_buttons.addWidget(button('校验并保存应用', self.apply_json))
        config_layout.addLayout(config_buttons)
        self.tabs.addTab(config_tab, '拓扑与联动策略 JSON')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(350)
        self.rebuild_tree()
        self.refresh_config()
        if autostart:
            self.runtime.start()
        self.refresh()

    @staticmethod
    def table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        return table

    def current_lab_config(self):
        return next(l for l in self.config['labs'] if l['id'] == self.selected_lab)

    def rebuild_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        self.lab_items = {}
        root = QTreeWidgetItem([self.config['building']['name'] + ' · 母系统'])
        self.tree.addTopLevelItem(root)
        root.setData(0, Qt.ItemDataRole.UserRole, dict(kind='building'))
        for lab in self.config['labs']:
            item = QTreeWidgetItem(root, [lab['name']])
            item.setData(0, Qt.ItemDataRole.UserRole, dict(kind='lab', lab_id=lab['id']))
            self.lab_items[lab['id']] = item
            macro = QTreeWidgetItem(item, ['宏观：' + lab['macro']['name']])
            macro.setData(0, Qt.ItemDataRole.UserRole, dict(kind='macro', lab_id=lab['id']))
            for bench in lab['benches']:
                child = QTreeWidgetItem(item, ['微观：' + bench['name']])
                child.setData(0, Qt.ItemDataRole.UserRole, dict(kind='bench', lab_id=lab['id'], bench_id=bench['id']))
        self.tree.expandAll()
        self.tree.setCurrentItem(self.lab_items[self.selected_lab])
        self.tree.blockSignals(False)
        self.build_monitor()

    def selection_changed(self, item, previous):
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get('lab_id') and data['lab_id'] != self.selected_lab:
            self.selected_lab = data['lab_id']
            self.build_monitor()
        self.refresh()

    def preview(self, camera, parent_layout):
        box = QGroupBox(camera['name'])
        layout = QVBoxLayout(box)
        image = QLabel('等待画面')
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setMinimumSize(300, 180)
        image.setMaximumHeight(260)
        image.setStyleSheet('background: #162436; color: #dbeafe; border-radius: 6px;')
        layout.addWidget(image)
        info = QLabel()
        info.setWordWrap(True)
        layout.addWidget(info)
        self.previews[camera['id']] = (image, info, camera)
        parent_layout.addWidget(box)
        return layout

    def build_monitor(self):
        lab = self.current_lab_config()
        self.previews, self.bench_labels = {}, {}
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.lab_summary = QLabel()
        self.lab_summary.setWordWrap(True)
        layout.addWidget(self.lab_summary)
        self.preview(lab['macro'], layout)
        if lab['macro']['source_kind'] == 'simulation':
            row = QHBoxLayout()
            self.occupied = QCheckBox('演练：宏观有人值守')
            self.occupied.setChecked(self.runtime.get_simulation(lab['macro']['id'])['occupied'])
            self.occupied.toggled.connect(lambda value, n=lab['macro']['id']: self.runtime.set_simulation(n, occupied=value))
            row.addWidget(self.occupied)
            row.addWidget(button('演练火情 → 触发楼宇联动', self.simulate_fire))
            row.addWidget(button('解除本实验室全部模拟危险', self.clear_simulation))
            layout.addLayout(row)
        manual = QHBoxLayout()
        manual.addWidget(button('人工报告火情', self.report_fire))
        manual.addWidget(button('解除人工火情报告', lambda: self.runtime.coordinator.manual_report(self.selected_lab, False)))
        layout.addLayout(manual)
        grid = QGridLayout()
        layout.addLayout(grid)
        for index, bench in enumerate(lab['benches']):
            holder = QWidget()
            sub = QVBoxLayout(holder)
            sub.setContentsMargins(0, 0, 0, 0)
            content = self.preview(bench['camera'], sub)
            self.bench_labels[bench['id']] = QLabel()
            content.addWidget(self.bench_labels[bench['id']])
            if bench['camera']['source_kind'] == 'simulation':
                heating = QCheckBox('演练：加热设备存在')
                heating.setChecked(self.runtime.get_simulation(bench['camera']['id'])['heating'])
                heating.toggled.connect(lambda value, n=bench['camera']['id']: self.runtime.set_simulation(n, heating=value))
                content.addWidget(heating)
            if bench['telemetry']['kind'] != 'serial':
                values = self.runtime.get_simulation(bench['id'])
                row = QHBoxLayout()
                for key, title, low, high in [('temperature', '温度 ℃', -40, 150), ('humidity', '湿度 %', 0, 100)]:
                    spin = QDoubleSpinBox()
                    spin.setRange(low, high)
                    spin.setValue(values[key])
                    spin.valueChanged.connect(lambda value, k=key, n=bench['id']: self.runtime.set_simulation(n, **{k: value}))
                    row.addWidget(QLabel('模拟' + title))
                    row.addWidget(spin)
                content.addLayout(row)
            grid.addWidget(holder, index // 2, index % 2)
        layout.addWidget(QLabel('模拟画面不运行 YOLO。真实通道发生错误会标记异常，不会自动替换视角或伪造检测结果。'))
        layout.addStretch()
        self.monitor_scroll.setWidget(panel)

    def refresh(self):
        snapshot = self.runtime.coordinator.snapshot()
        count = sum(len(l['benches']) for l in snapshot['labs'])
        color = {'NORMAL': '#166534', 'WARNING': '#92400e', 'CRITICAL': '#b91c1c'}[snapshot['level']]
        self.banner.setStyleSheet(f'background:{color}; color:white; padding:10px; border-radius:6px; font-weight:bold;')
        self.banner.setText(f'{snapshot["building"]["name"]} · {LEVEL[snapshot["level"]]}  |  {len(snapshot["labs"])} 间实验室 / {count} 个实验台  |  '
                            + (self.runtime.error or '实验台 → 实验室 → 母系统 → 目标实验室 → 执行回执'))
        for lab in snapshot['labs']:
            self.lab_items[lab['id']].setText(0, f'{lab["name"]} · {LEVEL[lab["level"]]}')
            if lab['id'] != self.selected_lab:
                continue
            occupancy = '未知' if lab['unattended'] is None else ('无人值守超时' if lab['unattended'] else (
                '有人员存在证据' if lab['person_evidence'] else '暂未检测到人员，正在计时'))
            self.lab_summary.setText(f'{lab["name"]}  |  {LEVEL[lab["level"]]}  |  宏观：{occupancy}\n'
                                     f'断电请求：{STATUS.get(lab["power"], lab["power"])}；'
                                     f'报警：{"已激活" if lab["alarm"] else "未激活"}\n' + '；'.join(lab['warnings']))
            for bench in lab['benches']:
                temp = '未知' if bench['temperature'] is None else f'{bench["temperature"]:.1f} ℃'
                humidity = '未知' if bench['humidity'] is None else f'{bench["humidity"]:.1f} %'
                source = '模拟温湿度' if bench['simulated'] else '真实温湿度' if bench['simulated'] is False else '温湿度过期'
                risk = '满足' if bench['dry'] is True else '不满足' if bench['dry'] is False else '数据不足'
                self.bench_labels[bench['id']].setText(f'{source}：{temp} / {humidity}\n加热风险条件：{risk}')
        for node_id, (image, info, camera) in self.previews.items():
            frame = self.runtime.coordinator.frame(node_id)
            lab_agent = self.runtime.coordinator.labs[self.selected_lab]
            with self.runtime.coordinator.lock:
                obs = lab_agent.macro if node_id == lab_agent.config['macro']['id'] else next(
                    b.observation for b in lab_agent.benches.values() if b.config['camera']['id'] == node_id)
                if obs is not None:
                    fresh = obs.fresh(self.runtime.coordinator.clock(), self.config['rules']['freshness_seconds'])
                    info.setText(f'{STATUS.get(obs.status, obs.status)}{" · 数据过期" if not fresh else ""} | '
                                 + ', '.join(obs.labels) + '\n' + obs.detail)
            if frame is not None:
                rgb = frame[:, :, ::-1].copy()
                height, width, _ = rgb.shape
                qimage = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
                image.setPixmap(QPixmap.fromImage(qimage).scaled(image.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.displayed_events = list(reversed(snapshot['events']))[:200]
        event_rows = []
        for event in self.displayed_events:
            active = event['condition_active']
            event_rows.append([datetime.fromtimestamp(event['timestamp']).strftime('%H:%M:%S'),
                event['lab_id'] + ' / ' + str(event.get('node_id') or '人工报告'), event['title'], event['origin'],
                '仍存在' if active is True else '已解除' if active is False else '证据未知',
                '已确认' if event['acknowledged'] else '待确认'])
        self.fill_table(self.events_table, event_rows)
        self.fill_table(self.commands_table, [[c['target_lab'], c['source_lab'], '报警' if c['action'] == 'alarm' else '断电',
                         STATUS.get(c['status'], c['status']), c['detail']] for c in reversed(snapshot['commands'][-300:])])
        for record in snapshot['records']:
            if record['seq'] > self.last_seq:
                self.log.appendPlainText(datetime.fromtimestamp(record['timestamp']).strftime('%H:%M:%S') + '  '
                                         + self.describe_record(record))
                self.last_seq = record['seq']

    def describe_record(self, record):
        names = {lab['id']: lab['name'] for lab in self.config['labs']}
        name = lambda key: names.get(record.get(key), record.get(key, ''))
        action = '报警' if record.get('action') == 'alarm' else '断电'
        kind = record['kind']
        if kind == 'EVENT':
            return f'{name("lab_id")} 上报：{record["title"]}（来源：{record["origin"]}）'
        if kind == 'COMMAND':
            return f'母系统 → {name("target_lab")}：请求{action}，来源 {name("source_lab")}'
        if kind == 'RECEIPT':
            return f'{name("target_lab")} {action}回执：{record["detail"]}'
        if kind == 'BUILDING_LINKAGE':
            return f'母系统受理 {name("source_lab")} 火情，按策略联动：' + '、'.join(names.get(x, x) for x in record['targets'])
        if kind == 'CONDITION':
            return '事件危险条件更新：' + ('仍存在' if record['active'] is True else '已解除' if record['active'] is False else '证据未知')
        if kind == 'MANUAL_REPORT':
            return name('lab_id') + (' 人工报告火情' if record['active'] else ' 解除人工火情报告')
        return record.get('message', kind)

    @staticmethod
    def fill_table(table, rows):
        table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(str(value)))

    def show_evidence(self, row, column):
        QMessageBox.information(self, '事件证据（模拟与真实来源分别标记）', json.dumps(self.displayed_events[row], ensure_ascii=False, indent=2))

    def notify_error(self, error):
        QMessageBox.warning(self, '操作未完成', str(error))

    def apply_config(self, candidate):
        candidate = validate_config(candidate)
        if any(not e['acknowledged'] for e in self.runtime.coordinator.snapshot()['events']):
            raise ValueError('存在未确认事件，先解除危险并确认告警，再调整拓扑')
        self.timer.stop()
        old = self.config
        try:
            self.runtime.stop()
            if any(not e['acknowledged'] for e in self.runtime.coordinator.snapshot()['events']):
                raise ValueError('停止过程中检测到新事件，已保留现场状态，请先处理事件')
            save_config(candidate, self.config_path)
            self.config = candidate
            self.runtime = HierarchyRuntime(candidate, self.state_dir, event_sink=self.event_sink)
            if self.selected_lab not in {l['id'] for l in candidate['labs']}:
                self.selected_lab = candidate['labs'][0]['id']
            self.last_seq = 0
            self.rebuild_tree()
            self.refresh_config()
            self.runtime.start()
        except Exception:
            self.config = old
            save_config(old, self.config_path)
            self.runtime = HierarchyRuntime(old, self.state_dir, event_sink=self.event_sink)
            self.runtime.start()
            raise
        finally:
            self.timer.start(350)

    def selected_data(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else {}

    def add_lab(self):
        cfg = deepcopy(self.config)
        token = uuid4().hex[:8]
        cfg['labs'].append(make_lab('lab-' + token, '新实验室 ' + str(len(cfg['labs']) + 1)))
        try:
            self.apply_config(cfg)
        except Exception as exc:
            self.notify_error(exc)

    def add_bench(self):
        cfg = deepcopy(self.config)
        lab = next(l for l in cfg['labs'] if l['id'] == self.selected_lab)
        lab['benches'].append(make_bench('bench-' + uuid4().hex[:8], '实验台 ' + str(len(lab['benches']) + 1)))
        try:
            self.apply_config(cfg)
        except Exception as exc:
            self.notify_error(exc)

    def edit_channel(self):
        data = self.selected_data()
        if data.get('kind') == 'building':
            self.tabs.setCurrentIndex(2)
            return
        cfg = deepcopy(self.config)
        lab = next(l for l in cfg['labs'] if l['id'] == self.selected_lab)
        bench = next((b for b in lab['benches'] if b['id'] == data.get('bench_id')), None)
        dialog = ChannelDialog(bench['camera'] if bench else lab['macro'], bench['telemetry'] if bench else None,
                               None if bench else lab['actuator'], self, node_name=bench['name'] if bench else lab['name'])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                camera, telemetry, actuator = dialog.result_config()
                if bench:
                    bench.update(name=dialog.node_name.text().strip(), camera=camera, telemetry=telemetry)
                else:
                    lab.update(name=dialog.node_name.text().strip(), macro=camera, actuator=actuator)
                self.apply_config(cfg)
            except Exception as exc:
                self.notify_error(exc)

    def remove_node(self):
        data = self.selected_data()
        cfg = deepcopy(self.config)
        try:
            if data.get('kind') == 'lab':
                cfg['labs'] = [l for l in cfg['labs'] if l['id'] != data['lab_id']]
                targets = cfg['fire_linkage']['targets']
                if isinstance(targets, list):
                    cfg['fire_linkage']['targets'] = [t for t in targets if t != data['lab_id']]
            elif data.get('kind') == 'bench':
                lab = next(l for l in cfg['labs'] if l['id'] == data['lab_id'])
                lab['benches'] = [b for b in lab['benches'] if b['id'] != data['bench_id']]
            else:
                raise ValueError('仅支持删除实验室或微观实验台；每间实验室保留一个宏观通道和至少一个微观通道')
            self.apply_config(cfg)
        except Exception as exc:
            self.notify_error(exc)

    def refresh_config(self):
        self.config_edit.setPlainText(json.dumps(self.config, ensure_ascii=False, indent=2))

    def apply_json(self):
        try:
            self.apply_config(json.loads(self.config_edit.toPlainText()))
        except Exception as exc:
            self.notify_error(exc)

    def simulate_fire(self):
        macro = self.current_lab_config()['macro']
        if macro['source_kind'] != 'simulation':
            return
        self.runtime.set_simulation(macro['id'], fire=True)

    def clear_simulation(self):
        lab = self.current_lab_config()
        for camera in [lab['macro'], *[b['camera'] for b in lab['benches']]]:
            if camera['source_kind'] == 'simulation':
                self.runtime.set_simulation(camera['id'], fire=False, heating=False, occupied=True)
        for bench in lab['benches']:
            if bench['telemetry']['kind'] != 'serial':
                self.runtime.set_simulation(bench['id'], temperature=25.0, humidity=55.0)
        self.build_monitor()

    def report_fire(self):
        text = ('将向母系统报告当前实验室火情，并按当前策略联动实验室报警、请求断电。\n'
                '真实串口执行器可能收到断电指令。仅演示请使用“演练火情”。\n是否报告火情？')
        if QMessageBox.question(self, '人工报告火情', text) == QMessageBox.StandardButton.Yes:
            self.runtime.coordinator.manual_report(self.selected_lab, True)

    def acknowledge(self):
        try:
            self.runtime.coordinator.acknowledge()
        except Exception as exc:
            self.notify_error(exc)

    def reset_simulation(self):
        try:
            self.runtime.coordinator.reset_simulated_power()
        except Exception as exc:
            self.notify_error(exc)

    def export_records(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出楼宇运行记录', '楼宇联动记录.json', 'JSON (*.json)')
        if path:
            try:
                Path(path).write_text(json.dumps(self.runtime.coordinator.snapshot(), ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                self.notify_error(exc)

    def closeEvent(self, event):
        self.timer.stop()
        try:
            self.runtime.stop()
        except RuntimeError as exc:
            print(exc)
        event.accept()
