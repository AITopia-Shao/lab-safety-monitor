#!/usr/bin/env bash
# 启动：默认电脑大窗口 + 网页大屏；开发板正式运行请用 deploy/orangepi/start.sh（小屏模式）
cd "$(dirname "$0")"
exec python3 run_app.py "$@"
