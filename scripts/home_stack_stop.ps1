#requires -Version 5.1
<#
关闭「云端能力」：停本机端口 + 杀本机相关进程。
  - 读取 data\run\home-stack.json 记录的 PID
  - 杀掉 sing/photo 中继、start_all 进程树（Web / TTS / 控制面）
  - 兜底按端口清理：9880/9882/9945/9946/8188
  - 只处理 fatfish-lab / ComfyUI 相关进程，不碰 tailscale 本身
#>
[CmdletBinding()]
param([switch]$NoPause)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root "data\run"
$statePath = Join-Path $runDir "home-stack.json"

function Write-Step([string]$text) {
    Write-Host ("[鲸鱼娘] " + $text) -ForegroundColor Cyan
}

function Stop-Tree([int]$processId, [string]$label) {
    if (-not $processId) { return }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Step ("{0} PID={1} 已不在" -f $label, $processId)
        return
    }
    Write-Step ("停止 {0} PID={1}" -f $label, $processId)
    & taskkill.exe /PID $processId /T /F 2>&1 | Out-Null
}

function Test-Port([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function Get-ListenerPid([int]$port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop | Select-Object -First 1
        return [int]$conn.OwningProcess
    } catch { return 0 }
}

function Get-CommandLine([int]$processId) {
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction Stop
        return [string]$p.CommandLine
    } catch { return "" }
}

if (Test-Path $statePath) {
    try {
        $state = Get-Content $statePath -Raw | ConvertFrom-Json
        Write-Step ("读取状态文件，启动于 {0}" -f $state.started_at)
        Stop-Tree ([int]$state.sing_pid) "唱歌中继"
        Stop-Tree ([int]$state.photo_pid) "照相中继"
        Stop-Tree ([int]$state.pet_shell_pid) "桌宠壳"
        Stop-Tree ([int]$state.pet_shell_server_pid) "桌宠静态资源"
        Stop-Tree ([int]$state.pet_gateway_pid) "桌宠 WS"
        Stop-Tree ([int]$state.start_all_pid) "统一运行时（含 Web/TTS/控制面）"
    } catch {
        Write-Step ("状态文件解析失败：{0}" -f $_.Exception.Message)
    }
} else {
    Write-Step "没有找到状态文件，按端口兜底清理。"
}

Start-Sleep -Milliseconds 800

# 兜底：端口占用者里只杀 fatfish-lab / ComfyUI 相关进程，避免误杀别的服务。
$targets = @(
    @{ Port = 9880; Name = "TTS" },
    @{ Port = 9882; Name = "控制面" },
    @{ Port = 9945; Name = "唱歌中继" },
    @{ Port = 9946; Name = "照相中继" },
    @{ Port = 9950; Name = "桌宠 WS" },
    @{ Port = 9951; Name = "桌宠静态资源" },
    @{ Port = 8188; Name = "ComfyUI" }
)
foreach ($target in $targets) {
    if (-not (Test-Port $target.Port)) { continue }
    $ownerPid = Get-ListenerPid $target.Port
    if (-not $ownerPid) { continue }
    $cmdline = Get-CommandLine $ownerPid
    if ($cmdline -match "fatfish-lab" -or $cmdline -match "ComfyUI" -or $cmdline -match "pet_gateway\.py" -or $cmdline -match "pet_shell_server\.py") {
        Write-Step ("端口 {0} 仍被 {1} PID={2} 占用，结束进程" -f $target.Port, $target.Name, $ownerPid)
        & taskkill.exe /PID $ownerPid /T /F 2>&1 | Out-Null
    } else {
        Write-Step ("端口 {0} 被非 fatfish 进程 PID={1} 占用，跳过（{2}）" -f $target.Port, $ownerPid, $cmdline)
    }
}

Remove-Item $statePath -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800

# 兜底杀孤儿：状态文件丢失、或 start_all 被 OOM/异常杀掉时，TTS/唱歌子进程
# 可能已经脱离父进程。这里只匹配本仓/模型 runner 的命令行，不动其它 Python。
$orphanPatterns = @(
    "fatfish-lab\\scripts\\indextts_server\.py",
    "fatfish-lab\\scripts\\embedding_server\.py",
    "fatfish-lab\\scripts\\sing_server\.py",
    "fatfish-lab\\scripts\\photo_server\.py",
    "fatfish-lab\\scripts\\pet_gateway\.py",
    "pet_gateway\.py",
    "pet_shell_server\.py",
    "fatfish-lab\\scripts\\start_all\.py",
    "fatfish-lab\\soulx\\song_yue2_rvc\.py",
    "ACE-Step.*YuE2",
    "YuE2.*yue2\.exe"
)
foreach ($proc in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
    $cmd = [string]$proc.CommandLine
    if (-not $cmd) { continue }
    foreach ($pattern in $orphanPatterns) {
        if ($cmd -match $pattern) {
            Write-Step ("清理残留 {0} PID={1}" -f $pattern, $proc.ProcessId)
            & taskkill.exe /PID $proc.ProcessId /T /F 2>&1 | Out-Null
            break
        }
    }
}
Start-Sleep -Milliseconds 800

$ports = @(9880, 9882, 9945, 9946, 9950, 9951, 8188)
$still = @()
foreach ($port in $ports) {
    if (Test-Port $port) {
        $still += "$port (PID $(Get-ListenerPid $port))"
    }
}

Write-Host ""
Write-Host "================ 鲸鱼娘云端能力已关闭 ================" -ForegroundColor Green
if ($still.Count -eq 0) {
    Write-Host "本机端口 9880/9882/9945/9946/9950/9951/8188 已全部释放。"
    Write-Host "云端的 TTS / 唱歌 / 照相请求现在会降级为文字或友好提示。"
} else {
    Write-Host "以下端口仍被占用：" -ForegroundColor Yellow
    foreach ($item in $still) { Write-Host ("  - " + $item) }
    Write-Host "可能不是本脚本启动的进程，未强行杀掉。" -ForegroundColor Yellow
}
Write-Host "=====================================================" -ForegroundColor Green
if (-not $NoPause) { Read-Host "按回车关闭这个窗口" | Out-Null }
