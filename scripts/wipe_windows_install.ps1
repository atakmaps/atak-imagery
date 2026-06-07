# Wipe ATAK Pipeline Windows install for a fresh ATAKSetup test.
# Run: powershell -ExecutionPolicy Bypass -File scripts\wipe_windows_install.ps1
param(
    [switch]$IncludeBuildTree
)

$ErrorActionPreference = "Continue"

function Stop-AtakProcesses {
    Write-Host "Stopping ATAK / adb processes..."
    foreach ($name in @("ATAKImageryDownloader", "ATAKDeviceInstaller", "ATAKPipeline", "adb")) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

function Invoke-InnoUninstall {
    # Inno Setup uninstall key ends with _is1
    $keys = @(
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    $found = $false
    foreach ($pattern in $keys) {
        Get-ChildItem $pattern -ErrorAction SilentlyContinue | ForEach-Object {
            $display = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DisplayName
            if ($display -ne "ATAK Pipeline") { return }
            $found = $true
            $uninst = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).UninstallString
            if (-not $uninst) {
                Write-Host "Uninstall registry entry found but UninstallString is empty - skipping."
                return
            }
            Write-Host "Uninstall registry: $uninst"
            $exe = $null
            $args = @("/VERYSILENT", "/NORESTART")
            if ($uninst -match '^"(?<path>[^"]+)"(?<rest>.*)$') {
                $exe = $matches.path
                if ($matches.rest.Trim()) {
                    $args = @($matches.rest.Trim().Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)) + $args
                }
            } else {
                $parts = $uninst.Split(" ", 2, [System.StringSplitOptions]::RemoveEmptyEntries)
                $exe = $parts[0]
                if ($parts.Count -gt 1) {
                    $args = @($parts[1].Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)) + $args
                }
            }
            if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
                Write-Host "Uninstaller missing on disk (already removed?) - continuing with manual wipe."
                return
            }
            Write-Host "Running uninstaller: $exe"
            try {
                $p = Start-Process -FilePath $exe -ArgumentList $args -Wait -PassThru -ErrorAction Stop
                Write-Host "Uninstaller exit code: $($p.ExitCode)"
            } catch {
                Write-Host "Uninstaller failed ($($_.Exception.Message)) - continuing with manual wipe."
            }
            Start-Sleep -Seconds 2
        }
    }
    if (-not $found) {
        Write-Host "No ATAK Pipeline uninstall registry entry - manual wipe only."
    }
}

function Remove-InstallArtifacts {
    $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\ATAK Pipeline"
    $DesktopDir = [Environment]::GetFolderPath("Desktop")
    $StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ATAK Pipeline"
    $LocalDataDir = Join-Path $env:LOCALAPPDATA "atak-pipeline"

    Write-Host "Removing install folder..."
    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Removing shortcuts..."
    foreach ($path in @(
        (Join-Path $DesktopDir "ATAK Device Installer.lnk"),
        (Join-Path $DesktopDir "ATAK Imagery Downloader.lnk"),
        (Join-Path $StartMenuDir "ATAK Device Installer.lnk"),
        (Join-Path $StartMenuDir "ATAK Imagery Downloader.lnk"),
        (Join-Path $StartMenuDir "Uninstall ATAK Pipeline.lnk")
    )) {
        if (Test-Path $path) { Remove-Item $path -Force -ErrorAction SilentlyContinue }
    }
    if ((Test-Path $StartMenuDir) -and -not (Get-ChildItem $StartMenuDir -ErrorAction SilentlyContinue)) {
        Remove-Item $StartMenuDir -Force -ErrorAction SilentlyContinue
    }

    foreach ($path in @(
        (Join-Path $DesktopDir "ATAK Pipeline.cmd"),
        (Join-Path $DesktopDir "ATAK Device Installer.cmd"),
        (Join-Path $DesktopDir "ATAK Imagery Downloader.cmd")
    )) {
        if (Test-Path $path) { Remove-Item $path -Force -ErrorAction SilentlyContinue }
    }

    Write-Host "Removing local data / logs..."
    if (Test-Path $LocalDataDir) {
        Remove-Item $LocalDataDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    # Stale Inno uninstall registry if uninstaller exe was deleted manually
    Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            $display = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DisplayName
            if ($display -eq "ATAK Pipeline") {
                $exe = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).UninstallString
                if ($exe -match '^"(?<p>[^"]+)"') { $exe = $matches.p }
                elseif ($exe) { $exe = ($exe -split '\s+')[0] }
                if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
                    Write-Host "Removing stale uninstall registry key: $($_.PSChildName)"
                    Remove-Item $_.PSPath -Force -ErrorAction SilentlyContinue
                }
            }
        }

    Write-Host ""
    Write-Host "Wipe complete."
    Write-Host "  Install dir should be gone: $InstallDir"
    if (Test-Path $InstallDir) {
        Write-Host "  WARNING: install dir still exists (files in use?) - close ATAK apps and rerun."
    }
}

Stop-AtakProcesses
Invoke-InnoUninstall
Remove-InstallArtifacts

if ($IncludeBuildTree) {
    Write-Host "Removing C:\ATAKBuild (full reset)..."
    Remove-Item "C:\ATAKBuild" -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Run the installer when ready, for example:"
Write-Host "  .\installer-dist\ATAKSetup-v1.3.37.exe"
