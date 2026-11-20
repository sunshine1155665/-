@echo off
setlocal
chcp 65001 >nul
cd /d "<PROJECT_ROOT>"

rem 8910 已有服务在监听就不重复起（重复点快捷方式是安全的）
netstat -ano | findstr ":8910" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  start "fatfish-stats" /min cmd /c "python scripts\serve_stats.py --host 127.0.0.1 --port 8910"
  "%SystemRoot%\System32\timeout.exe" /t 3 /nobreak >nul
)

tailscale serve --bg --https=8447 http://127.0.0.1:8910
if errorlevel 1 (
  echo.
  echo [!] tailscale serve 启动失败：请确认 Tailscale 已登录并在运行。
  pause
  exit /b 1
)

echo.
echo ==================================================
echo   远程监控已就绪，手机浏览器打开：
echo   https://<desktop-host>.example-tailnet.ts.net:8447
echo   （本机预览页面即将自动打开）
echo ==================================================
start "" http://127.0.0.1:8910/
"%SystemRoot%\System32\timeout.exe" /t 6 /nobreak >nul
endlocal
