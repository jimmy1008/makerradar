@echo off
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8111 ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo MakerRadar stopped.
timeout /t 2 >nul
