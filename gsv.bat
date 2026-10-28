@echo off
rem GPT-SoVITS engine (port 9880), started silently by run.bat. NO pause.
setlocal
cd /d "%~dp0"
if not exist logs\gsv mkdir logs\gsv
set PYTHONPATH=%CD%endor\GPT-SoVITS\code\GPT_SoVITS;%CD%endor\GPT-SoVITS\code
set PYTHONIOENCODING=utf-8
cd /d "%~dp0vendor\GPT-SoVITS\code"
"%~dp0.venv-gsv\Scripts\pythonw.exe" -s api_v2.py -a 127.0.0.1 -p 9880 -c "%~dp0logs\gsv	ts_infer.yaml" >> "%~dp0logs\gsvpi.log" 2>&1
exit /b 0
