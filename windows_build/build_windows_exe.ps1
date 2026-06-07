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
$PlatformToolsUrl = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
$PlatformToolsZip = Join-Path $ToolsDir "platform-tools-latest-windows.zip"

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

function Get-WindowsBundleManifest {
    param([string]$Py, [string]$ScriptsRoot)
    $raw = & $Py (Join-Path $ScriptsRoot "windows_bundle_manifest.py") --json
    if ($LASTEXITCODE -ne 0) { throw "windows_bundle_manifest.py --json failed: $raw" }
    return $raw | ConvertFrom-Json
}

function Format-PyInstallerAddData {
    param(
        [Parameter(Mandatory)][string]$SourcePath,
        [Parameter(Mandatory)][string]$DestFolder
    )
    # PyInstaller on Windows: source;dest  (never use "path\" in double quotes - breaks PS parsing)
    return ('{0};{1}' -f $SourcePath, $DestFolder)
}

function Stop-AtakAppProcesses {
    foreach ($name in @("ATAKImageryDownloader", "ATAKDeviceInstaller", "ATAKPipeline")) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-PlatformTools {
    if (Test-Path (Join-Path $PlatformToolsDir "adb.exe")) {
        return (Join-Path $PlatformToolsDir "adb.exe")
    }

    Write-Host "=== platform-tools not found; downloading Android platform-tools ==="
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

    if (Test-Path $PlatformToolsZip) {
        Remove-Item $PlatformToolsZip -Force -ErrorAction SilentlyContinue
    }

    Invoke-WebRequest -Uri $PlatformToolsUrl -OutFile $PlatformToolsZip -UseBasicParsing

    $extractRoot = Join-Path $ToolsDir "_extract_platform_tools"
    if (Test-Path $extractRoot) {
        Remove-Item $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    Expand-Archive -Path $PlatformToolsZip -DestinationPath $extractRoot -Force
    $inner = Join-Path $extractRoot "platform-tools"
    if (-not (Test-Path (Join-Path $inner "adb.exe"))) {
        throw "platform-tools zip did not contain adb.exe"
    }

    if (Test-Path $PlatformToolsDir) {
        Remove-Item $PlatformToolsDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Move-Item $inner $PlatformToolsDir
    Remove-Item $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $PlatformToolsZip -Force -ErrorAction SilentlyContinue
    return (Join-Path $PlatformToolsDir "adb.exe")
}

function Build-OneExe {
    param(
        [string]$Py,
        [string]$Name,
        [string]$Launcher,
        [string]$WorkSubdir,
        [string]$BuildRootPath,
        [string]$ScriptsRoot,
        [string]$DistRoot,
        [string]$BuildWorkRoot,
        [string]$TclPath,
        [string]$TkPath,
        [string]$DataRoot,
        [string]$VersionLabel,
        [array]$HiddenImportList,
        [array]$ScriptBundleList,
        [array]$CollectAllList,
        $MgrsPydFile
    )
    $WorkPath = Join-Path $BuildWorkRoot $WorkSubdir
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
        "--paths", $BuildRootPath,
        "--paths", $ScriptsRoot,
        "--distpath", $DistRoot,
        "--workpath", $WorkPath,
        "--add-data", (Format-PyInstallerAddData -SourcePath $TclPath -DestFolder "_tcl_data"),
        "--add-data", (Format-PyInstallerAddData -SourcePath $TkPath -DestFolder "_tk_data"),
        "--add-data", (Format-PyInstallerAddData -SourcePath $DataRoot -DestFolder "scripts/data")
    )
    foreach ($scriptName in $ScriptBundleList) {
        $src = Join-Path $BuildRootPath $scriptName
        if (-not (Test-Path $src)) { $src = Join-Path $ScriptsRoot $scriptName }
        if (Test-Path $src) {
            $pyArgs += @("--add-data", (Format-PyInstallerAddData -SourcePath $src -DestFolder "scripts"))
        }
    }
    foreach ($hi in $HiddenImportList) {
        $pyArgs += @("--hidden-import", $hi)
    }
    foreach ($pkg in $CollectAllList) {
        $pyArgs += @("--collect-all", $pkg)
    }
    if ($MgrsPydFile) {
        $pyArgs += @("--add-binary", (Format-PyInstallerAddData -SourcePath $MgrsPydFile.FullName -DestFolder "."))
        $pyArgs += @("--add-binary", (Format-PyInstallerAddData -SourcePath $MgrsPydFile.FullName -DestFolder "mgrs"))
    }
    $pyArgs += $Launcher

    Write-Host ""
    Write-Host "=== Building $Name.exe (v$VersionLabel) ==="
    Stop-AtakAppProcesses
    & $Py @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $Name (exit $LASTEXITCODE)" }
    Stop-AtakAppProcesses
    $out = Join-Path $DistRoot "$Name.exe"
    if (-not (Test-Path $out)) {
        throw "Build did not produce $out"
    }
    Write-Host "Created: $out"
}

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

$PythonExe = Resolve-BuildPython -Preferred $PythonExe
Write-Host ""
Write-Host "=== Syncing windows_build from scripts/ (Linux copy + Windows patches) ==="
& $PythonExe (Join-Path $ScriptsDir "sync_windows_build.py")
if ($LASTEXITCODE -ne 0) { throw "sync_windows_build.py failed" }

Write-Host "=== Auditing Windows bundle (manifest + py_compile) ==="
& $PythonExe (Join-Path $ScriptsDir "audit_windows_bundle.py")
if ($LASTEXITCODE -ne 0) { throw "audit_windows_bundle.py failed - fix missing modules/deps before building" }

$BundleManifest = Get-WindowsBundleManifest -Py $PythonExe -ScriptsRoot $ScriptsDir
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
    & $PythonExe -m pip install pyinstaller requests mgrs packaging certifi urllib3 charset-normalizer idna
}

$MgrsNativePyd = $null
try {
    $sitePackages = (& $PythonExe -c "import site; print(site.getsitepackages()[0])").Trim()
    if ($sitePackages -and (Test-Path $sitePackages)) {
        $MgrsNativePyd = Get-ChildItem $sitePackages -Filter "libmgrs*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $MgrsNativePyd) {
            $MgrsNativePyd = Get-ChildItem $sitePackages -Recurse -Filter "libmgrs*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
        }
    }
} catch { }
if ($MgrsNativePyd) {
    Write-Host "MGRS native library: $($MgrsNativePyd.FullName)"
} else {
    Write-Warning "libmgrs*.pyd not found after pip install mgrs - radius MGRS entry will fail in the EXE."
}

