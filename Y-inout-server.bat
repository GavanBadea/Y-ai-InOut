@echo off
cd /d "%~dp0"
title Y-ai InOut - Server (PC1)

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  echo.
  echo  ERROR: Python not found in PATH.
  echo  Install Python from https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo.
echo  === Installing requirements ===
%PY% -m pip install -r requirements.txt -q

echo.
echo  === Allow LAN port in Windows Firewall ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\allow-lan-port.ps1"

echo.
echo  Stopping old process on port 8000 (if any)...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>&1

echo.
echo  === Starting server ===
echo  This PC:   http://127.0.0.1:8000/login
echo  From WiFi: see the LAN URL printed below.
echo.

start "" cmd /c "ping 127.0.0.1 -n 3 >nul & start http://127.0.0.1:8000/login"

%PY% run_server.py
pause
