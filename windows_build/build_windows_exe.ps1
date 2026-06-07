# Build ATAK Device Installer + Imagery Downloader EXEs (PyInstaller).
# Called by setup_windows_pipeline.ps1 or run directly from repo root.
param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$Root = (Get-Location).Path
$BuildRoot = Join-Path $Root "windows_build"
$ScriptsDir = Join-Path $Root "scripts"
$DistDir = Join-Path $Root "dist"
$BuildDir = Join-Path $Root "build"
$VersionFile = Join-Path $Root "VERSION"
$ToolsDir = Join-Path $Root "tools"
$PlatformToolsDir = Join-Path $ToolsDir "platform-tools"

foreach ($req in @(
    (Join-Path $BuildRoot "windows_launcher.py"),
    (Join-Path $BuildRoot "windows_installer_launcher.py"),
    (Join-Path $BuildRoot "atak_adb_deploy_win.py"),
    (Join-Path $BuildRoot "atak_downloader_finalbuild_win.py")
)) {
    if (-not (Test-Path $req)) {
        throw "Missing required file: $req`nRun: python scripts/sync_windows_build.py (on Linux) or git pull."
    }
}

function Resolve-BuildPython {
    param([string]$Preferred)
    if ($Preferred -and (Test-Path $Preferred)) {
        & $Preferred -c "import ssl, venv, tkinter" 2>$null
        if ($LASTEXITCODE -eq 0) { return $Preferred }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $py = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($py -and (Test-Path $py)) { return $py.Trim() }
    }
    $found = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    if ($found) { return $found }
    throw "Python not found for build."
}

$PythonExe = Resolve-BuildPython -Preferred $PythonExe
Write-Host ""
Write-Host "=== Syncing windows_build from scripts/ (Linux copy + Windows patches) ==="
& $PythonExe (Join-Path $ScriptsDir "sync_windows_build.py")
$PythonRoot = Split-Path -Parent $PythonExe
$TclDir = Join-Path $PythonRoot "tcl\tcl8.6"
$TkDir  = Join-Path $PythonRoot "tcl\tk8.6"

if (-not (Test-Path $TclDir)) { throw "Missing Tcl: $TclDir" }
if (-not (Test-Path $TkDir))  { throw "Missing Tk: $TkDir" }

$DataDir = Join-Path $BuildRoot "data"
foreach ($req in @("us_states.geojson", "zoom_estimates_z10_z16.json")) {
    if (-not (Test-Path (Join-Path $DataDir $req))) {
        throw "Missing required data file: $DataDir\$req"
    }
}

$Version = "0.0.0"
if (Test-Path $VersionFile) {
    $Version = (Get-Content $VersionFile -Raw).Trim()
    if ($Version.StartsWith("v")) { $Version = $Version.Substring(1) }
}

Write-Host "Build Python: $PythonExe"
& $PythonExe -m pip install --upgrade pip
$WinReq = Join-Path $Root "requirements-windows-build.txt"
if (Test-Path $WinReq) {
    & $PythonExe -m pip install -r $WinReq
} else {
    & $PythonExe -m pip install pyinstaller requests mgrs packaging
}

$env:TCL_LIBRARY = $TclDir
$env:TK_LIBRARY  = $TkDir

Remove-Item $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

$HiddenImports = @(
    "atak_downloader_finalbuild_win",
    "atak_downloader_from_installer_win",
    "atak_imagery_sqlite_builder_finalbuild_win",
    "atak_dted_downloader_win",
    "atak_adb_deploy_win",
    "bundled_plugin_install",
    "git_update_check",
    "imagery_tile_selection",
    "tk_window_scaling",
    "win_subprocess",
    "usgs_throughput_probe",
    "mgrs",
    "mgrs.core",
    "packaging",
    "packaging.tags",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.simpledialog",
    "tkinter.ttk",
    "_tkinter"
)

$ScriptBundles = @(
    "imagery_tile_selection.py",
    "git_update_check.py",
    "tk_window_scaling.py",
    "win_subprocess.py",
    "bundled_plugin_install.py",
    "usgs_throughput_probe.py"
)

function Stop-AtakAppProcesses {
    foreach ($name in @("ATAKImageryDownloader", "ATAKDeviceInstaller", "ATAKPipeline")) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

function Build-OneExe {
    param(
        [string]$Name,
        [string]$Launcher,
        [string]$WorkSubdir
    )
    $WorkPath = Join-Path $BuildDir $WorkSubdir
    $SpecPath = Join-Path $Root "$Name.spec"
    Remove-Item $WorkPath -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $SpecPath -Force -ErrorAction SilentlyContinue

    $pyArgs = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", $Name,
        "--paths", $BuildRoot,
        "--paths", $ScriptsDir,
        "--distpath", $DistDir,
        "--workpath", $WorkPath,
        "--add-data", "${TclDir};_tcl_data",
        "--add-data", "${TkDir};_tk_data",
        "--add-data", "$DataDir;scripts\data"
    )
    foreach ($scriptName in $ScriptBundles) {
        $src = Join-Path $BuildRoot $scriptName
        if (-not (Test-Path $src)) { $src = Join-Path $ScriptsDir $scriptName }
        if (Test-Path $src) {
            $pyArgs += @("--add-data", "${src};scripts\")
        }
    }
    foreach ($hi in $HiddenImports) {
        $pyArgs += @("--hidden-import", $hi)
    }
    # mgrs uses a platform libmgrs.*.pyd beside the package — must collect binaries.
    $pyArgs += @("--collect-all", "mgrs")
    $pyArgs += $Launcher

    Write-Host ""
    Write-Host "=== Building $Name.exe (v$Version) ==="
    Stop-AtakAppProcesses
    & $PythonExe @pyArgs
    Stop-AtakAppProcesses
    $out = Join-Path $DistDir "$Name.exe"
    if (-not (Test-Path $out)) {
        throw "Build did not produce $out"
    }
    Write-Host "Created: $out"
}

Build-OneExe -Name "ATAKImageryDownloader" -Launcher (Join-Path $BuildRoot "windows_launcher.py") -WorkSubdir "imagery"
Build-OneExe -Name "ATAKDeviceInstaller" -Launcher (Join-Path $BuildRoot "windows_installer_launcher.py") -WorkSubdir "installer"

$DeployExample = Join-Path $Root "deploy.env.example"
if (-not (Test-Path $DeployExample)) { $DeployExample = Join-Path $BuildRoot "deploy.env.example" }
if (Test-Path $DeployExample) {
    Copy-Item $DeployExample (Join-Path $DistDir "deploy.env.example") -Force
    $deployDest = Join-Path $DistDir "deploy.env"
    if (-not (Test-Path $deployDest)) {
        Copy-Item $DeployExample $deployDest -Force
    }
}
if (Test-Path $VersionFile) {
    Copy-Item $VersionFile (Join-Path $DistDir "VERSION") -Force
}

# Bundle adb beside EXEs so frozen apps find it without a global PATH install.
if (Test-Path (Join-Path $PlatformToolsDir "adb.exe")) {
    $distTools = Join-Path $DistDir "tools\platform-tools"
    New-Item -ItemType Directory -Force -Path $distTools | Out-Null
    Copy-Item (Join-Path $PlatformToolsDir "*") $distTools -Recurse -Force
    Write-Host "Copied platform-tools to $distTools"
}

Write-Host ""
Write-Host "Build complete (v$Version):"
Write-Host "  $(Join-Path $DistDir 'ATAKImageryDownloader.exe')"
Write-Host "  $(Join-Path $DistDir 'ATAKDeviceInstaller.exe')"
