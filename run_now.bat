@echo off
chcp 65001 >nul
cd /d "D:\workbuddy自动签到领积分完成成长任务"
"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" -u -c "from orchestrator.run_all import run_all; run_all()"
pause
