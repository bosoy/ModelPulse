@echo off
setlocal
cd /d "%~dp0"
title ModelPulse

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH.
  echo Install Python or edit this file to point at python.exe
  pause
  exit /b 1
)

echo ModelPulse starting on http://127.0.0.1:8765
echo Press Ctrl+C in this window to stop the monitor.
start "" "http://127.0.0.1:8765"
python server.py --port 8765 --interval 900
echo.
echo ModelPulse stopped.
pause
