@echo off
cd /d "%~dp0"
title Y-ai InOut - Open from network

for /f "delims=" %%U in ('python -c "import json; d=json.load(open('network_config.json',encoding='utf-8')); print((d.get('server_url') or '').strip())" 2^>nul') do set "SERVER_URL=%%U"

if not defined SERVER_URL (
  echo server_url not found in network_config.json
  echo Edit the file and set e.g. http://192.168.1.100:8000
  pause
  exit /b 1
)

echo Opening: %SERVER_URL%
start "" "%SERVER_URL%"
exit /b 0
