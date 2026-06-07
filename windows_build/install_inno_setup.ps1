# Install Inno Setup 6 on this Windows build VM (required once to compile ATAKSetup.exe).
# Run from repo root:
#   powershell -ExecutionPolicy Bypass -File windows_build\install_inno_setup.ps1
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$InnoSetupUrl = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
$CacheDir = Join-Path $env:LOCALAPPDATA "atak-pipeline\cache"
$InstallerPath = Join-Path $CacheDir "innosetup-6.7.3.exe"

function Find-Iscc {
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 7\ISCC.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe")
    )) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    foreach ($hive in @("HKLM:\SOFTWARE", "HKLM:\SOFTWARE\WOW6432Node", "HKCU:\SOFTWARE")) {
        foreach ($suffix in @("Inno Setup 6_is1", "Inno Setup 7_is1")) {
            $key = Join-Path $hive "Microsoft\Windows\CurrentVersion\Uninstall\$suffix"
            if (-not (Test-Path $key)) { continue }
            $dir = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallLocation
            if ($dir) {
                $iscc = Join-Path $dir.TrimEnd('\') "ISCC.exe"
                if (Test-Path -LiteralPath $iscc) { return $iscc }
            }
        }
    }
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path -LiteralPath $cmd.Source)) { return $cmd.Source }
    return $null
}

Write-Host ""
Write-Host "=== Inno Setup installer for ATAK Pipeline build VM ==="

$existing = Find-Iscc
if ($existing -and -not $Force) {
    Write-Host "Already installed: $existing"
    Write-Host ""
    Write-Host "Next:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File windows_build\build_windows_installer.ps1"
    exit 0
}

Write-Host "ISCC.exe not found - installing Inno Setup 6..."

if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host ""
    Write-Host "Trying winget..."
    try {
        winget install --id JRSoftware.InnoSetup -e `
            --accept-package-agreements --accept-source-agreements `
            --disable-interactivity
        Start-Sleep -Seconds 5
        $existing = Find-Iscc
        if ($existing) {
            Write-Host "Installed via winget: $existing"
            exit 0
        }
        Write-Host "winget finished but ISCC.exe not found yet - trying direct download..."
    } catch {
        Write-Host "winget failed: $($_.Exception.Message)"
        Write-Host "Trying direct download..."
    }
}

Write-Host ""
Write-Host "Downloading Inno Setup 6.7.3..."
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
if (Test-Path $InstallerPath) { Remove-Item $InstallerPath -Force }
Invoke-WebRequest -Uri $InnoSetupUrl -OutFile $InstallerPath -UseBasicParsing
if (-not (Test-Path $InstallerPath)) {
    throw "Download failed: $InnoSetupUrl"
}
Write-Host "Downloaded: $InstallerPath"

Write-Host ""
Write-Host "Running silent install (no UI)..."
$p = Start-Process -FilePath $InstallerPath -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"
) -Wait -PassThru
Write-Host "Installer exit code: $($p.ExitCode)"
Start-Sleep -Seconds 3

$existing = Find-Iscc
if (-not $existing) {
    throw @"
Inno Setup install did not create ISCC.exe.

Try manual install:
  1. Open: $InstallerPath
  2. Click through the wizard (defaults are fine)
  3. Reopen PowerShell and run:
       Test-Path `"${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe`"
  4. Then:
       powershell -ExecutionPolicy Bypass -File windows_build\build_windows_installer.ps1
"@
}

Write-Host ""
Write-Host "Success: $existing"
Write-Host ""
Write-Host "Next:"
Write-Host "  cd C:\ATAKBuild\pipeline"
Write-Host "  powershell -ExecutionPolicy Bypass -File windows_build\build_windows_installer.ps1"
