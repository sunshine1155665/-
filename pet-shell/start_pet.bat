@echo off
rem 鲸鱼娘桌宠启动器：确保 9951 静态服务在跑，然后拉起 Electron 窗口
cd /d <PROJECT_ROOT>\pet-shell
start "pet-server" /min python <PROJECT_ROOT>\scripts\pet_shell_server.py --root <PROJECT_ROOT>\pet-shell --port 9951
timeout /t 2 /nobreak >nul
start "" "<PROJECT_ROOT>\pet-shell\node_modules\electron\dist\electron.exe" .
