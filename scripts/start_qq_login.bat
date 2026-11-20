@echo off
chcp 65001 >nul
cd /d "%~dp0.."
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

python --version >nul 2>&1
if errorlevel 1 goto no_python

python scripts\qq_login_ui.py
if errorlevel 1 (
  echo.
  echo [fatfish] QQ/NapCat login helper did not finish. Read the message above.
  pause
)
exit /b 0

:no_python
echo.
echo [fatfish] Python was not found on PATH.
echo [fatfish] Install Python 3.11 or newer from https://www.python.org/downloads/
echo [fatfish] and enable "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1
