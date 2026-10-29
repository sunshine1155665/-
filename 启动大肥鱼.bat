@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

python --version >nul 2>&1
if errorlevel 1 goto no_python

python scripts\start_all.py %*
if errorlevel 1 (
  echo.
  echo [大肥鱼] 启动失败，请把上面的信息发给维护者。
  pause
  exit /b 1
)
exit /b 0

:no_python
echo.
echo [大肥鱼] 没有找到 Python。
echo 请安装 Python 3.11 或更新版本：https://www.python.org/downloads/
echo 安装时勾选 "Add python.exe to PATH"，然后重新双击本文件。
echo.
pause
exit /b 1
