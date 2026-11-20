@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0.."

set "NAPCAT_DIR=%CD%\vendor\napcat\onekey\NapCat.52230.Shell"
set "NAPCAT_APP=%NAPCAT_DIR%\versions\9.9.33-52230\resources\app\napcat"
set "NAPCAT_CONFIG=%NAPCAT_APP%\config"

if not exist "%NAPCAT_DIR%\NapCatWinBootMain.exe" (
  echo [fatfish] NapCat OneKey is not installed under "%NAPCAT_DIR%".
  echo [fatfish] Run first:
  echo     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_napcat.ps1
  echo.
  pause
  exit /b 1
)

if not exist "%NAPCAT_DIR%\QQ.exe" (
  echo [fatfish] The portable QQ.exe is missing from "%NAPCAT_DIR%".
  echo [fatfish] Re-run:
  echo     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_napcat.ps1 -Force
  echo.
  pause
  exit /b 1
)

if not exist "%NAPCAT_CONFIG%\onebot11.json" (
  echo [fatfish] OneBot config is missing: "%NAPCAT_CONFIG%\onebot11.json"
  echo [fatfish] Re-run:
  echo     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_napcat.ps1
  echo.
  pause
  exit /b 1
)

tasklist /FI "IMAGENAME eq QQ.exe" /NH 2>nul | "%SystemRoot%\System32\find.exe" /I "QQ.exe" >nul
if not errorlevel 1 (
  echo [fatfish] QQ.exe is already running. Close QQ completely, then run this script again.
  echo [fatfish] NapCat must start its own portable QQ process.
  echo.
  pause
  exit /b 1
)

echo [fatfish] QQ      : "%NAPCAT_DIR%\QQ.exe" (portable 9.9.33-52230)
echo [fatfish] NapCat  : "%NAPCAT_DIR%"
echo [fatfish] Config  : "%NAPCAT_CONFIG%"
echo [fatfish] OneBot  : ws://127.0.0.1:3001
echo [fatfish] Starting NapCat. Scan the QR code with your phone QQ when it appears.
echo [fatfish] QR image: "%NAPCAT_APP%\cache\qrcode.png"
echo.

cd /d "%NAPCAT_DIR%"

rem OneKey takes a positional QQ number for quick login, not "-q".
if /I "%~1"=="-q" (
  if "%~2"=="" (
    echo [fatfish] Missing QQ number after -q.
    echo.
    pause
    exit /b 1
  )
  "%NAPCAT_DIR%\NapCatWinBootMain.exe" "%~2"
) else (
  "%NAPCAT_DIR%\NapCatWinBootMain.exe" %*
)
set "NAPCAT_RC=%ERRORLEVEL%"

if not "%NAPCAT_RC%"=="0" (
  echo.
  echo [fatfish] NapCat launcher exited with code %NAPCAT_RC%.
  echo [fatfish] See "%NAPCAT_APP%\logs" for details.
  echo.
  pause
  exit /b %NAPCAT_RC%
)

exit /b 0
