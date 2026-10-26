@echo off
setlocal
title 停止 voxd
cd /d "%~dp0"

echo 正在停止 8765 端口上的 voxd ...
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if(-not $c){Write-Host '  没有在运行'; exit 0}; $p=Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; if($p -and $p.ProcessName -eq 'python'){Stop-Process -Id $p.Id -Force; Write-Host ('  已停止 pid=' + $p.Id)} else {Write-Host '  端口被别的进程占用，未处理'; exit 1}"
echo 正在停止 9880 端口上的 age12 音色 ...
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 9880 -State Listen -ErrorAction SilentlyContinue; if(-not $c){Write-Host '  没有在运行'; exit 0}; $p=Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; if($p -and $p.ProcessName -eq 'python'){Stop-Process -Id $p.Id -Force; Write-Host ('  已停止 pid=' + $p.Id)} else {Write-Host '  端口被别的进程占用，未处理'}"
echo.
pause
