#requires -Version 5.1
<#
启动「云端能力」所需的本机服务：
  - start_all.py：Web / 控制面 / TTS（9880，模型在第一次 /tts 时才加载）
  - sing_server.py（9945，按需加载唱歌模型）
  - photo_server.py（9946，按需拉起 ComfyUI）
  - pet_gateway.py（9950，桌宠/本地鲸鱼娘的持久 WebSocket）
Tailscale Serve 已配置：9443→9880 / 9445→9945 / 9446→9946 / 9447→9950。
（9444→9881 的 bge 向量入口已随表情包检索下线，不再使用。）
云端的 TTS / 唱歌 / 照相请求会走这些 tailnet HTTPS 地址；桌宠控制走 9447 的持久 WSS。

本脚本只启动进程，不主动下载/加载大模型；第一次真正用到时才加载。
#>
[CmdletBinding()]
param([switch]$NoPause)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root "data\run"
$logDir = Join-Path $root "data\logs"
$statePath = Join-Path $runDir "home-stack.json"
New-Item -ItemType Directory -Force -Path $runDir, $logDir | Out-Null

function Write-Step([string]$text) {
    Write-Host ("[鲸鱼娘] " + $text) -ForegroundColor Cyan
}

function Test-Port([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(700)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function Wait-Port([int]$port, [int]$seconds, [string]$label) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    Write-Host ("[鲸鱼娘] 警告：{0} (端口 {1}) 在 {2}s 内未就绪" -f $label, $port, $seconds) -ForegroundColor Yellow
    return $false
}

function Get-Python {
    $candidates = @(
        "D:\develop\python\python.exe",
        (Join-Path $root ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "找不到 Python 3.11+"
}

function Test-ExistingStack {
    if (-not (Test-Path $statePath)) { return $null }
    try {
        $state = Get-Content $statePath -Raw | ConvertFrom-Json
        $alive = $false
        foreach ($pidValue in @($state.start_all_pid, $state.sing_pid, $state.photo_pid)) {
            if ($pidValue -and (Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue)) {
                $alive = $true
            }
        }
        if ($alive) { return $state }
    } catch { }
    return $null
}

$existing = Test-ExistingStack
if ($existing) {
    Write-Step "检测到本机服务已经在运行（start_all PID=$($existing.start_all_pid)）。"
    Write-Step "云端可用地址不变；如果只想重启，请先运行「关闭云端能力」。"
    if (-not $NoPause) { Read-Host "按回车关闭这个窗口" | Out-Null }
    exit 0
}

$python = Get-Python
$state = [ordered]@{
    started_at = (Get-Date).ToString("s")
    python = $python
    start_all_pid = $null
    sing_pid = $null
    photo_pid = $null
    pet_gateway_pid = $null
    pet_shell_server_pid = $null
    pet_shell_pid = $null
    ports = @(9880, 9882, 9945, 9946, 9950, 9951)
}

Write-Step "启动统一运行时（Web / 控制面 / TTS）…"
$startAllArgs = @(
    (Join-Path $root "scripts\start_all.py"),
    "--no-qq",
    "--no-browser",
    "--profile", "low"
)
$startAllLog = Join-Path $logDir "home-stack-start_all.log"
$startAllErr = Join-Path $logDir "home-stack-start_all.err.log"
$startAll = Start-Process -FilePath $python -ArgumentList $startAllArgs `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $startAllLog -RedirectStandardError $startAllErr
$state.start_all_pid = $startAll.Id
Write-Step ("start_all PID={0}，等待 9880/9882 就绪（TTS 模型不预加载）…" -f $startAll.Id)

$ttsUp = Wait-Port 9880 90 "TTS"
$ctlUp = Wait-Port 9882 30 "控制面"
if (-not $ttsUp) {
    Write-Host "[鲸鱼娘] TTS 未起来：检查 data\logs\home-stack-start_all.err.log" -ForegroundColor Yellow
}

# 媒体中继：唱歌/照相。服务器本身很轻，模型只在收到请求时加载。
$tokensPath = Join-Path $runDir "media_tokens.env"
if (-not (Test-Path $tokensPath)) { throw "缺少 $tokensPath（SING_SERVER_TOKEN / PHOTO_SERVER_TOKEN）" }
foreach ($line in Get-Content $tokensPath) {
    $line = $line.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }
    $parts = $line.Split("=", 2)
    if ($parts.Count -eq 2) {
        [Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
    }
}
$singLog = Join-Path $logDir "home-stack-sing.log"
$singErr = Join-Path $logDir "home-stack-sing.err.log"
$sing = Start-Process -FilePath $python `
    -ArgumentList @((Join-Path $root "scripts\sing_server.py"), "--host", "127.0.0.1", "--port", "9945", "--timeout", "900", "--config", (Join-Path $root "config\remote.toml")) `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $singLog -RedirectStandardError $singErr
$state.sing_pid = $sing.Id

# 桌宠/本地鲸鱼娘的持久 WebSocket 入口。云端 qqbot 反向连这条 WSS；
# 不再为每次桌宠/语音请求重新做 TLS 握手。TTS/唱歌/拍照仍走原有 HTTP 协议。
$petLog = Join-Path $logDir "home-stack-pet.log"
$petErr = Join-Path $logDir "home-stack-pet.err.log"
$pet = Start-Process -FilePath $python `
    -ArgumentList @((Join-Path $root "scripts\pet_gateway.py"), "--host", "127.0.0.1", "--port", "9950", "--token-env", "PET_GATEWAY_TOKEN") `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $petLog -RedirectStandardError $petErr
$state.pet_gateway_pid = $pet.Id

# 静态资源 + Electron 桌面壳。没有 Electron 构建时只启动静态页，用户可手动
# 用 Edge `--app` 打开；不阻塞其余服务。
$shellRoot = Join-Path $root "pet-shell"
$shellServerLog = Join-Path $logDir "home-stack-pet-shell-server.log"
$shellServerErr = Join-Path $logDir "home-stack-pet-shell-server.err.log"
$shellServer = Start-Process -FilePath $python `
    -ArgumentList @((Join-Path $root "scripts\pet_shell_server.py"), "--root", $shellRoot, "--host", "127.0.0.1", "--port", "9951") `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $shellServerLog -RedirectStandardError $shellServerErr
$state.pet_shell_server_pid = $shellServer.Id
$shellUp = Wait-Port 9951 20 "桌宠静态资源"

$electron = Join-Path $shellRoot "node_modules\electron\dist\electron.exe"
if ($shellUp -and (Test-Path $electron)) {
    $shellLog = Join-Path $logDir "home-stack-pet-shell.log"
    $shellErr = Join-Path $logDir "home-stack-pet-shell.err.log"
    $oldElectronRunAsNode = $env:ELECTRON_RUN_AS_NODE
    Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    $shell = Start-Process -FilePath $electron -ArgumentList @($shellRoot) `
        -WorkingDirectory $root -PassThru `
        -RedirectStandardOutput $shellLog -RedirectStandardError $shellErr
    if ($null -ne $oldElectronRunAsNode) {
        $env:ELECTRON_RUN_AS_NODE = $oldElectronRunAsNode
    }
    $state.pet_shell_pid = $shell.Id
} elseif (-not (Test-Path $electron)) {
    Write-Host "[鲸鱼娘] 未找到 Electron，先只开着静态页：http://127.0.0.1:9951/renderer/index.html" -ForegroundColor Yellow
}

$photoLog = Join-Path $logDir "home-stack-photo.log"
$photoErr = Join-Path $logDir "home-stack-photo.err.log"
$photo = Start-Process -FilePath $python `
    -ArgumentList @((Join-Path $root "scripts\photo_server.py"), "--host", "127.0.0.1", "--port", "9946", "--timeout", "150", "--emit-json", "--config", (Join-Path $root "config\remote.toml")) `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $photoLog -RedirectStandardError $photoErr
$state.photo_pid = $photo.Id

$singUp = Wait-Port 9945 30 "唱歌中继"
$photoUp = Wait-Port 9946 30 "照相中继"
$petUp = Wait-Port 9950 30 "桌宠 WS"
$state | ConvertTo-Json -Depth 4 | Set-Content -Path $statePath -Encoding UTF8

# 不再修改 tailscale serve；现有配置由运维保留。只做状态提示。
$serveStatus = ""
try { $serveStatus = (& tailscale serve status 2>$null | Out-String) } catch { }
$serveOk = $serveStatus -match ":9443" -and $serveStatus -match ":9445" -and $serveStatus -match ":9446"
if (-not ($serveStatus -match ":9447")) {
    try {
        & tailscale serve --bg --https=9447 http://127.0.0.1:9950 2>$null | Out-Null
        $serveStatus = (& tailscale serve status 2>$null | Out-String)
    } catch {
        Write-Host "[鲸鱼娘] 警告：添加 tailnet 9447→9950 失败，桌宠 WS 云端连不上。" -ForegroundColor Yellow
    }
}
$serveOk = $serveStatus -match ":9443" -and $serveStatus -match ":9445" -and $serveStatus -match ":9446"
$petServeOk = $serveStatus -match ":9447"
if (-not $serveOk) {
    Write-Host "[鲸鱼娘] 警告：tailnet serve 缺少 9443/9445/9446，云端会连不上。" -ForegroundColor Yellow
}
if (-not $petServeOk) {
    Write-Host "[鲸鱼娘] 警告：tailnet serve 缺少 9447，桌宠 WS 云端会连不上。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "================ 鲸鱼娘云端能力 ================" -ForegroundColor Green
Write-Host ("本机 TTS(9880):    {0}  （第一次说话时才加载模型）" -f $(if ($ttsUp) { "已监听" } else { "未监听" }))
Write-Host ("本机 控制面(9882): {0}" -f $(if ($ctlUp) { "已监听" } else { "未监听" }))
Write-Host ("唱歌中继(9945):    {0}" -f $(if ($singUp) { "已监听" } else { "未监听" }))
Write-Host ("照相中继(9946):    {0}" -f $(if ($photoUp) { "已监听" } else { "未监听" }))
Write-Host ("桌宠 WS(9950):     {0}  （云端持久连接，桌宠/本地鲸鱼娘用）" -f $(if ($petUp) { "已监听" } else { "未监听" }))
Write-Host ("桌宠壳(9951):      {0}  {1}" -f $(if ($shellUp) { "静态页已开" } else { "未开" }), $(if ($state.pet_shell_pid) { "Electron 已启动" } else { "Electron 未启动" }))
Write-Host ("Tailscale Serve:   {0}" -f $(if ($serveOk -and $petServeOk) { "9443/9444/9445/9446/9447 已配置" } else { "需要检查" }))
Write-Host "云端现在可以请求 TTS / 歌曲 / 照相；大模型全部按需加载。"
Write-Host "关闭请点桌面「关闭鲸鱼娘云端能力」。"
Write-Host "================================================" -ForegroundColor Green
Write-Host ("状态文件：{0}" -f $statePath)
Write-Host ("日志目录：{0}" -f $logDir)
if (-not $NoPause) { Read-Host "按回车关闭这个窗口" | Out-Null }
