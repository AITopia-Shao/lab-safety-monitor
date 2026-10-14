@echo off
chcp 65001 >nul
cd /d "%~dp0\02_源码与可执行程序"
call 一键启动_Windows.bat %*
