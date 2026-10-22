"""拓扑配置与校验。配置中的相对文件路径均相对源码目录。"""
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
from src.config import cfg
DEFAULT_CONFIG = ROOT / cfg('hierarchy.topology_file', 'config/hierarchy.json')
ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def resolve_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def make_camera(node_id, name, role):
    return dict(id=node_id, name=name, source_kind='simulation', source='',
                model=f'models/{"ppe" if role == "macro" else "chem"}_best.pt',
                confidence=0.35, fps=3,
                person_labels=['Person', 'Head', 'Drinking', 'Eating', 'Gloves', 'Googles', 'Head Mask',
                               'Lab Coat', 'Mask', 'No Gloves', 'No Head Mask', 'No Lab coat', 'No Mask', 'No googles'],
                heating_labels=['Alcohol lamp', 'Alcohol_lamp', 'burner', 'hotplate', 'heater', '酒精灯', '加热板'],
                fire_labels=['fire', 'flame', 'smoke', '火焰', '烟雾'])


def make_bench(bench_id='bench-1', name='实验台 1'):
    return dict(id=bench_id, name=name, camera=make_camera(bench_id + '-micro', name + ' · 微观', 'micro'),
                telemetry=dict(kind='simulation', port='', baudrate=9600, temperature=25.0, humidity=55.0))


def make_lab(lab_id='lab-101', name='实验室 101'):
    return dict(id=lab_id, name=name, macro=make_camera(lab_id + '-macro', name + ' · 宏观', 'macro'),
                benches=[make_bench(lab_id + '-bench-1')], actuator=dict(mode='simulation', port='', baudrate=115200))


def default_config():
    first, second = make_lab(), make_lab('lab-102', '实验室 102')
    first['benches'].append(make_bench('lab-101-bench-2', '实验台 2'))
    return dict(schema_version=2, building=dict(id='building-a', name='实验楼 A'),
                rules=dict(unattended_seconds=5.0, freshness_seconds=8.0, high_temperature=55.0, high_humidity=80.0),
                fire_linkage=dict(targets='all', actions=['alarm', 'power_off']), labs=[first, second])


def validate_config(raw, check_files=True):
    cfg = deepcopy(raw)
    ids, cameras, serial_ports = set(), set(), set()

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def node(item, label):
        require(isinstance(item, dict), f'{label} 必须是对象')
        value = item.get('id', '')
        require(isinstance(value, str) and ID.fullmatch(value), f'{label} ID 仅可使用英文、数字、下划线和短横线')
        require(value not in ids, f'ID 重复：{value}')
        ids.add(value)
        require(isinstance(item.get('name'), str) and bool(item['name'].strip()), f'{label} 名称不能为空')

    def number(value, low, high, label):
        require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                and low <= value <= high, f'{label} 必须在 {low}–{high} 范围内')

    def serial_port(port):
        require(isinstance(port, str) and bool(port.strip()), '串口模式必须指定端口')
        require(port not in serial_ports, f'串口被多个节点占用：{port}')
        serial_ports.add(port)

    def camera(item):
        node(item, '摄像头')
        kind, source = item.get('source_kind'), item.get('source')
        require(kind in {'simulation', 'camera', 'video', 'image', 'stream'}, '未知视频源类型')
        if kind == 'camera':
            require(isinstance(source, int) and not isinstance(source, bool) and source >= 0, 'USB 摄像头索引必须是非负整数')
            require(source not in cameras, f'同一 USB 摄像头索引 {source} 不可重复分配给不同视角')
            cameras.add(source)
        elif kind in {'video', 'image'}:
            require(isinstance(source, str) and bool(source.strip()), '素材路径不能为空')
            require(not check_files or resolve_path(source).is_file(), f'素材不存在：{source}')
        elif kind == 'stream':
            require(isinstance(source, str) and urlsplit(source).scheme in {'rtsp', 'http', 'https'}
                    and bool(urlsplit(source).hostname), '网络源必须为有效的 RTSP/HTTP/HTTPS 地址')
        require(isinstance(item.get('model'), str) and bool(item['model'].strip()), '模型路径不能为空')
        require(not check_files or resolve_path(item['model']).is_file(), f'模型不存在：{item.get("model")}')
        number(item.get('confidence'), 0.01, 1, '置信度')
        number(item.get('fps'), 0.2, 30, '单通道推理频率')
        for key in ('person_labels', 'heating_labels', 'fire_labels'):
            require(isinstance(item.get(key), list) and all(isinstance(x, str) and x.strip() for x in item[key]), f'{key} 必须是标签字符串数组')

    require(isinstance(cfg, dict) and cfg.get('schema_version') == 2, '配置 schema_version 必须为 2')
    node(cfg.get('building'), '实验楼')
    require(isinstance(cfg.get('rules'), dict), '缺少规则配置')
    for key, low, high in [('unattended_seconds', 0.5, 600), ('freshness_seconds', 1, 120),
                           ('high_temperature', -40, 150), ('high_humidity', 0, 100)]:
        number(cfg['rules'].get(key), low, high, key)
    require(isinstance(cfg.get('labs'), list) and bool(cfg['labs']), '至少需要一间实验室')
    for lab in cfg['labs']:
        node(lab, '实验室')
        camera(lab.get('macro'))
        require(isinstance(lab.get('benches'), list) and bool(lab['benches']), '每间实验室至少需要一个微观实验台')
        actuator = lab.get('actuator', {})
        require(actuator.get('mode') in {'simulation', 'serial'}, '执行器模式必须为 simulation 或 serial')
        number(actuator.get('baudrate'), 1200, 115200, '执行器波特率')
        if actuator['mode'] == 'serial':
            serial_port(actuator.get('port'))
        for bench in lab['benches']:
            node(bench, '实验台')
            camera(bench.get('camera'))
            telemetry = bench.get('telemetry', {})
            require(telemetry.get('kind') in {'simulation', 'serial', 'legacy_ipc'}, '未知温湿度源类型')
            number(telemetry.get('baudrate'), 1200, 115200, '传感器波特率')
            number(telemetry.get('temperature'), -40, 150, '模拟温度')
            number(telemetry.get('humidity'), 0, 100, '模拟湿度')
            if telemetry['kind'] == 'serial':
                serial_port(telemetry.get('port'))
    for lab in cfg['labs']:
        if lab['actuator']['mode'] == 'serial':
            require(lab['macro']['source_kind'] != 'simulation'
                    and all(b['camera']['source_kind'] != 'simulation' and b['telemetry']['kind'] == 'serial'
                            for b in lab['benches']), '真实执行器实验室不能混入模拟视觉或模拟温湿度')
    linkage = cfg.get('fire_linkage', {})
    targets, actions = linkage.get('targets'), linkage.get('actions')
    lab_ids = {lab['id'] for lab in cfg['labs']}
    require(targets == 'all' or (isinstance(targets, list) and len(set(targets)) == len(targets)
                                and all(t in lab_ids for t in targets)), '联动目标必须是 all 或有效实验室 ID 数组')
    require(isinstance(actions, list) and len(set(actions)) == len(actions)
            and all(a in {'alarm', 'power_off'} for a in actions), '联动动作仅支持 alarm 和 power_off')
    return cfg


def load_config(path=DEFAULT_CONFIG):
    with open(path, encoding='utf-8') as handle:
        return validate_config(json.load(handle))


def save_config(cfg, path=DEFAULT_CONFIG):
    cfg = validate_config(cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
