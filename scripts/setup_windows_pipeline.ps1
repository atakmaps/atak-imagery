# ATAK Pipeline — full Windows setup for a fresh machine.
# Installs dependencies, builds both EXEs, installs to Programs folder + desktop icons.
# Does not launch any program — user runs Device Installer or Imagery Downloader when ready.
# Run from repo root:  install_windows.cmd
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $Root "VERSION"))) {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not (Test-Path (Join-Path $Root "VERSION"))) {
    throw "Run from the ATAK pipeline repo root (folder containing VERSION)."
}

$WinBuild = Join-Path $Root "windows_build"
$ToolsDir = Join-Path $Root "tools"
$PlatformToolsDir = Join-Path $ToolsDir "platform-tools"
$DistDir = Join-Path $Root "dist"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\ATAK Pipeline"
$LogFile = Join-Path $Root "setup_windows.log"
$DesktopDir = [Environment]::GetFolderPath("Desktop")
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ATAK Pipeline"
$PlatformToolsUrl = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
$PlatformToolsZip = Join-Path $ToolsDir "platform-tools-latest-windows.zip"

function Write-Log([string]$Msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Refresh-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Ensure-UserPathContains([string]$Dir) {
    if (-not (Test-Path $Dir)) { return }
    $normalized = (Resolve-Path $Dir).Path
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($userPath) { $parts = $userPath -split ';' | Where-Object { $_ -and ($_.Trim() -ne "") } }
    foreach ($p in $parts) {
        if ($p -ieq $normalized) { return }
    }
    $newPath = if ($parts.Count -gt 0) { "$normalized;" + ($parts -join ';') } else { $normalized }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    if ($env:Path -notlike "*$normalized*") { $env:Path = "$normalized;" + $env:Path }
    Write-Log "Added to user PATH: $normalized"
}

function Resolve-PythonExe {
    Refresh-SessionPath
    $tryList = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $pyFromLauncher = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($pyFromLauncher -and (Test-Path $pyFromLauncher)) { $tryList += $pyFromLauncher.Trim() }
        } catch { }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $tryList += $cmd.Source }
    }
    $localApp = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $localApp) {
        Get-ChildItem $localApp -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            ForEach-Object { $tryList += $_.FullName }
    }
    $seen = @{}
    foreach ($exe in $tryList) {
        if (-not $exe -or $seen[$exe]) { continue }
        $seen[$exe] = $true
        try {
            & $exe -c "import ssl, venv, tkinter" 2>$null
            if ($LASTEXITCODE -eq 0) { return $exe }
        } catch { }
    }
    return $null
}

function Install-Python {
    Write-Log "Installing Python 3.12 via winget..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python not found and winget is unavailable. Install Python 3 from https://www.python.org/downloads/ (check Add to PATH), then rerun install_windows.cmd"
    }
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --disable-interactivity
    Refresh-SessionPath
    Start-Sleep -Seconds 3
}

