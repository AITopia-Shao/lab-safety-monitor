@echo off
chcp 65001 >nul
echo =====================================================================
echo   基于双摄协同的实验室安全事前干预监测系统 - Windows 打包程序
echo =====================================================================
echo.

REM 检查 Python 环境
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请确保已安装 Python 并加入系统环境变量 PATH！
    pause
    exit /b 1
)

echo [*] 正在安装与检查打包依赖...
pip install pyinstaller pyinstaller-hooks-contrib -q

echo [*] 开始执行 PyInstaller 打包构建...
pyinstaller --clean -y LabSafetyMonitor.spec

if %errorlevel% neq 0 (
    echo [失败] 打包过程中出现错误，请检查控制台报错信息！
    pause
    exit /b 1
)

echo.
echo =====================================================================
echo [成功] Windows 可执行程序已生成至 dist\LabSafetyMonitor\ 目录下！
echo 运行程序：dist\LabSafetyMonitor\LabSafetyMonitor.exe
echo =====================================================================
pause
