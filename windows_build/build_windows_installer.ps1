# Build end-user ATAKSetup-vX.Y.Z.exe (Inno Setup) from dist\ EXEs.
# Run from repo root after build_windows_exe.ps1.
param(
    [string]$IsccPath = ""
)

$ErrorActionPreference = "Stop"

$Root = (Get-Location).Path
$DistDir = Join-Path $Root "dist"
$IssFile = Join-Path $Root "ATAK_Setup.iss"
$VersionFile = Join-Path $Root "VERSION"
$OutDir = Join-Path $Root "installer-dist"

function Write-Step([string]$Msg) {
    Write-Host ""
    Write-Host "=== $Msg ==="
}

function Resolve-Iscc {
    param([string]$Preferred)
    if ($Preferred -and (Test-Path -LiteralPath $Preferred)) {
        return (Resolve-Path -LiteralPath $Preferred).Path
    }
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 7\ISCC.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path -LiteralPath $cmd.Source)) { return $cmd.Source }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        $found = Get-ChildItem -Path $root -Filter ISCC.exe -Recurse -Depth 5 -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'Inno Setup' } |
            Select-Object -First 1 -ExpandProperty FullName
        if ($found) { return $found }
    }
    return $null
}

Write-Step "Checking build outputs"
$Required = @(
    (Join-Path $DistDir "ATAKDeviceInstaller.exe"),
    (Join-Path $DistDir "ATAKImageryDownloader.exe")
)
foreach ($path in $Required) {
    if (-not (Test-Path $path)) {
        throw "Missing $path - run windows_build\build_windows_exe.ps1 first."
    }
    Write-Host "  OK: $path"
}

$adb = Join-Path $DistDir "tools\platform-tools\adb.exe"
if (Test-Path $adb) {
    Write-Host "  OK: $adb"
} else {
    Write-Warning "adb.exe not in dist\tools\platform-tools - installer will skip platform-tools (USB features may need adb on PATH)."
}

foreach ($name in @("deploy.env.example", "VERSION")) {
    $distCopy = Join-Path $DistDir $name
    $rootCopy = Join-Path $Root $name
    if (Test-Path $distCopy) {
        Write-Host "  OK: $distCopy"
    } elseif (Test-Path $rootCopy) {
        Write-Host "  OK: $rootCopy (repo root)"
    } else {
        Write-Warning "Optional file missing: $name"
    }
}

$Version = "0.0.0"
if (Test-Path $VersionFile) {
    $Version = (Get-Content $VersionFile -Raw).Trim()
    if ($Version.StartsWith("v")) { $Version = $Version.Substring(1) }
}
Write-Host "  Version: $Version"

Write-Step "Checking Inno Setup compiler"
$Iscc = Resolve-Iscc -Preferred $IsccPath
if (-not $Iscc) {
    throw @"
Inno Setup 6 is not installed (ISCC.exe not found).

Run this first (downloads and installs silently):

  powershell -ExecutionPolicy Bypass -File windows_build\install_inno_setup.ps1

Or install manually from https://jrsoftware.org/isdl.php then rerun:

  powershell -ExecutionPolicy Bypass -File windows_build\build_windows_installer.ps1

If ISCC is in a custom location:

  powershell -ExecutionPolicy Bypass -File windows_build\build_windows_installer.ps1 -IsccPath "C:\path\to\ISCC.exe"
"@
}
Write-Host "  ISCC: $Iscc"

if (-not (Test-Path $IssFile)) {
    throw "Missing $IssFile"
}
Write-Host "  Script: $IssFile"

Write-Step "Compiling ATAKSetup-v$Version.exe (no console windows)"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
& $Iscc "/DMyAppVersion=$Version" $IssFile
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup compile failed (exit $LASTEXITCODE)"
}

$SetupExe = Join-Path $OutDir "ATAKSetup-v$Version.exe"
if (-not (Test-Path $SetupExe)) {
    throw "Expected installer not found: $SetupExe"
}

Write-Step "Done"
Write-Host "Created: $SetupExe"
Write-Host ""
Write-Host "End users: double-click ATAKSetup-v$Version.exe"
Write-Host "  - Progress window only (no terminal)"
Write-Host "  - No prompts during install"
Write-Host "  - Finish page shows next steps"
