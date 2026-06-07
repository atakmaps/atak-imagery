@echo off
setlocal
title ATAK Pipeline Setup

echo.
echo  ATAK Pipeline - Windows Setup
echo  =============================
echo.
echo  This installs everything needed and creates two programs:
echo    - ATAK Device Installer
echo    - ATAK Imagery Downloader
echo.
echo  Desktop icons will appear when setup finishes.
echo  Setup takes about 10-20 minutes. Nothing will launch automatically.
echo.
echo  Log: setup_windows.log
echo.
pause

cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows_pipeline.ps1"
set ERR=%ERRORLEVEL%
echo.
if %ERR% neq 0 (
  echo Setup failed. Open setup_windows.log in this folder for details.
) else (
  echo Setup finished.
  echo Use the desktop icons: ATAK Device Installer / ATAK Imagery Downloader
)
echo.
pause
exit /b %ERR%
