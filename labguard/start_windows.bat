@echo off
chcp 65001 >nul
rem 双击启动：电脑大窗口 + 网页大屏（实际网址会打印在下面的窗口里）
cd /d "%~dp0"
set "PY=python"
rem 装了 conda 并建过 lab_safety 环境（conda env create -f environment.yml）就优先用它
for %%D in ("%USERPROFILE%\miniforge3" "%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%LOCALAPPDATA%\miniforge3" "%ProgramData%\miniforge3" "%ProgramData%\anaconda3" "%USERPROFILE%\.conda") do (
    if exist "%%~D\envs\lab_safety\python.exe" set "PY=%%~D\envs\lab_safety\python.exe"
)
if exist "%CONDA_PREFIX%\envs\lab_safety\python.exe" set "PY=%CONDA_PREFIX%\envs\lab_safety\python.exe"
if "%PY%"=="python" if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
echo 使用的 Python：%PY%
"%PY%" run_app.py %*
if %errorlevel% neq 0 (
    echo.
    echo 程序异常退出（代码 %errorlevel%）。错误详情在 runtime\crash.log
    echo 第一次运行请先装环境：conda env create -f environment.yml
    echo 不用 conda 的话：pip install -r requirements.txt
    pause
)
