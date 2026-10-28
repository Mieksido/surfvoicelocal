@echo off
rem Started by SurfCore: NO pause here (hidden window would hang forever).
setlocal
cd /d "%~dp0"

powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if($c){exit 0}else{exit 1}"
if not errorlevel 1 exit /b 0

if not exist logs mkdir logs
if not exist logs\gsv mkdir logs\gsv

rem local voice engine (GPT-SoVITS 9880): start silently with pythonw if not listening
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 9880 -State Listen -ErrorAction SilentlyContinue; if($c){exit 0}else{exit 1}"
if errorlevel 1 (
    if exist "%~dp0gsv.bat" start "" /b "%~dp0gsv.bat"
)

rem voxd via pythonw (no console window), output to log
start "" /b "%~dp0.venv\Scripts\pythonw.exe" -m voxd --config config.toml >> "%~dp0logsoxd.out.log" 2>&1
exit /b 0
