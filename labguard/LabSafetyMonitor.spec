# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
project_root = os.path.abspath(os.getcwd())

# 收集 ultralytics 的全部模块与权重元数据
ultralytics_datas, ultralytics_binaries, ultralytics_hiddenimports = collect_all('ultralytics')

datas = [
    ('models/chem_best.pt', 'models'),
    ('models/ppe_best.pt', 'models'),
    ('config/hierarchy.json', 'config'),
    ('LabSafetyMonitor/Zpix.ttf', 'LabSafetyMonitor'),
    ('LabSafetyMonitor/NerdFontSymbols.ttf', 'LabSafetyMonitor'),
    ('config.yaml', '.'),
    ('knowledge', 'knowledge'),
    ('src/web/static', 'src/web/static'),
] + ultralytics_datas

hiddenimports = [
    'serial',
    'serial.tools',
    'serial.tools.list_ports',
    'PyQt6',
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'PyQt6.sip',
    'torch',
    'torchvision',
    'cv2',
    'numpy',
    'src',
    'src.ui.main_window',
    'src.hardware.sensor_worker',
    'src.core.state_machine',
    'src.core.events',
    'src.core.telemetry',
    'src.config',
    'src.core.engine',
    'src.core.world_model',
    'src.core.env_control',
    'src.core.demo',
    'src.core.jepa_link',
    'src.agent.agent',
    'src.agent.llm_client',
    'src.agent.rag',
    'src.web.server',
    'src.ui.kiosk',
    'fastapi',
    'uvicorn',
    'yaml',
    'sqlite3',
    'dual_inference',
    'serial_comm',
    'camera_manager',
] + ultralytics_hiddenimports

binaries = [] + ultralytics_binaries

a = Analysis(
    ['run_app.py'],
    pathex=[project_root, os.path.join(project_root, 'LabSafetyMonitor'), os.path.join(project_root, 'src')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['triton', 'tkinter'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LabSafetyMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 演示时保留控制台输出方便查看推理日志与串口信息
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='LabSafetyMonitor',
)
