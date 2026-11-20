<#
.SYNOPSIS
  Install the self-contained NapCatQQ OneKey bundle into vendor\napcat and seed
  a OneBot v11 forward WebSocket config for ws://127.0.0.1:3001.

.DESCRIPTION
  The OneKey bundle deploys its own portable QQ 9.9.33-52230 together with
  NapCat. This avoids the "Cannot find module ...loadNapCat.js" failure seen
  when the manual NapCat.Shell loader is pointed at the QQ versions already
  installed on this machine (9.9.25-42941 / 9.9.36-53644). The user's normal
  QQ installation is never modified.

  Idempotent: an existing working install is reused, an existing user config
  is never overwritten, and re-running only fills in what is missing.

.PARAMETER Version
  NapCat OneKey release tag. Pinned to v4.18.41.

.PARAMETER Destination
  Install root. Defaults to <repo>\vendor\napcat.

.PARAMETER Token
  OneBot access token used when the config is first created. A random token is
  generated when omitted.

.PARAMETER Force
  Reinstall the portable QQ/NapCat tree even when it looks correct. Cached
  installer downloads are reused, and the existing NapCat config is preserved.
#>
[CmdletBinding()]
param(
    [string]$Version = "v4.18.41",
    [string]$Destination = "",
    [string]$Token = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue

# OneKey asset size published on the v4.18.41 GitHub release (bytes).
$PinnedSizes = @{ "v4.18.41" = 994134 }
$AssetName = "NapCat.Shell.Windows.OneKey.zip"

# Constants embedded in the v4.18.41 OneKey installer.
$PortableQQVersion = "9.9.33-52230"
$OneKeyInstallName = "NapCat.52230.Shell"

function Test-ZipArchive {
    param([string]$Path)
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        try {
            return ($archive.Entries.Count -gt 0)
        }
        finally {
            $archive.Dispose()
        }
    }
    catch {
        return $false
    }
}

function New-RandomToken {
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $repoRoot "vendor\napcat"
}
$Destination = [System.IO.Path]::GetFullPath($Destination)

$oneKeyDir = Join-Path $Destination "onekey"
$zipPath = Join-Path $Destination $AssetName
$installerPath = Join-Path $oneKeyDir "NapCatInstaller.exe"
$installDir = Join-Path $oneKeyDir $OneKeyInstallName
$appDir = Join-Path $installDir "versions\$PortableQQVersion\resources\app"
$napCatDir = Join-Path $appDir "napcat"
$configDir = Join-Path $napCatDir "config"
$configPath = Join-Path $configDir "onebot11.json"
$legacyConfigPath = Join-Path $Destination "NapCat.Shell\config\onebot11.json"
$templatePath = Join-Path $Destination "onebot11.template.json"

$downloadUrl = "https://github.com/NapNeko/NapCatQQ/releases/download/$Version/$AssetName"
$releaseUrl = "https://github.com/NapNeko/NapCatQQ/releases/tag/$Version"

Write-Host "[napcat] repo      : $repoRoot"
Write-Host "[napcat] install   : $installDir"
Write-Host "[napcat] version   : $Version (portable QQ $PortableQQVersion)"

if (-not (Test-Path -LiteralPath $Destination)) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
}
if (-not (Test-Path -LiteralPath $oneKeyDir)) {
    New-Item -ItemType Directory -Path $oneKeyDir -Force | Out-Null
}

$requiredInstallFiles = @(
    (Join-Path $installDir "QQ.exe"),
    (Join-Path $installDir "NapCatWinBootMain.exe"),
    (Join-Path $napCatDir "napcat.mjs"),
    (Join-Path $appDir "package.json")
)
$installed = $true
foreach ($path in $requiredInstallFiles) {
    if (-not (Test-Path -LiteralPath $path)) {
        $installed = $false
    }
}

