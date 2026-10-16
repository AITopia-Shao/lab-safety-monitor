#!/usr/bin/env bash
# 启动：默认电脑大窗口 + 网页大屏；开发板正式运行请用 deploy/orangepi/start.sh（小屏模式）
cd "$(dirname "$0")"
PY=python3
for d in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/.conda"; do
  if [ -x "$d/envs/lab_safety/bin/python" ]; then PY="$d/envs/lab_safety/bin/python"; break; fi
done
if [ "$PY" = python3 ] && [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi
exec "$PY" run_app.py "$@"
