@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem 免扫码：借本机 Edge/Chrome 的登录态续期 QQ 空间 cookie。
rem   - 平时 cookie 过期，跑这个（或 `python -m fatfish qzone refresh`）即可，
rem     浏览器在后台静默续期，不需要扫码。
rem   - 第一次 / 登录态彻底失效时，会开一个 Edge 窗口：在里面正常登录一次，
rem     登录态留在 data\run\qzone-browser，之后都是免扫码的。

python --version >nul 2>&1
if errorlevel 1 goto no_python

python -m fatfish qzone login --browser --timeout 600
if errorlevel 1 (
  echo.
  echo [fatfish] 免扫码续期没有成功，先看上面的 error / hint。
  echo [fatfish] 兜底：QQ空间登录.bat（扫码）或 python -m fatfish qzone login --cookie "..."
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
