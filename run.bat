@echo off
setlocal
title voxd 语音服务
cd /d "%~dp0"

powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if($c){exit 0}else{exit 1}"
if not errorlevel 1 (
    echo.
    echo   voxd 已经在运行 http://127.0.0.1:8765/
    echo   要重启就先双击 stop.bat
    echo.
    pause
    exit /b 0
)

echo ================================================================
echo   voxd 本地语音服务
echo   试音台 http://127.0.0.1:8765/
echo   这个窗口就是服务本身，关掉它等于停止语音服务
echo ================================================================
echo.
echo 正在启动，首次加载引擎约 3 秒...
echo.

if not exist logs mkdir logs

rem age12 音色（GPT-SoVITS，9880）：已经在跑就不重复开
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 9880 -State Listen -ErrorAction SilentlyContinue; if($c){exit 0}else{exit 1}"
if errorlevel 1 (
    echo 正在另开窗口启动 age12 音色 GPT-SoVITS 9880，约 40 秒...
    start "GPT-SoVITS 9880" "%~dp0gsv.bat"
) else (
    echo age12 音色已在运行 9880
)
echo.

".venv\Scripts\python.exe" -m voxd --config config.toml

echo.
echo [voxd] 已退出，退出码 %errorlevel%
echo [voxd] 启动失败时上面会有报错，常见原因 8765 被占 / 模型文件缺失
pause
