@echo off
cd /d "%~dp0"
call "scripts\start_qq_login.bat"
exit /b %errorlevel%
