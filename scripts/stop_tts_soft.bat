@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "PIDFILE=data\run\tts_soft.pid"
set "PID="
set "STOPPED="

rem Marker lets start_tts_soft.bat treat a forced kill as an intentional stop.
if not exist "data\run" mkdir "data\run" >nul 2>&1
echo stop> "data\run\tts_soft.stopped"

rem 1) Preferred: PID recorded by the server itself.
if exist "%PIDFILE%" (
  set /p PID=<"%PIDFILE%"
  echo(!PID!| findstr /R "^[0-9][0-9]*$" >nul
  if errorlevel 1 set "PID="
)

set "IS_PYTHON="
if defined PID (
  for /f "tokens=1" %%N in ('tasklist /FI "PID eq !PID!" /FO CSV /NH 2^>nul') do (
    echo %%N | findstr /I /C:"python.exe" >nul && set "IS_PYTHON=1"
  )
  if defined IS_PYTHON (
    taskkill /PID !PID! /T /F >nul 2>&1
    if not errorlevel 1 set "STOPPED=1"
  ) else (
    echo [tts-soft] stale PID file ^(PID !PID! is not python.exe^); ignoring.
  )
)

rem 2) Fallback: owner of TCP port 9880, but only if it is python.exe.
if not defined STOPPED (
  set "PORT_PID="
  for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":9880 .*LISTENING"') do set "PORT_PID=%%P"
  if defined PORT_PID (
    set "IS_PYTHON="
    for /f "tokens=1" %%N in ('tasklist /FI "PID eq !PORT_PID!" /FO CSV /NH 2^>nul') do (
      echo %%N | findstr /I /C:"python.exe" >nul && set "IS_PYTHON=1"
    )
    if defined IS_PYTHON (
      taskkill /PID !PORT_PID! /T /F >nul 2>&1
      if not errorlevel 1 set "STOPPED=1"
    ) else (
      echo [tts-soft] port 9880 is owned by PID !PORT_PID! ^(not python.exe^); not killing it.
    )
  )
)

rem 3) Last resort: the dedicated console title set by start_tts_soft.bat.
if not defined STOPPED (
  taskkill /FI "WINDOWTITLE eq fatfish-tts-soft*" /T /F >nul 2>&1
  if not errorlevel 1 set "STOPPED=1"
)

if defined STOPPED (
  echo [tts-soft] stopped.
) else (
  echo [tts-soft] not running.
)
del /q "%PIDFILE%" >nul 2>&1
exit /b 0
