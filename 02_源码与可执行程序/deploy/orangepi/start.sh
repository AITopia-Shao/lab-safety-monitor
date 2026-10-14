#!/usr/bin/env bash
# 启动监测系统（开机自启也调用这个脚本）
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
mkdir -p runtime
export DISPLAY="${DISPLAY:-:0}"
# 路演 / 值守场景：关闭屏保与息屏
xset s off -dpms 2>/dev/null || true
# CPU 调度设为性能模式（需要 sudo 免密时才会生效，失败不影响运行）
echo performance | sudo -n tee /sys/devices/system/cpu/cpufreq/policy*/scaling_governor >/dev/null 2>&1 || true
source .venv/bin/activate
exec python run_app.py >> runtime/app.log 2>&1
