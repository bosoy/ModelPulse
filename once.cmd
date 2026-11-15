@echo off
setlocal
cd /d "%~dp0"
title ModelPulse - single pass

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH.
  pause
  exit /b 1
)

echo Running one probe pass over all configured models...
echo The HTML report will open in a separate browser window.
echo.
python once.py %*
echo.
echo Done. The report is also saved under data\reports\.
pause
