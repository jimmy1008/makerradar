@echo off
cd /d "%~dp0"
start "" /B pythonw -m uvicorn web_app:app --host 127.0.0.1 --port 8111
echo MakerRadar started in background (port 8111)
echo To stop: stop.bat
timeout /t 3 >nul