function Find-AdbPath {
    $cmd = Get-Command adb -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path $cmd.Source)) { return $cmd.Source }
    foreach ($p in @(
        (Join-Path $InstallDir "tools\platform-tools\adb.exe"),
        (Join-Path $PlatformToolsDir "adb.exe"),
        (Join-Path $DistDir "tools\platform-tools\adb.exe"),
        (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe")
    )) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

function Install-PlatformTools {
    if (Test-Path (Join-Path $PlatformToolsDir "adb.exe")) {
        Write-Log "platform-tools already at $PlatformToolsDir"
        return (Join-Path $PlatformToolsDir "adb.exe")
    }
    Write-Log "Downloading Android platform-tools..."
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    if (Test-Path $PlatformToolsZip) { Remove-Item $PlatformToolsZip -Force }
    Invoke-WebRequest -Uri $PlatformToolsUrl -OutFile $PlatformToolsZip -UseBasicParsing
    $extractRoot = Join-Path $ToolsDir "_extract"
    if (Test-Path $extractRoot) { Remove-Item $extractRoot -Recurse -Force }
    Expand-Archive -Path $PlatformToolsZip -DestinationPath $extractRoot -Force
    $inner = Join-Path $extractRoot "platform-tools"
    if (-not (Test-Path $inner)) { throw "platform-tools zip invalid." }
    if (Test-Path $PlatformToolsDir) { Remove-Item $PlatformToolsDir -Recurse -Force }
    Move-Item $inner $PlatformToolsDir
    Remove-Item $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $PlatformToolsZip -Force -ErrorAction SilentlyContinue
    $adb = Join-Path $PlatformToolsDir "adb.exe"
    Write-Log "Installed platform-tools: $adb"
    return $adb
}

function Install-BuiltPrograms {
    Write-Log "Installing programs to: $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

    foreach ($exe in @("ATAKDeviceInstaller.exe", "ATAKImageryDownloader.exe")) {
        $src = Join-Path $DistDir $exe
        if (-not (Test-Path $src)) { throw "Missing build output: $src" }
        Copy-Item $src (Join-Path $InstallDir $exe) -Force
    }

    $installTools = Join-Path $InstallDir "tools\platform-tools"
    New-Item -ItemType Directory -Force -Path $installTools | Out-Null
    if (Test-Path (Join-Path $DistDir "tools\platform-tools")) {
        Copy-Item (Join-Path $DistDir "tools\platform-tools\*") $installTools -Recurse -Force
    } elseif (Test-Path $PlatformToolsDir) {
        Copy-Item (Join-Path $PlatformToolsDir "*") $installTools -Recurse -Force
    }

    $deployExample = Join-Path $Root "deploy.env.example"
    if (-not (Test-Path $deployExample)) { $deployExample = Join-Path $WinBuild "deploy.env.example" }
    $deployInstall = Join-Path $InstallDir "deploy.env"
    if (-not (Test-Path $deployInstall) -and (Test-Path $deployExample)) {
        Copy-Item $deployExample $deployInstall
    }
    if (Test-Path $deployExample) {
        Copy-Item $deployExample (Join-Path $InstallDir "deploy.env.example") -Force
    }

    $versionFile = Join-Path $Root "VERSION"
    if (Test-Path $versionFile) {
        Copy-Item $versionFile (Join-Path $InstallDir "VERSION") -Force
    }
}

function New-ProgramShortcut {
    param(
        [string]$LinkPath,
        [string]$TargetExe,
        [string]$WorkingDir,
        [string]$Description
    )
    $parent = Split-Path -Parent $LinkPath
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    if (Test-Path $LinkPath) { Remove-Item $LinkPath -Force }
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($LinkPath)
    $sc.TargetPath = $TargetExe
    $sc.WorkingDirectory = $WorkingDir
    $sc.Description = $Description
    $sc.Save()
    Write-Log "Shortcut: $LinkPath"
}

function Stop-AtakAppProcesses {
    foreach ($name in @("ATAKImageryDownloader", "ATAKDeviceInstaller", "ATAKPipeline")) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

function Remove-LegacyLaunchers {
    foreach ($path in @(
        (Join-Path $Root "run_atak_pipeline.cmd"),
        (Join-Path $Root "run_atak_device_installer.cmd"),
        (Join-Path $Root "Run_ATAK_Imagery_Downloader.cmd"),
        (Join-Path $Root "Run_ATAK_Device_Installer.cmd"),
        (Join-Path $Root "Run_ATAK_Pipeline.cmd"),
        (Join-Path $DesktopDir "ATAK Pipeline.cmd"),
        (Join-Path $DesktopDir "ATAK Imagery Downloader.cmd"),
        (Join-Path $DesktopDir "ATAK Device Installer.cmd")
    )) {
        if (Test-Path $path) {
            Remove-Item $path -Force
            Write-Log "Removed legacy launcher: $path"
        }
    }
}

function Install-Shortcuts {
    $installerExe = Join-Path $InstallDir "ATAKDeviceInstaller.exe"
    $imageryExe = Join-Path $InstallDir "ATAKImageryDownloader.exe"

    New-ProgramShortcut -LinkPath (Join-Path $DesktopDir "ATAK Device Installer.lnk") `
        -TargetExe $installerExe -WorkingDir $InstallDir `
        -Description "Install ATAK and TAK-UV-PRO on your phone over USB"

    New-ProgramShortcut -LinkPath (Join-Path $DesktopDir "ATAK Imagery Downloader.lnk") `
        -TargetExe $imageryExe -WorkingDir $InstallDir `
        -Description "Download USGS imagery and build ATAK map packages"

    New-ProgramShortcut -LinkPath (Join-Path $StartMenuDir "ATAK Device Installer.lnk") `
        -TargetExe $installerExe -WorkingDir $InstallDir `
        -Description "ATAK Device Installer"

    New-ProgramShortcut -LinkPath (Join-Path $StartMenuDir "ATAK Imagery Downloader.lnk") `
        -TargetExe $imageryExe -WorkingDir $InstallDir `
        -Description "ATAK Imagery Downloader"
}

# --- Main ---
"" | Set-Content -Path $LogFile -Encoding UTF8
Write-Log "ATAK Pipeline Windows setup starting"
Write-Log "Build root: $Root"
Write-Log "Install dir: $InstallDir"

Write-Log "[1/7] Python..."
$PythonExe = Resolve-PythonExe
if (-not $PythonExe) {
    Install-Python
    $PythonExe = Resolve-PythonExe
}
if (-not $PythonExe) {
    throw "Python setup failed. Install Python 3 manually, check Add to PATH, rerun install_windows.cmd"
}
Write-Log "Python: $PythonExe"

Write-Log "[2/7] Android platform-tools (adb)..."
$AdbPath = Find-AdbPath
if (-not $AdbPath) { $AdbPath = Install-PlatformTools }
$AdbDir = Split-Path -Parent $AdbPath
Ensure-UserPathContains $AdbDir
& $AdbPath version 2>&1 | Select-Object -First 1 | ForEach-Object { Write-Log "adb: $_" }

Write-Log "[3/7] deploy.env (build tree)..."
$DeployEnv = Join-Path $Root "deploy.env"
$DeployExample = Join-Path $Root "deploy.env.example"
if (-not (Test-Path $DeployExample)) { $DeployExample = Join-Path $WinBuild "deploy.env.example" }
if (-not (Test-Path $DeployEnv) -and (Test-Path $DeployExample)) {
    Copy-Item $DeployExample $DeployEnv
    Write-Log "Created deploy.env in repo root"
}

if (-not $SkipBuild) {
    $launcherPath = Join-Path $WinBuild "windows_launcher.py"
    $launcherText = Get-Content $launcherPath -Raw -ErrorAction Stop
    if ($launcherText -notmatch 'getattr\(sys,\s*"frozen"') {
        throw "Outdated $launcherPath will open the Imagery Downloader during build. Update the repo (git pull) before running setup."
    }
    Write-Log "[4/7] Building ATAK Device Installer + Imagery Downloader (several minutes)..."
    Stop-AtakAppProcesses
    Push-Location $Root
    try {
        & (Join-Path $WinBuild "build_windows_exe.ps1") -PythonExe $PythonExe
    } finally {
        Pop-Location
        Stop-AtakAppProcesses
    }
} else {
    Write-Log "[4/7] Skipping EXE build (-SkipBuild)."
}

Write-Log "[5/7] Installing to Programs folder..."
Install-BuiltPrograms
$InstallAdbDir = Join-Path $InstallDir "tools\platform-tools"
if (Test-Path $InstallAdbDir) {
    Ensure-UserPathContains $InstallAdbDir
}

Write-Log "[6/7] Creating Desktop + Start Menu shortcuts..."
Remove-LegacyLaunchers
Install-Shortcuts

Write-Log "[7/7] Setup complete."
Stop-AtakAppProcesses
Write-Log "Programs installed to: $InstallDir"
Write-Log "Log: $LogFile"
Write-Host ""
Write-Host "Installed to:"
Write-Host "  $InstallDir"
Write-Host ""
Write-Host "Desktop shortcuts:"
Write-Host "  ATAK Device Installer.lnk"
Write-Host "  ATAK Imagery Downloader.lnk"
Write-Host ""
Write-Host "Start Menu: Programs -> ATAK Pipeline"
Write-Host ""
Write-Host "Run either desktop icon when you are ready. Setup does not launch the apps."
Write-Host ""
