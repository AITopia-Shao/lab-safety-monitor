"""不依赖 Qt/YOLO 的分层智能体与联动决策，便于独立验证。"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import threading
import time
from uuid import uuid4


def normalized(value):
    return str(value).strip().casefold().replace('_', ' ')


def matches(labels, configured):
    allowed = {normalized(x) for x in configured}
    return any(normalized(x) in allowed for x in labels)


@dataclass
class Observation:
    node_id: str
    timestamp: float
    labels: list = field(default_factory=list)
    status: str = 'LIVE'
    detail: str = ''
    frame: object = field(default=None, repr=False)

    def fresh(self, now, ttl):
        return self.status in {'LIVE', 'SIMULATION'} and 0 <= now - self.timestamp <= ttl


@dataclass
class Telemetry:
    timestamp: float
    temperature: float
    humidity: float
    simulated: bool = True

    def fresh(self, now, ttl):
        return (0 <= now - self.timestamp <= ttl and math.isfinite(self.temperature)
                and math.isfinite(self.humidity) and -40 <= self.temperature <= 150 and 0 <= self.humidity <= 100)


class MicroAgent:
    def __init__(self, config):
        self.config = config
        self.observation = None
        self.telemetry = None

    def assess(self, now, rules, unattended):
        vision_ok = self.observation is not None and self.observation.fresh(now, rules['freshness_seconds'])
        sensor_ok = self.telemetry is not None and self.telemetry.fresh(now, rules['freshness_seconds'])
        labels = self.observation.labels if vision_ok else []
        fire = matches(labels, self.config['camera']['fire_labels']) if vision_ok else None
        # 只有加热设备类别才构成加热线索；容器类别本身不等于正在加热。
        heating = matches(labels, self.config['camera']['heating_labels']) if vision_ok else None
        dry = None
        if vision_ok and sensor_ok and unattended is not None:
            dry = heating and unattended and self.telemetry.temperature > rules['high_temperature']
        return dict(id=self.config['id'], name=self.config['name'], camera_id=self.config['camera']['id'],
                    vision_ok=vision_ok, sensor_ok=sensor_ok, labels=labels, fire=fire, dry=dry,
                    heating=heating, temperature=self.telemetry.temperature if sensor_ok else None,
                    humidity=self.telemetry.humidity if sensor_ok else None,
                    simulated=self.telemetry.simulated if sensor_ok else None,
                    status=self.observation.status if self.observation else 'WAITING',
                    detail=self.observation.detail if self.observation else '等待微观画面')


class LaboratoryAgent:
    def __init__(self, config):
        self.config = config
        self.macro = None
        self.benches = {b['id']: MicroAgent(b) for b in config['benches']}
        self.absent_since = None
        self.manual_fire = False

    def receive(self, observation, ttl):
        if observation.node_id == self.config['macro']['id']:
            previous = self.macro
            # 乱序的旧消息不可覆盖新状态，也不可推进无人计时。
            if previous and observation.timestamp <= previous.timestamp:
                return
            continuous = previous is not None and observation.timestamp - previous.timestamp <= ttl
            if observation.status not in {'LIVE', 'SIMULATION'}:
                self.absent_since = None
            elif matches(observation.labels, self.config['macro']['person_labels']):
                self.absent_since = None
            elif self.absent_since is None or not continuous:
                self.absent_since = observation.timestamp
            self.macro = observation
            return
        for bench in self.benches.values():
            if observation.node_id == bench.config['camera']['id']:
                if bench.observation is None or observation.timestamp > bench.observation.timestamp:
                    bench.observation = observation
                return

    def assess(self, now, rules):
        macro_ok = self.macro is not None and self.macro.fresh(now, rules['freshness_seconds'])
        unattended = None
        if macro_ok:
            unattended = self.absent_since is not None and now - self.absent_since >= rules['unattended_seconds']
        micro = [b.assess(now, rules, unattended) for b in self.benches.values()]
        conditions = {'manual_fire': (self.manual_fire, 'FIRE', '人工报告火情', 'manual', None)}
        macro_fire = matches(self.macro.labels, self.config['macro']['fire_labels']) if macro_ok else None
        origin = 'simulation' if self.macro and self.macro.status == 'SIMULATION' else 'vision'
        conditions['macro_fire'] = (macro_fire, 'FIRE', '宏观视觉火情线索', origin, self.config['macro']['id'])
        for item in micro:
            observation = self.benches[item['id']].observation
            origin = 'simulation' if observation and observation.status == 'SIMULATION' else 'vision'
            conditions['micro_fire:' + item['id']] = (item['fire'], 'FIRE', '微观视觉火情线索', origin, item['id'])
            mixed = origin == 'simulation' or item['simulated'] is True or (self.macro and self.macro.status == 'SIMULATION')
            conditions['dry:' + item['id']] = (item['dry'], 'DRY_BURN', '无人值守高温加热风险',
                                               'simulation' if mixed else 'vision+telemetry', item['id'])
        warnings = []
        if not macro_ok:
            warnings.append('宏观视觉数据不可用/过期，无人值守状态未知')
        if macro_ok and any(normalized(x).startswith('no ') or normalized(x) in {'eating', 'drinking'} for x in self.macro.labels):
            warnings.append('宏观检测到防护/饮食违规线索')
        for item in micro:
            if not item['vision_ok']:
                warnings.append(item['name'] + '：微观视觉不可用/过期')
            if not item['sensor_ok']:
                warnings.append(item['name'] + '：温湿度不可用/过期')
            elif item['humidity'] > rules['high_humidity']:
                warnings.append(item['name'] + '：高湿度')
        return dict(id=self.config['id'], name=self.config['name'], unattended=unattended,
                    person_evidence=matches(self.macro.labels, self.config['macro']['person_labels']) if macro_ok else None,
                    macro_ok=macro_ok, macro_status=self.macro.status if self.macro else 'WAITING',
                    macro_labels=self.macro.labels if macro_ok else [], benches=micro, warnings=warnings), conditions


class BuildingCoordinator:
    """实验室上报 → 母系统路由 → 目标实验室执行 → 回执；同一指令幂等。"""
    def __init__(self, config, dispatch=None, state_dir=None, clock=time.time, event_sink=None):
        self.config = deepcopy(config)
        self.clock = clock
        self.event_sink = event_sink
        self.lock = threading.RLock()
        self.labs = {lab['id']: LaboratoryAgent(lab) for lab in config['labs']}
        self.node_labs = {}
        for lab in config['labs']:
            self.node_labs[lab['macro']['id']] = lab['id']
            self.node_labs.update({b['camera']['id']: lab['id'] for b in lab['benches']})
        self.events, self.commands, self.power_latches = {}, {}, {}
        self.records = deque(maxlen=300)
        self.sequence = 0
        self.dispatch = dispatch or self._simulate_dispatch
        self.state_dir = Path(state_dir) if state_dir else None
        if self.state_dir:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            state = self.state_dir / 'state.json'
            if state.exists():
                saved = json.loads(state.read_text(encoding='utf-8'))
                if saved.get('building_id') != config['building']['id']:
                    raise ValueError('运行状态属于另一栋楼，请使用独立的状态目录')
                self.events = saved.get('events', {})
                self.commands = saved.get('commands', {})
                self.power_latches = saved.get('power_latches', {})
                self.sequence = saved.get('sequence', 0)
                for lab_id, active in saved.get('manual_reports', {}).items():
                    if lab_id in self.labs:
                        self.labs[lab_id].manual_fire = bool(active)
                for event in self.events.values():
                    if not event['acknowledged'] and event['lab_id'] not in self.labs:
                        raise ValueError('拓扑删除了尚有未确认事件的实验室，请恢复原拓扑并处理事件')
                    if not event['acknowledged']:
                        lab = self.labs[event['lab_id']]
                        keys = {'manual_fire', 'macro_fire'} | {
                            prefix + bench for bench in lab.benches for prefix in ('micro_fire:', 'dry:')}
                        if event['condition_key'] not in keys:
                            raise ValueError('拓扑删除了尚有未确认事件的实验台，请恢复原拓扑并处理事件')
                    event['condition_active'] = None  # 重启后必须重新获取证据。
                for command in self.commands.values():
                    if command['status'] == 'PENDING':
                        command.update(status='UNKNOWN', detail='程序在执行完成前中断，需要人工核查，未自动重发')
                        if command['action'] == 'power_off':
                            self.power_latches[command['target_lab']] = 'UNKNOWN'
                self.record('STARTUP', message='已恢复未确认事件与断电指令记录')

    def record(self, kind, **data):
        self.sequence += 1
        record = {**data, 'seq': self.sequence, 'timestamp': self.clock(), 'kind': kind}
        self.records.append(record)
        if self.state_dir:
            with (self.state_dir / 'events.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        if self.event_sink:
            self.event_sink(deepcopy(record))
        return record

    def persist(self):
        if self.state_dir:
            target = self.state_dir / 'state.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(building_id=self.config['building']['id'], sequence=self.sequence, events=self.events,
                                                commands=self.commands, power_latches=self.power_latches,
                                                manual_reports={key: lab.manual_fire for key, lab in self.labs.items()}),
                                            ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)

    def ingest(self, observation):
        with self.lock:
            if observation.node_id not in self.node_labs:
                raise ValueError('未知视觉节点：' + observation.node_id)
            now = self.clock()
            if not math.isfinite(observation.timestamp) or observation.timestamp > now + 0.5:
                return
            self.labs[self.node_labs[observation.node_id]].receive(observation, self.config['rules']['freshness_seconds'])

    def update_telemetry(self, lab_id, bench_id, telemetry):
        with self.lock:
            bench = self.labs[lab_id].benches[bench_id]
            if bench.telemetry is None or telemetry.timestamp > bench.telemetry.timestamp:
                bench.telemetry = telemetry

    def manual_report(self, lab_id, active):
        with self.lock:
            self.labs[lab_id].manual_fire = bool(active)
            self.record('MANUAL_REPORT', lab_id=lab_id, active=bool(active))
            self.persist()
            self.tick()

    def _simulate_dispatch(self, command, callback):
        callback(command['id'], 'SIMULATED', '模拟执行完成，未操作硬件')

    def _issue(self, event, target, action):
        key = f'{event["id"]}:{target}:{action}'
        if key in self.commands:
            return
        command = dict(id=key, event_id=event['id'], source_lab=event['lab_id'], target_lab=target,
                       action=action, origin=event['origin'], status='PENDING', detail='等待目标实验室执行',
                       timestamp=self.clock())
        self.commands[key] = command
        if action == 'power_off':
            self._refresh_power(target)
        self.record('COMMAND', **deepcopy(command))
        # 先落盘再执行，重启后不会把中断的命令误认成从未发送。
        self.persist()
        try:
            self.dispatch(deepcopy(command), self.complete_command)
        except Exception as exc:
            self.complete_command(key, 'FAILED', str(exc))

    def complete_command(self, command_id, status, detail):
        with self.lock:
            command = self.commands[command_id]
            if command['status'] != 'PENDING':
                return
            command.update(status=status, detail=detail, completed_at=self.clock())
            if command['action'] == 'power_off':
                self._refresh_power(command['target_lab'])
            self.record('RECEIPT', **deepcopy(command))
            self.persist()

    def _refresh_power(self, lab_id):
        # 模拟回执不能覆盖实际发送/失败/中断记录；展示最需要核查的状态。
        priority = {'SIMULATED': 1, 'PENDING': 2, 'SENT': 3, 'ACKNOWLEDGED': 3, 'FAILED': 4, 'UNKNOWN': 5}
        statuses = [c['status'] for c in self.commands.values()
                    if c['target_lab'] == lab_id and c['action'] == 'power_off']
        if statuses:
            self.power_latches[lab_id] = max(statuses, key=lambda status: priority.get(status, 5))

    def _route(self, event):
        for action in ('alarm', 'power_off'):
            self._issue(event, event['lab_id'], action)
        if event['event_type'] == 'FIRE':
            policy = self.config['fire_linkage']
            targets = list(self.labs) if policy['targets'] == 'all' else policy['targets']
            for target in targets:
                for action in policy['actions']:
                    self._issue(event, target, action)

    def tick(self):
        with self.lock:
            now = self.clock()
            changed = False
            for lab_id, lab in self.labs.items():
                view, conditions = lab.assess(now, self.config['rules'])
                for key, (active, event_type, title, origin, node_id) in conditions.items():
                    existing = next((e for e in self.events.values() if e['lab_id'] == lab_id
                                     and e['condition_key'] == key and not e['acknowledged']), None)
                    if existing:
                        if existing['condition_active'] != active:
                            existing['condition_active'] = active
                            self.record('CONDITION', event_id=existing['id'], active=active)
                            changed = True
                        if active is True:
                            self._route(existing)
                        continue
                    if active is not True:
                        continue
                    event = dict(id=uuid4().hex, lab_id=lab_id, node_id=node_id, condition_key=key,
                                 event_type=event_type, title=title, origin=origin, level='CRITICAL',
                                 timestamp=now, condition_active=True, acknowledged=False,
                                 evidence=dict(unattended=view['unattended'], macro_labels=view['macro_labels'],
                                               benches=view['benches']))
                    self.events[event['id']] = event
                    self.record('EVENT', **deepcopy(event))
                    # 实验室先执行本地措施；母系统只对火情执行楼宇联动。
                    if event_type == 'FIRE':
                        policy = self.config['fire_linkage']
                        targets = list(self.labs) if policy['targets'] == 'all' else policy['targets']
                        self.record('BUILDING_LINKAGE', event_id=event['id'], source_lab=lab_id, targets=targets)
                    self._route(event)
                    changed = True
            if changed:
                self.persist()

    def acknowledge(self):
        with self.lock:
            pending = [e for e in self.events.values() if not e['acknowledged']]
            if any(e['condition_active'] is not False for e in pending):
                raise ValueError('危险仍存在或证据已过期。先解除火情/模拟场景并恢复数据，再确认告警。')
            pending_ids = {e['id'] for e in pending}
            if any(c['event_id'] in pending_ids and c['status'] == 'PENDING' for c in self.commands.values()):
                raise ValueError('联动指令仍在执行，请等待执行回执后确认告警')
            for event in pending:
                event['acknowledged'] = True
                self.record('ACKNOWLEDGED', event_id=event['id'], message='人工确认，不发送恢复供电指令')
            self.persist()

    def reset_simulated_power(self):
        with self.lock:
            if any(not e['acknowledged'] for e in self.events.values()):
                raise ValueError('请先解除危险并确认告警')
            if any(l.config['actuator']['mode'] != 'simulation' for l in self.labs.values()):
                raise ValueError('仅允许全部执行器为模拟模式时复位演示记录')
            if any(status != 'SIMULATED' for status in self.power_latches.values()):
                raise ValueError('存在真实发送、失败或未知执行记录，不能作为模拟记录复位')
            self.power_latches.clear()
            self.record('SIMULATION_RESET', message='仅复位模拟断电记录')
            self.persist()

    def snapshot(self):
        with self.lock:
            now = self.clock()
            open_ids = {e['id'] for e in self.events.values() if not e['acknowledged']}
            views = []
            for lab_id, lab in self.labs.items():
                view, _ = lab.assess(now, self.config['rules'])
                power = self.power_latches.get(lab_id, 'NONE')
                if power in {'SENT', 'FAILED', 'UNKNOWN', 'PENDING', 'ACKNOWLEDGED'}:
                    view['warnings'].append('断电执行或实际供电状态待人工核查')
                affected = (any(e['lab_id'] == lab_id and e['id'] in open_ids for e in self.events.values())
                            or any(c['target_lab'] == lab_id and c['event_id'] in open_ids for c in self.commands.values()))
                view.update(level='CRITICAL' if affected else ('WARNING' if view['warnings'] else 'NORMAL'),
                            alarm=affected, power=power,
                            manual_fire=lab.manual_fire)
                views.append(view)
            level = 'CRITICAL' if any(v['level'] == 'CRITICAL' for v in views) else (
                'WARNING' if any(v['level'] == 'WARNING' for v in views) else 'NORMAL')
            return deepcopy(dict(building=self.config['building'], level=level, labs=views,
                                 events=list(self.events.values()), commands=list(self.commands.values()), records=list(self.records)))

    def frame(self, node_id):
        with self.lock:
            lab = self.labs[self.node_labs[node_id]]
            obs = lab.macro if lab.config['macro']['id'] == node_id else next(
                b.observation for b in lab.benches.values() if b.config['camera']['id'] == node_id)
            return obs.frame if obs else None
