#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 原生托盘气泡提醒（纯 ctypes 调用 Win32 API，不依赖 PowerShell、不依赖第三方库）。

用法：pythonw toast.py "标题" "内容"
独立进程运行，不阻塞主程序；显示约 12 秒后自动消失。
"""
import ctypes
import sys
import time
from ctypes import wintypes

NIF_INFO = 0x00000010
NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIIF_WARNING = 0x00000002
WM_USER = 0x0400


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON)]


def show(title, message, seconds=12):
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = None
    nid.uID = 1
    nid.uFlags = NIF_INFO
    nid.uCallbackMessage = WM_USER
    nid.hIcon = user32.LoadIconW(None, 32516)  # IDI_WARNING
    nid.szTip = "buddy-auto"
    nid.szInfo = message[:255]
    nid.szInfoTitle = title[:63]
    nid.dwInfoFlags = NIIF_WARNING
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    time.sleep(seconds)
    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else "buddy-auto"
    m = sys.argv[2] if len(sys.argv) > 2 else ""
    show(t, m)
