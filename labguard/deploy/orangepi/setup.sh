#!/usr/bin/env bash
# 香橙派 5（RK3588S）一键部署脚本
# 适用：Orange Pi 官方 Ubuntu 22.04 Desktop 镜像（Python 3.10）
# 用法：bash deploy/orangepi/setup.sh
#   默认用 conda（Miniforge）建 Python 环境，和电脑上的 environment.yml 是同一份；
#   想用系统 Python + venv 的老办法：LABGUARD_USE_VENV=1 bash deploy/orangepi/setup.sh
set -e
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
echo "==> 项目目录: $APP_DIR"

echo "==> 1/6 安装系统依赖"
sudo apt update
sudo apt install -y libgl1 libglib2.0-0 libegl1 libxkbcommon-x11-0 libxcb-cursor0 \
    v4l-utils fonts-wqy-microhei x11-xserver-utils wget
if [ -n "$LABGUARD_USE_VENV" ]; then
  sudo apt install -y python3-venv python3-pip python3-dev python3-pyqt6
fi

echo "==> 2/6 串口与摄像头权限（重新登录后生效）"
sudo usermod -aG dialout,video "$USER"
sudo cp deploy/orangepi/99-labguard.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

if [ -n "$LABGUARD_USE_VENV" ]; then
  echo "==> 3/6 创建虚拟环境（复用系统的 PyQt6，避免在 ARM 上编译 Qt）"
  python3 -m venv --system-site-packages .venv
  source .venv/bin/activate
  pip install -U pip
  grep -v -i "^PySide6" requirements.txt > /tmp/labguard_req.txt
  pip install -r /tmp/labguard_req.txt
else
  echo "==> 3/6 用 conda 创建 Python 环境 lab_safety（第一次要下载约 1–2 GB，耐心等）"
  CONDA_DIR="$HOME/miniforge3"
  if ! command -v conda >/dev/null 2>&1 && [ ! -x "$CONDA_DIR/bin/conda" ]; then
    INSTALLER=/tmp/Miniforge3-Linux-aarch64.sh
    wget -q -O "$INSTALLER" https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh \
      || wget -q -O "$INSTALLER" https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-Linux-aarch64.sh
    bash "$INSTALLER" -b -p "$CONDA_DIR"
  fi
  if [ -x "$CONDA_DIR/bin/conda" ]; then CONDA="$CONDA_DIR/bin/conda"; else CONDA="$(command -v conda)"; fi
  if "$CONDA" env list | grep -q "^lab_safety "; then
    "$CONDA" env update -n lab_safety -f environment.yml --prune
  else
    "$CONDA" env create -f environment.yml
  fi
  eval "$("$CONDA" shell.bash hook)"
  conda activate lab_safety
fi
echo "==> 4/6 NPU 推理运行库（RKNN）"
pip install rknn-toolkit-lite2 || echo "!! rknn-toolkit-lite2 安装失败：请从 github.com/airockchip/rknn-toolkit2 的 rknn-toolkit-lite2/packages 下载对应 Python 版本的 whl 手动安装"
if [ ! -f /usr/lib/librknnrt.so ]; then
  echo "   未找到 /usr/lib/librknnrt.so，尝试下载..."
  sudo wget -q -O /usr/lib/librknnrt.so \
    https://github.com/airockchip/rknn-toolkit2/raw/master/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so \
    || echo "!! 下载失败：请从 rknn-toolkit2 仓库 rknpu2/runtime/Linux/librknn_api/aarch64/ 手动拷贝 librknnrt.so 到 /usr/lib/"
fi
if ls models/*_rknn_model >/dev/null 2>&1; then
  echo "   已找到 RKNN 模型：$(ls -d models/*_rknn_model | tr '\n' ' ')"
else
  echo "!! models/ 下没有 RKNN 模型，将使用 .pt 在 CPU 上推理（较慢）。请在 x86 Linux / WSL2 上运行 tools/export_rknn.py 导出后拷过来"
fi

echo "==> 5/6 开机自启"
mkdir -p ~/.config/autostart runtime
sed "s#__APP_DIR__#$APP_DIR#g" deploy/orangepi/labguard.desktop > ~/.config/autostart/labguard.desktop
chmod +x deploy/orangepi/start.sh

echo "==> 6/6 摄像头列表（把 by-id 路径填进 config.yaml 的 camera.pano / camera.desk，插拔后编号不会变）"
ls -l /dev/v4l/by-id/ 2>/dev/null || v4l2-ctl --list-devices
echo
echo "完成。重新登录（或重启）后生效；手动启动：bash deploy/orangepi/start.sh"
