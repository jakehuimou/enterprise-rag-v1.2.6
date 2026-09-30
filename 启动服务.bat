@echo off
chcp 936 >nul
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "RUN=%ROOT%scripts\run.py"
set "LOG=%ROOT%data\logs\startup.log"

REM Check virtual environment
if not exist "%PYTHON%" (
    echo [Error] Venv python not found: %PYTHON%
    echo Please create .venv and install requirements first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

REM Check if already running on port 7860
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue) { exit 1 }" 2>nul
if errorlevel 1 (
    echo [Info] RAG service seems already running on port 7860.
    echo Opening existing UI...
    start "" "http://127.0.0.1:7860"
    pause
    exit /b 0
)

if not exist "%ROOT%data\logs" mkdir "%ROOT%data\logs"

echo Starting Enterprise RAG service...
echo   Backend API : http://127.0.0.1:8000
echo   Frontend UI : http://127.0.0.1:7860
echo   Startup log : %LOG%
echo (Service runs in its own minimized window; close this window freely)

start "EnterpriseRAG" /min cmd /c ""%PYTHON%" "%RUN%" >> "%LOG%" 2>&1"

echo Waiting for service ready and opening browser...
timeout /t 6 >nul
start "" "http://127.0.0.1:7860"
echo If browser does not open, visit http://127.0.0.1:7860 manually.
pause