if ($installed -and -not $Force) {
    Write-Host "[napcat] OneKey install already present; skipping download."
}
else {
    # Preserve config across a forced reinstall. A broken partial install is
    # removed so the installer can recreate a clean tree.
    $configBackup = $null
    if (Test-Path -LiteralPath $configDir) {
        $configBackup = Join-Path $Destination ("_napcat_config_backup_" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $configBackup -Force | Out-Null
        Copy-Item -Path (Join-Path $configDir "*") -Destination $configBackup -Recurse -Force
        Write-Host "[napcat] backed up existing config"
    }
    if (Test-Path -LiteralPath $installDir) {
        Write-Host "[napcat] removing old portable install: $installDir"
        Remove-Item -LiteralPath $installDir -Recurse -Force
    }

    # --- 1. download OneKey -------------------------------------------------
    $expectedSize = $null
    if ($PinnedSizes.ContainsKey($Version)) {
        $expectedSize = [int64]$PinnedSizes[$Version]
    }

    $zipOk = $false
    if (Test-Path -LiteralPath $zipPath) {
        $actualSize = (Get-Item -LiteralPath $zipPath).Length
        if (($null -eq $expectedSize -or $actualSize -eq $expectedSize) -and (Test-ZipArchive $zipPath)) {
            $zipOk = $true
        }
        else {
            Write-Warning "[napcat] existing OneKey zip is invalid (size=$actualSize); downloading again."
        }
    }
    if ($Force) {
        $zipOk = $false
    }

    if (-not $zipOk) {
        $partialPath = "$zipPath.part"
        if (Test-Path -LiteralPath $partialPath) {
            Remove-Item -LiteralPath $partialPath -Force
        }
        Write-Host "[napcat] downloading $downloadUrl"
        try {
            Invoke-WebRequest -Uri $downloadUrl -OutFile $partialPath -UseBasicParsing
        }
        catch {
            if (Test-Path -LiteralPath $partialPath) {
                Remove-Item -LiteralPath $partialPath -Force
            }
            Write-Host ""
            Write-Host "[napcat] ERROR: download failed." -ForegroundColor Red
            Write-Host "[napcat] Official URL : $releaseUrl"
            Write-Host "[napcat] Direct asset : $downloadUrl"
            Write-Host "[napcat] Download the zip manually and place it at: $zipPath"
            exit 1
        }

        if ($null -ne $expectedSize) {
            $actualSize = (Get-Item -LiteralPath $partialPath).Length
            if ($actualSize -ne $expectedSize) {
                Remove-Item -LiteralPath $partialPath -Force
                Write-Host ""
                Write-Host "[napcat] ERROR: size mismatch: got $actualSize bytes, expected $expectedSize." -ForegroundColor Red
                Write-Host "[napcat] Official URL : $releaseUrl"
                exit 1
            }
        }
        Move-Item -LiteralPath $partialPath -Destination $zipPath -Force
        Write-Host "[napcat] downloaded $([math]::Round((Get-Item -LiteralPath $zipPath).Length / 1MB, 1)) MB"
    }

    if (-not (Test-ZipArchive $zipPath)) {
        Write-Host ""
        Write-Host "[napcat] ERROR: the zip is corrupt: $zipPath" -ForegroundColor Red
        Write-Host "[napcat] Delete it and re-run this script, or download from: $releaseUrl"
        exit 1
    }

    # --- 2. extract and run the OneKey installer ---------------------------
    Write-Host "[napcat] extracting to $oneKeyDir"
    Expand-Archive -LiteralPath $zipPath -DestinationPath $oneKeyDir -Force
    if (-not (Test-Path -LiteralPath $installerPath)) {
        Write-Host "[napcat] ERROR: NapCatInstaller.exe is missing after extraction." -ForegroundColor Red
        exit 1
    }

    Write-Host "[napcat] running NapCatInstaller (first run downloads ~300 MB QQ and ~35 MB NapCat)"
    # "< NUL" makes the installer's final "Press any key" return immediately.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $env:ComSpec
    $psi.Arguments = "/d /c `"`"$installerPath`" < NUL`""
    $psi.WorkingDirectory = $oneKeyDir
    $psi.UseShellExecute = $false
    $installer = [System.Diagnostics.Process]::Start($psi)
    $installer.WaitForExit()
    if ($installer.ExitCode -ne 0) {
        Write-Host ""
        Write-Host "[napcat] ERROR: NapCatInstaller.exe exited with code $($installer.ExitCode)." -ForegroundColor Red
        Write-Host "[napcat] Re-run this script, or download the OneKey zip manually from: $releaseUrl"
        exit 1
    }

    if ($configBackup -and (Test-Path -LiteralPath $configBackup)) {
        if (-not (Test-Path -LiteralPath $configDir)) {
            New-Item -ItemType Directory -Path $configDir -Force | Out-Null
        }
        Copy-Item -Path (Join-Path $configBackup "*") -Destination $configDir -Recurse -Force
        Remove-Item -LiteralPath $configBackup -Recurse -Force
        Write-Host "[napcat] restored existing config"
    }
}

foreach ($path in $requiredInstallFiles) {
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host "[napcat] ERROR: missing $path after installation." -ForegroundColor Red
        Write-Host "[napcat] Delete $installDir and re-run this script." -ForegroundColor Red
        exit 1
    }
}

