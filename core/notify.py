#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警：Windows 气泡 + 告警日志 + 可选 webhook。任何一环失败都不影响主流程。"""
import json
import os
import subprocess
import sys
import urllib.request

from . import config as C

TOAST_SCRIPT = os.path.join(C.BASE_DIR, "core", "toast.py")


def write_alert(text):
    cfg = C.load_config()
    path = os.path.join(C.BASE_DIR, cfg["alert"].get("alert_log", "logs/alerts.log"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (C.now_str(), text))


def desktop_toast(title, message):
    """Windows 托盘气泡：优先独立 pythonw 进程跑 toast.py（纯 Win32 API），
    失败退回 PowerShell，都失败也只写告警日志。"""
    runners = []
    if os.path.exists(TOAST_SCRIPT):
        base, ext = os.path.splitext(sys.executable)
        pythonw = base + "w" + ext
        runners.append(pythonw if os.path.exists(pythonw) else sys.executable)
    try:
        if runners:
            subprocess.Popen(
                [runners[0], TOAST_SCRIPT, title, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        raise FileNotFoundError("toast.py 缺失")
    except Exception as e:
        write_alert("[桌面提醒降级] %s" % e)

    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$ni = New-Object System.Windows.Forms.NotifyIcon;"
        "$ni.Icon = [System.Drawing.SystemIcons]::Warning;"
        "$ni.BalloonTipTitle = '%s';"
        "$ni.BalloonTipText = '%s';"
        "$ni.BalloonTipIcon = 'Warning';"
        "$ni.Visible = $true; $ni.ShowBalloonTip(15000);"
        "Start-Sleep -Seconds 12; $ni.Dispose()"
    ) % (title.replace("'", "''"), message.replace("'", "''"))
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        write_alert("[桌面提醒失败] %s" % e)


def notify(title, message, level="error"):
    cfg = C.load_config()
    alert = cfg.get("alert", {})
    write_alert("[%s] %s | %s" % (level, title, message))
    if alert.get("desktop_notify", True):
        desktop_toast(title, message)
    hook = alert.get("webhook_url", "")
    if hook:
        try:
            body = json.dumps({"msgtype": "text", "text": {"content": "%s\n%s" % (title, message)}}).encode()
            req = urllib.request.Request(hook, method="POST", data=body)
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            write_alert("[webhook 发送失败] %s" % e)
