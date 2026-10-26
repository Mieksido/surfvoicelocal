@echo off
setlocal
title GPT-SoVITS 9880 (age12)
cd /d "%~dp0"

echo ================================================================
echo   GPT-SoVITS - age12 定稿音色
echo   端口 9880 - 加载约 40 秒
echo   关掉这个窗口就会停止这个音色，voxd 会退回 Piper
echo ================================================================
echo.

if not exist logs mkdir logs
set PYTHONPATH=%CD%\vendor\GPT-SoVITS\code\GPT_SoVITS;%CD%\vendor\GPT-SoVITS\code
set PATH=%CD%\tools\bin;%PATH%
set PYTHONIOENCODING=utf-8

cd /d "%~dp0vendor\GPT-SoVITS\code"
"%~dp0.venv-gsv\Scripts\python.exe" -s api_v2.py -a 127.0.0.1 -p 9880 -c "%~dp0logs\gsv\tts_infer.yaml"

echo.
echo [gsv] 已退出，退出码 %errorlevel%
pause
