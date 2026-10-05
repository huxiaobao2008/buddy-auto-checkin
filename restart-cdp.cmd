@echo off
setlocal
title WorkBuddy 客户端重启（带 CDP 调试端口）

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

"%PY%" "%~dp0client_cdp.py" --restart
pause
