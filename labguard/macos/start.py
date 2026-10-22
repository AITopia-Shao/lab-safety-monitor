"""使用启动器选定的环境执行原入口，支持中文路径与任意启动目录。"""
import os
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parent.parent
entry = sys.argv[1]
arguments = sys.argv[2:]
if entry not in {"run_app.py"}:
    raise SystemExit("未知入口")
os.chdir(root)
sys.path.insert(0, str(root))
sys.argv = [str(root / entry), *arguments]
runpy.run_path(str(root / entry), run_name="__main__")
