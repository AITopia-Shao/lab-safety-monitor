#!/bin/bash
# 兼容 macOS 自带的 Bash 3.2，不依赖用户的 shell/Conda 激活状态。
set -Eeuo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$(dirname -- "$SOURCE_DIR")"
ENV_DIR="$SOURCE_DIR/.venv-macos"
RUNTIME_DIR="$SOURCE_DIR/.macos-runtime"
REQUIREMENTS="$SOURCE_DIR/macos/requirements.txt"
CHECK_ONLY=0
LEGACY=0
LOCK_HELD=0

for argument in "$@"; do
    case "$argument" in
        --check) CHECK_ONLY=1 ;;
        --legacy) LEGACY=1 ;;
        *) printf '不支持的参数：%s\n可用参数：--check（安装并自检）、--legacy（原单实验室界面）\n' "$argument"; exit 2 ;;
    esac
done

mkdir -p "$RUNTIME_DIR/logs"
# 使用普通管道记录日志，不依赖部分受限终端不允许访问的 /dev/fd。
if [[ -z "${_LAB_SAFETY_LOG_FILE:-}" ]]; then
    LOG_FILE="$RUNTIME_DIR/logs/$(date '+%Y%m%d-%H%M%S')-$$.log"
    set +e
    _LAB_SAFETY_LOG_FILE="$LOG_FILE" /bin/bash "$SOURCE_DIR/macos/launch.sh" "$@" 2>&1 | /usr/bin/tee -a "$LOG_FILE"
    status=${PIPESTATUS[0]}
    exit "$status"
fi
LOG_FILE="$_LAB_SAFETY_LOG_FILE"
unset _LAB_SAFETY_LOG_FILE

