@echo off
rem Stop by port: netstat + taskkill (Get-NetTCPConnection may return multiple objects -> old script did nothing).
setlocal
cd /d "%~dp0"
for %%P in (8765 9880) do (
  for /f "tokens=5" %%I in ('netstat -ano ^| findstr ":%%P" ^| findstr LISTENING') do taskkill /PID %%I /T /F >nul 2>&1
)
exit /b 0
