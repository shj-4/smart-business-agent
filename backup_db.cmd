@echo off
REM مسار مرن: SMART_BUSINESS_AGENT_ROOT أو مجلد السكربت نفسه
set "ROOT=%~dp0"
if defined SMART_BUSINESS_AGENT_ROOT set "ROOT=%SMART_BUSINESS_AGENT_ROOT%"
if defined SMART_BUSINESS_ROOT set "ROOT=%SMART_BUSINESS_ROOT%"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"
"%ROOT%\venv\Scripts\python.exe" scripts\backup_db.py