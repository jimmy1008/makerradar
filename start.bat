@echo off
cd /d "%~dp0"
python -m uvicorn web_app:app --host 127.0.0.1 --port 8111 --reload
pause
