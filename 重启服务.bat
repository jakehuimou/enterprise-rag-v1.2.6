@echo off
chcp 936 >nul
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PSSCRIPT=%ROOT%scripts\stop_service.ps1"

echo Restarting Enterprise RAG service...
echo --------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%PSSCRIPT%"
echo --------------------------------------------------
echo Waiting 3 seconds for ports to release...
timeout /t 3 >nul
echo Starting...
call "%ROOT%Æô¶¯·þÎñ.bat"
