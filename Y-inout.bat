@echo off
cd /d "%~dp0"
title Y-ai InOut

REM Main launcher: starts server then opens browser
REM Network PC1: Y-inout-server.bat | clients: Y-inout-client.bat

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  echo.
  echo  ERROR: Python not found in PATH.
  echo  Install Python from https://www.python.org/downloads/
  echo  Enable "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

echo.
echo  Installing requirements...
%PY% -m pip install -r requirements.txt -q

echo.
echo  Stopping old process on port 8000 (if any)...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>&1

echo.
echo  Starting Y-ai InOut — keep this window open.
echo  Browser: http://127.0.0.1:8000/login
echo.

start "" cmd /c "ping 127.0.0.1 -n 3 >nul & start http://127.0.0.1:8000/login"

%PY% run_server.py
if errorlevel 1 (
  echo.
  echo  Startup failed. Check the message above.
  echo  If port 8000 is busy, close a previous Y-ai window.
)
pause
