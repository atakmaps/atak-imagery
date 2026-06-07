# Runs automated setup only — never launches ATAK apps.
$Root = Split-Path -Parent $PSScriptRoot
& (Join-Path $Root "scripts\setup_windows_pipeline.ps1") @args
