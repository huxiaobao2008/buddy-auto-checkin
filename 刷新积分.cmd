@echo off
chcp 65001 >nul
title 刷新积分台账
cd /d "%~dp0"

echo.
echo   [积分台账] 正在查询全部账号...
echo   （本操作只调官方接口，不需要启动客户端，也不会消耗积分）
echo.

python credit_report.py %*
if errorlevel 1 goto :fail

echo.
echo   刷新完成，正在打开报告...
start "" "logs\credits_report.html"
timeout /t 2 >nul
exit /b 0

:fail
echo.
echo   [错误] 刷新失败，请看上面的输出。
pause
exit /b 1
