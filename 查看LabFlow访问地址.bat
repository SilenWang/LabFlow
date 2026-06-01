@echo off
cd /d "%~dp0"

echo Checking LabFlow service...
python start_labflow_background.py

echo.
echo Available LabFlow LAN URLs:
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "ipconfig | Select-String 'IPv4' | ForEach-Object { if ($_.Line -match '(\d+\.\d+\.\d+\.\d+)') { 'http://' + $Matches[1] + ':8080' } }"

echo.
echo After switching this server PC to network B, run this file again.
echo Send the B-network URL above to your colleagues.
echo.
pause
