@echo off
cd /d "%~dp0"
call "scripts\start_bili_login.bat"
exit /b %errorlevel%
