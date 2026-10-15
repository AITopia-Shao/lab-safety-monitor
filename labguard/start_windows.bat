@echo off
chcp 65001 >nul
rem 双击启动：电脑大窗口 + 网页大屏（浏览器打开 http://127.0.0.1:8080）
cd /d "%~dp0"
python run_app.py %*
if %errorlevel% neq 0 (
    echo.
    echo 启动失败。第一次运行请先安装依赖：pip install -r requirements.txt
    pause
)