$appPackage = Get-Content -LiteralPath (Join-Path $appDir "package.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$appPackage.main -ne "./napcat/napcat.mjs") {
    Write-Warning "[napcat] unexpected package.json main entry: $($appPackage.main)"
}

# --- 3. seed OneBot v11 config --------------------------------------------
$oneBotToken = ""
$oneBotHost = "127.0.0.1"
$oneBotPort = "3001"
if (-not (Test-Path -LiteralPath $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}

if (Test-Path -LiteralPath $configPath) {
    Write-Host "[napcat] keeping existing $configPath"
}
elseif (Test-Path -LiteralPath $legacyConfigPath) {
    Copy-Item -LiteralPath $legacyConfigPath -Destination $configPath -Force
    Write-Host "[napcat] migrated config from $legacyConfigPath"
}
else {
    if (-not (Test-Path -LiteralPath $templatePath)) {
        Write-Host "[napcat] ERROR: template not found: $templatePath" -ForegroundColor Red
        exit 1
    }
    if ([string]::IsNullOrWhiteSpace($Token)) {
        $Token = New-RandomToken
    }
    $json = Get-Content -LiteralPath $templatePath -Raw -Encoding UTF8
    $json = [regex]::Replace($json, "__REPLACE_WITH_YOUR_TOKEN__", $Token, "IgnoreCase")
    if ($json -match "__REPLACE_WITH_YOUR_TOKEN__") {
        Write-Host "[napcat] ERROR: could not replace the token placeholder in the template." -ForegroundColor Red
        exit 1
    }
    # UTF-8 without BOM: JSON.parse in NapCat rejects a leading BOM.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($configPath, $json, $utf8NoBom)
    Write-Host "[napcat] created $configPath"
}

try {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.network.websocketServers -and $config.network.websocketServers.Count -gt 0) {
        $oneBotToken = [string]$config.network.websocketServers[0].token
        if ($config.network.websocketServers[0].host) {
            $oneBotHost = [string]$config.network.websocketServers[0].host
        }
        if ($config.network.websocketServers[0].port) {
            $oneBotPort = [string]$config.network.websocketServers[0].port
        }
    }
    else {
        Write-Warning "[napcat] $configPath has no websocketServers entry; check it manually."
    }
}
catch {
    Write-Warning "[napcat] could not parse $configPath; check it manually."
}
if ([string]::IsNullOrWhiteSpace($oneBotToken)) {
    Write-Warning "[napcat] OneBot token is empty; set one before exposing the port."
}

# --- 4. report -------------------------------------------------------------
$qrPath = Join-Path $napCatDir "cache\qrcode.png"
$qqExe = Join-Path $installDir "QQ.exe"
$launcher = Join-Path $installDir "NapCatWinBootMain.exe"

Write-Host ""
Write-Host "[napcat] install complete"
Write-Host "  portable QQ : $qqExe ($PortableQQVersion)"
Write-Host "  launcher    : $launcher"
Write-Host "  onebot cfg  : $configPath"
Write-Host "  onebot ws   : ws://$oneBotHost`:$oneBotPort"
Write-Host "  webui       : http://127.0.0.1:6099 (token on console / config\webui.json)"
Write-Host "  qrcode      : $qrPath"
if ($oneBotToken) {
    Write-Host "  token       : $oneBotToken"
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) Make the token available to fatfish (current terminal):"
if ($oneBotToken) {
    Write-Host "       set QQ_ACCESS_TOKEN=$oneBotToken"
}
else {
    Write-Host "       set QQ_ACCESS_TOKEN=<token from $configPath>"
}
Write-Host "     Or add this line to $repoRoot\.env :"
Write-Host "       QQ_ACCESS_TOKEN=$oneBotToken"
Write-Host "  2) Fully exit QQ, then start NapCat and scan the QR code with your phone:"
Write-Host "       scripts\start_napcat.bat"
Write-Host "  3) After login, verify the listener and the adapter:"
Write-Host "       netstat -ano | findstr :3001"
Write-Host "       python -m fatfish qq status"
Write-Host ""
Write-Host "The user's installed QQ under D:\Program Files\Tencent\QQNT is NOT modified."
Write-Host "NapCat uses its own portable QQ $PortableQQVersion and is NOT logged in; the QR scan is done by you."
exit 0
