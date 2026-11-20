@echo off
cd /d "%~dp0.."
start "fatfish" python -m fatfish run --poll 30
echo fatfish runtime started in a new window.
