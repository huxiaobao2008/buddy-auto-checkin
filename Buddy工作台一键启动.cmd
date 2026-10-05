@echo off
setlocal
title Buddy 工作台 - 启动器

cd /d "%~dp0"

set "PY="
if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined PY if exist "D:\empire_tools\green_tools\python\python.exe" set "PY=D:\empire_tools\green_tools\python\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  echo [ERROR] 找不到可用的 Python。
  pause
  exit /b 2
)

echo 使用 Python: %PY%
"%PY%" "%~dp0launcher.py"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo [ERROR] 启动失败（退出码 %RC%），请查看 logs\launcher.log。
pause
exit /b %RC%