$env:TCL_LIBRARY = $TclDir
$env:TK_LIBRARY  = $TkDir

Remove-Item $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

Write-Host "=== Smoke-importing bundled modules (Windows build venv) ==="
& $PythonExe (Join-Path $ScriptsDir "audit_windows_bundle.py") --smoke-imports
if ($LASTEXITCODE -ne 0) { throw "Import smoke test failed - a runtime module or pip dependency is missing" }

$HiddenImports = @($BundleManifest.hidden_imports)
$ScriptBundles = @($BundleManifest.script_bundles)
$CollectAll = @($BundleManifest.collect_all)

$buildCommon = @{
    Py               = $PythonExe
    BuildRootPath    = $BuildRoot
    ScriptsRoot      = $ScriptsDir
    DistRoot         = $DistDir
    BuildWorkRoot    = $BuildDir
    TclPath          = $TclDir
    TkPath           = $TkDir
    DataRoot         = $DataDir
    VersionLabel     = $Version
    HiddenImportList = $HiddenImports
    ScriptBundleList = $ScriptBundles
    CollectAllList   = $CollectAll
    MgrsPydFile      = $MgrsNativePyd
}

Build-OneExe @buildCommon -Name "ATAKImageryDownloader" -Launcher (Join-Path $BuildRoot "windows_launcher.py") -WorkSubdir "imagery"
Build-OneExe @buildCommon -Name "ATAKDeviceInstaller" -Launcher (Join-Path $BuildRoot "windows_installer_launcher.py") -WorkSubdir "installer"

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

$bundledAdb = $null
try {
    $bundledAdb = Ensure-PlatformTools
} catch {
    Write-Warning "Could not auto-download platform-tools: $($_.Exception.Message)"
}
if ($bundledAdb -and (Test-Path $bundledAdb)) {
    $distTools = Join-Path $DistDir "tools\platform-tools"
    New-Item -ItemType Directory -Force -Path $distTools | Out-Null
    Copy-Item (Join-Path $PlatformToolsDir "*") $distTools -Recurse -Force
    Write-Host "Copied platform-tools to $distTools"
} else {
    Write-Warning "adb.exe not available for bundling; dist build will require adb on PATH."
}

Write-Host ""
Write-Host "Build complete (v$Version):"
Write-Host "  $(Join-Path $DistDir 'ATAKImageryDownloader.exe')"
Write-Host "  $(Join-Path $DistDir 'ATAKDeviceInstaller.exe')"

$InstallerScript = Join-Path $BuildRoot "build_windows_installer.ps1"
if (Test-Path $InstallerScript) {
    $IsccCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 7\ISCC.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($IsccCandidates) {
        Write-Host ""
        Write-Host "=== Building end-user setup installer (Inno Setup) ==="
        & powershell -NoProfile -ExecutionPolicy Bypass -File $InstallerScript
    } else {
        Write-Host ""
        Write-Host "Inno Setup 6 not found - skipped ATAKSetup.exe (install from https://jrsoftware.org/isdl.php)"
    }
}
