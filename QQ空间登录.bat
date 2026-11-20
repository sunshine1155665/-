@echo off
cd /d "%~dp0"
call "scripts\start_qzone_login.bat"
exit /b %errorlevel%
