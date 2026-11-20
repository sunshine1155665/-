@echo off
rem fatfish 媒体中继：sing(9945) + photo(9946)，供云端 tailscale serve 转发
rem tokens 在 data\run\media_tokens.env（本地文件，不进 git）
cd /d <PROJECT_ROOT>
if not exist data\run\media_tokens.env (
    echo [fatfish-relays] missing data\run\media_tokens.env >&2
    exit /b 2
)
for /f "usebackq tokens=1,* delims==" %%a in (`type data\run\media_tokens.env`) do set %%a=%%b
set PY=D:\develop\python\python.exe
start "fatfish-sing" /min "%PY%" scripts\sing_server.py --host 127.0.0.1 --port 9945 --config config\remote.toml
start "fatfish-photo" /min "%PY%" scripts\photo_server.py --host 127.0.0.1 --port 9946 --timeout 150 --emit-json --config config\remote.toml
exit /b 0