finish() {
    local status=$?
    trap - EXIT
    if [[ "$LOCK_HELD" == 1 ]]; then
        rm -f "$RUNTIME_DIR/setup.lock/pid"
        rmdir "$RUNTIME_DIR/setup.lock" 2>/dev/null || true
    fi
    if [[ "$status" != 0 ]]; then
        printf '\n启动未完成（错误码 %s）。\n日志：%s\n' "$status" "$LOG_FILE"
        printf '检查网络后可重新双击；详细处理方法见项目中的「docs/分层系统升级说明与操作教程.md」。\n'
        if [[ -t 0 && "${LAB_SAFETY_NO_PAUSE:-0}" != 1 ]]; then
            read -r -p '按回车关闭此窗口…' _unused || true
        fi
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() { printf '\n%s\n' "$*"; exit 1; }

printf '实验室安全监测系统 · macOS 启动器\n项目位置：%s\n日志：%s\n\n' "$PROJECT_DIR" "$LOG_FILE"
[[ "$(uname -s)" == Darwin ]] || fail '此启动文件仅用于 macOS。'
for required in run_app.py models/ppe_best.pt models/chem_best.pt macos/requirements.txt macos/check_runtime.py; do
    [[ -s "$SOURCE_DIR/$required" ]] || fail "项目文件缺失：$required。请保留完整项目文件夹。"
done
cd "$SOURCE_DIR"

# 不继承其他 Python/Qt 环境的搜索路径。已有 Linux 虚拟环境不适用于 Mac。
unset PYTHONHOME PYTHONPATH QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QT_QPA_FONTDIR
export PYTHONNOUSERSITE=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8
export LAB_SAFETY_ISOLATED_ENV=1
export YOLO_CONFIG_DIR="$RUNTIME_DIR/ultralytics"
export MPLCONFIGDIR="$RUNTIME_DIR/matplotlib"
export INFERENCE_DEVICE="${INFERENCE_DEVICE:-cpu}"
mkdir -p "$YOLO_CONFIG_DIR" "$MPLCONFIGDIR"

# 同一时刻只允许一个安装/检查流程，防止连续双击时交叉写入环境。
if ! mkdir "$RUNTIME_DIR/setup.lock" 2>/dev/null; then
    owner="$(cat "$RUNTIME_DIR/setup.lock/pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" 2>/dev/null; then
        rm -f "$RUNTIME_DIR/setup.lock/pid"
        rmdir "$RUNTIME_DIR/setup.lock" 2>/dev/null || true
        mkdir "$RUNTIME_DIR/setup.lock" 2>/dev/null || fail '另一个启动器正在准备环境，请等待它完成。'
    else
        fail '另一个启动器正在准备环境，请等待它完成。若上次被强制中断，可删除 labguard/.macos-runtime/setup.lock 后重试。'
    fi
fi
LOCK_HELD=1
printf '%s\n' "$$" > "$RUNTIME_DIR/setup.lock/pid"

valid_python() {
    "$1" -I -c 'import platform, sys; sys.exit(0 if sys.platform == "darwin" and (3, 10) <= sys.version_info[:2] < (3, 13) and platform.machine() == sys.argv[1] else 1)' "$(uname -m)" >/dev/null 2>&1
}

if [[ ! -x "$ENV_DIR/bin/python" ]] || ! valid_python "$ENV_DIR/bin/python"; then
    # 不删除用户文件；残缺环境保留为备份，项目搬家后也能重新生成。
    if [[ -e "$ENV_DIR" ]]; then
        mv "$ENV_DIR" "$ENV_DIR.backup-$(date '+%Y%m%d-%H%M%S')-$$"
    fi
    BASE_PYTHON=""
    if [[ -n "${LAB_SAFETY_PYTHON:-}" ]]; then
        valid_python "$LAB_SAFETY_PYTHON" || fail 'LAB_SAFETY_PYTHON 必须指向与本机架构一致的 Python 3.10 / 3.11 / 3.12。'
        BASE_PYTHON="$LAB_SAFETY_PYTHON"
    else
        for version in 3.11 3.12 3.10; do
            for candidate in \
                "$(command -v "python$version" 2>/dev/null || true)" \
                "$HOME/.local/bin/python$version" \
                "/opt/homebrew/bin/python$version" \
                "/opt/homebrew/opt/python@$version/bin/python$version" \
                "/usr/local/bin/python$version" \
                "/usr/local/opt/python@$version/bin/python$version" \
                "/Library/Frameworks/Python.framework/Versions/$version/bin/python$version"; do
                if [[ -x "$candidate" ]] && valid_python "$candidate"; then
                    BASE_PYTHON="$candidate"
                    break 2
                fi
            done
        done
        if [[ -z "$BASE_PYTHON" ]]; then
            for candidate in \
                "$(command -v python3 2>/dev/null || true)" \
                "/opt/anaconda3/bin/python3" "$HOME/anaconda3/bin/python3" \
                "$HOME/miniconda3/bin/python3" "$HOME/miniforge3/bin/python3"; do
                if [[ -x "$candidate" ]] && valid_python "$candidate"; then
                    BASE_PYTHON="$candidate"
                    break
                fi
            done
        fi
    fi
    [[ -n "$BASE_PYTHON" ]] || fail '未找到 Python 3.10 / 3.11 / 3.12。请从 https://www.python.org/downloads/macos/ 安装 Python 3.11 后，再双击启动。本启动器不会自动安装 Homebrew 或修改系统 Python。'
    printf '创建项目独立环境，使用：%s\n' "$BASE_PYTHON"
    "$BASE_PYTHON" -I -m venv "$ENV_DIR"
fi
PYTHON="$ENV_DIR/bin/python"
"$PYTHON" -I -c 'import platform,sys; print("Python:", sys.version.split()[0], "| 架构:", platform.machine())'
REQUIREMENTS_HASH="$(/usr/bin/shasum -a 256 "$REQUIREMENTS" | /usr/bin/awk '{print $1}')"
SAVED_HASH="$(cat "$ENV_DIR/.requirements-sha256" 2>/dev/null || true)"

if [[ "$REQUIREMENTS_HASH" != "$SAVED_HASH" ]] || ! "$PYTHON" -I "$SOURCE_DIR/macos/check_runtime.py" --imports-only; then
    printf '\n首次运行或依赖发生变化，开始安装。需要联网下载，耗时取决于网速。\n'
    "$PYTHON" -I -m ensurepip --upgrade
    "$PYTHON" -I -m pip install --upgrade pip --disable-pip-version-check --timeout 30 --retries 2
    "$PYTHON" -I -m pip install --only-binary=:all: --disable-pip-version-check --timeout 30 --retries 2 -r "$REQUIREMENTS"
    "$PYTHON" -I -m pip check
    "$PYTHON" -I "$SOURCE_DIR/macos/check_runtime.py"
    printf '%s\n' "$REQUIREMENTS_HASH" > "$ENV_DIR/.requirements-sha256"
    "$PYTHON" -I -m pip freeze > "$RUNTIME_DIR/已安装依赖.txt"
elif [[ "$CHECK_ONLY" == 1 ]]; then
    "$PYTHON" -I "$SOURCE_DIR/macos/check_runtime.py"
fi

rm -f "$RUNTIME_DIR/setup.lock/pid"
rmdir "$RUNTIME_DIR/setup.lock"
LOCK_HELD=0

if [[ "$CHECK_ONLY" == 1 ]]; then
    printf '\n自检通过：依赖、Qt 窗口与两个真实模型均可运行。\n'
    exit 0
fi
printf '\n正在打开窗口。默认进入分层母系统；各通道按拓扑配置运行。\n关闭程序窗口即可退出；运行期间请保留此终端窗口。\n'
if [[ "$LEGACY" == 1 ]]; then
    "$PYTHON" -I "$SOURCE_DIR/macos/start.py" run_app.py
else
    "$PYTHON" -I "$SOURCE_DIR/macos/start.py" run_app.py --hierarchy
fi
printf '\n程序已关闭。可以关闭此终端窗口。\n'
