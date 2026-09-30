@echo off
chcp 936 >nul
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PSSCRIPT=%ROOT%scripts\stop_service.ps1"

echo Stopping Enterprise RAG service...
echo --------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%PSSCRIPT%"
echo --------------------------------------------------
echo Service stopped. If ports are still occupied, wait a few seconds and retry.
echo To restart, run: Æô¶¯·þÎñ.bat
pause
