@echo off
setlocal
cd /d "%~dp0.."
title fatfish-tts-soft

set "PY=.venv-cosyvoice\Scripts\python.exe"
set "MODEL=models\Fun-CosyVoice3-0.5B"
set "REF=data\whale_voice_demo_v2\refs\ref_base_x2.wav"
set "STOPMARK=data\run\tts_soft.stopped"
del /q "%STOPMARK%" >nul 2>&1

if not exist "%PY%" (
  echo [tts-soft] missing Python venv: %PY%
  echo [tts-soft] create it first with the CosyVoice environment.
  pause
  exit /b 1
)
if not exist "%MODEL%\llm.pt" (
  echo [tts-soft] missing model: %MODEL%
  pause
  exit /b 1
)
if not exist "%REF%" (
  echo [tts-soft] missing reference: %REF%
  pause
  exit /b 1
)

rem Keep Hugging Face / transformers fully offline.
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"

rem Dynamic VRAM defaults: load on the first /tts, release after 5 minutes idle.
rem Extra arguments are appended after these defaults so they win (for example
rem --idle-unload-seconds 60 or --no-lazy-load --no-warmup).
"%PY%" "scripts\cosyvoice_soft_server.py" --lazy-load --idle-unload-seconds 300 %*
set "CODE=%ERRORLEVEL%"
if exist "%STOPMARK%" (
  rem Intentional shutdown from stop_tts_soft.bat; not an error.
  del /q "%STOPMARK%" >nul 2>&1
  exit /b 0
)
if not "%CODE%"=="0" (
  echo.
  echo [tts-soft] server exited with code %CODE%.
  pause
)
exit /b %CODE%
