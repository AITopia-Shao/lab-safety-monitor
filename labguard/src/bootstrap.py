"""
Environment Bootstrap Module
Automatically locates and injects local venv site-packages and project paths into sys.path.
"""
import sys
import os

_PY_TAG = f"python{sys.version_info.major}.{sys.version_info.minor}"


def _env_matches(env_path):
    """检查 Windows 环境目录中的 pythonXY.dll 是否与当前解释器版本一致"""
    dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    return os.path.exists(os.path.join(env_path, dll)) or os.path.exists(os.path.join(env_path, "Scripts", dll))


def bootstrap_env():
    """自动寻找工程路径与本地 venv 依赖，避免未激活虚拟环境时出现 ModuleNotFoundError"""
    current = os.path.abspath(__file__)
    # 从当前文件向上回溯寻找工程根目录 (包含 venv 或 requirements.txt 的目录)
    search_dir = os.path.dirname(current)
    root_dir = None
    for _ in range(5):
        if os.path.exists(os.path.join(search_dir, "venv")) or os.path.exists(os.path.join(search_dir, "requirements.txt")):
            root_dir = search_dir
            break
        parent = os.path.dirname(search_dir)
        if parent == search_dir:
            break
        search_dir = parent

    if not root_dir:
        root_dir = os.path.dirname(os.path.dirname(current))

    # 1. 将项目目录注入 sys.path
    sub_paths = [
        root_dir,
        os.path.join(root_dir, "src"),
        os.path.join(root_dir, "LabSafetyMonitor"),
        os.path.join(root_dir, "person", "model_code"),
    ]
    for p in sub_paths:
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    if os.environ.get("LAB_SAFETY_ISOLATED_ENV") == "1":
        return

    # 2. 扫描候选 venv / conda 环境，追加到 sys.path 末尾（不覆盖当前环境已有的包）
    user_home = os.path.expanduser("~")
    candidate_envs = [
        os.path.join(root_dir, ".venv"),
        os.path.join(root_dir, "venv"),
        os.path.join(root_dir, "conda_env"),
        os.path.join(root_dir, "LabSafetyMonitor", "venv"),
        # Conda 全局命名环境支持 (lab_safety)
        os.path.join(user_home, ".conda", "envs", "lab_safety"),
        os.path.join(user_home, "miniconda3", "envs", "lab_safety"),
        os.path.join(user_home, "anaconda3", "envs", "lab_safety"),
        os.path.join(user_home, "miniforge3", "envs", "lab_safety"),
    ]
    for env_path in candidate_envs:
        if not os.path.isdir(env_path):
            continue
        # Linux / Unix 布局: <env>/lib/pythonX.Y/site-packages
        # 只注入与当前解释器版本一致的环境，避免 numpy / torch 等二进制包 ABI 不兼容导致 DLL 加载失败
        if os.path.abspath(sys.prefix) == os.path.abspath(env_path):
            continue
        lib_path = os.path.join(env_path, "lib")
        sp = os.path.join(lib_path, _PY_TAG, "site-packages")
        if os.path.isdir(sp) and sp not in sys.path:
            sys.path.append(sp)
        # Windows 布局: <env>/Lib/site-packages，需要核对环境自带的 python 版本
        win_sp = os.path.join(env_path, "Lib", "site-packages")
        if os.path.isdir(win_sp) and win_sp not in sys.path and _env_matches(env_path):
            sys.path.append(win_sp)

# 在被导入时自动执行一次
bootstrap_env()
