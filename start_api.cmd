@echo off
REM مسار مرن: SMART_BUSINESS_AGENT_ROOT أو مجلد السكربت نفسه (%~dp0)
set "ROOT=%~dp0"
if defined SMART_BUSINESS_AGENT_ROOT set "ROOT=%SMART_BUSINESS_AGENT_ROOT%"
if defined SMART_BUSINESS_ROOT set "ROOT=%SMART_BUSINESS_ROOT%"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"
if errorlevel 1 exit /b 78
if not exist "%ROOT%\venv\Scripts\python.exe" (
  echo ERROR: Project virtual environment Python was not found: "%ROOT%\venv\Scripts\python.exe" 1>&2
  exit /b 78
)
set "PYTHONUNBUFFERED=1"
"%ROOT%\venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
exit /b %ERRORLEVEL%
