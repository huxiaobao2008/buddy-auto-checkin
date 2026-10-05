#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 CDP 客户端（零第三方依赖，标准库实现 WebSocket + Chrome DevTools Protocol）。

连上 WorkBuddy 客户端自带的调试端口（环境变量 WORKBUDDY_REMOTE_DEBUGGING_PORT 开启，
见 client_cdp 说明），在渲染进程里执行 JS / 读界面状态 / 驱动 UI。

关键点（全部实战教训）：
  - 每次操作新建连接 + Emulation.setFocusEmulationEnabled(True)：
    窗口被遮挡/锁屏时页面被判 hidden，定时器节流 ~19 倍、虚拟列表渲染 0 项，
    焦点模拟按连接生效，所以必须在 connect() 里每次设置。
  - 本机代理会把 127.0.0.1 也走代理，必须显式绕过。
  - evaluate 带 userGesture=True，返回 returnByValue。
"""
import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.request
from urllib.parse import urlparse

from . import config as C

PORT = C.CDP_PORT
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class CdpError(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=30.0):
        u = urlparse(url)
        if u.scheme != "ws":
            raise CdpError("只支持 ws:// 地址，收到 %s" % url)
        self.host = u.hostname
        self.port = u.port or 80
        self.path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.timeout = timeout
        self._buf = b""
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ) % (self.path, self.host, self.port, key)
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("握手时连接被关闭")
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        self._buf = rest
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if not status.startswith("HTTP/1.1 101"):
            raise CdpError("握手失败：%s" % status)
        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode().lower()
        got = ""
        for line in head.decode("latin-1").split("\r\n"):
            if line.lower().startswith("sec-websocket-accept:"):
                got = line.split(":", 1)[1].strip().lower()
        if got and got != expect:
            raise CdpError("Sec-WebSocket-Accept 校验失败")

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise CdpError("连接被对端关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        b0, b1 = self._read(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if masked else None
        payload = self._read(ln) if ln else b""
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return bool(fin), opcode, payload

    def recv_text(self):
        chunks = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise CdpError("收到关闭帧")
            if opcode in (OP_TEXT, OP_BIN):
                chunks = [payload]
            elif opcode == 0x0:
                chunks.append(payload)
            else:
                continue
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def _send_frame(self, opcode, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, s):
        self._send_frame(OP_TEXT, s)

    def close(self):
        try:
            self._send_frame(OP_CLOSE, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    def __init__(self, ws_url, timeout=30.0):
        self.ws = WebSocket(ws_url, timeout=timeout)
        self._id = 0
        self._events = []
        self.handlers = {}

    def call(self, method, params=None, timeout=30.0):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        self.ws.send_text(json.dumps(msg))
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise CdpError("%s 超时（%.0fs）" % (method, timeout))
            self.ws.sock.settimeout(max(0.5, remain))
            raw = self.ws.recv_text()
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("id") == mid:
                if "error" in obj:
                    raise CdpError("%s 返回错误：%s" % (method, obj["error"]))
                return obj.get("result") or {}
            if "method" in obj:
                h = self.handlers.get(obj["method"])
                if h:
                    try:
                        h(obj.get("params") or {})
                    except Exception:
                        pass
                else:
                    self._events.append(obj)

    def evaluate(self, expr, await_promise=True, return_by_value=True, timeout=30.0):
        r = self.call("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": await_promise,
            "returnByValue": return_by_value,
            "userGesture": True,
        }, timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            desc = (d.get("exception") or {}).get("description") or d.get("text")
            raise CdpError("JS 抛错：%s" % desc)
        return (r.get("result") or {}).get("value")

    def close(self):
        self.ws.close()


def list_pages():
    # 修复：原先不关闭响应体，长时间运行会累积占用连接（Windows 下更明显）。
    r = _OPENER.open("http://127.0.0.1:%d/json/list" % PORT, timeout=5)
    try:
        return json.loads(r.read().decode("utf-8", "replace"))
    finally:
        try:
            r.close()
        except Exception:
            pass


def find_workbuddy_page(prefer_substr=None):
    """挑主窗口。

    坑（2026-09-22 实测踩中）：「设置」是在**第二个渲染进程窗口**打开的，
    URL 带 windowAppId=sett…。list_pages() 里它常常排在前面，而主窗口 URL 才带
    accountSnapshot。取到设置窗口会导致所有主窗口操作（读昵称、点侧边栏…）静默失败，
    症状是「切过去后什么都没做」。所以这里先按 accountSnapshot 认主窗口，
    明确排除设置子窗口。
    """
    pages = [p for p in list_pages() if p.get("type") == "page"]
    if not pages:
        raise CdpError("端口 %d 上没有可驱动的 page（客户端是否带调试端口启动？）" % PORT)

    def _main(p):
        u = p.get("url") or ""
        return ("accountSnapshot" in u) and ("windowAppId=sett" not in u)

    for p in pages:
        if _main(p):
            return p
    if prefer_substr:
        for p in pages:
            u = p.get("url") or ""
            if prefer_substr in u and "windowAppId=sett" not in u:
                return p
    return pages[0]


def unhide(cdp):
    """焦点模拟：窗口被遮挡/锁屏时页面被判 hidden（定时器节流、虚拟列表不渲染）。

    按 CDP 连接生效，所以必须在每次 connect() 里设置。
    """
    try:
        cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
    except Exception:
        pass
    return cdp


def connect(prefer_substr="app.asar/renderer"):
    p = find_workbuddy_page(prefer_substr)
    ws = p.get("webSocketDebuggerUrl")
    if not ws:
        raise CdpError("该页面没有 webSocketDebuggerUrl")
    return unhide(CDP(ws)), p


def find_target(url_substr=None, type_=None):
    """按 URL 子串 / 目标类型找调试目标（跨域 iframe 是独立 "iframe" 类型 OOPIF）。"""
    for p in list_pages():
        if type_ and p.get("type") != type_:
            continue
        if url_substr and url_substr not in (p.get("url") or ""):
            continue
        if p.get("webSocketDebuggerUrl"):
            return p
    return None


def connect_target(url_substr=None, type_=None):
    p = find_target(url_substr, type_)
    if not p:
        raise CdpError("找不到调试目标 url~%r type=%r" % (url_substr, type_))
    return unhide(CDP(p["webSocketDebuggerUrl"])), p


def screenshot(cdp, path, full=False):
    params = {"format": "png"}
    if full:
        params["captureBeyondViewport"] = True
    r = cdp.call("Page.captureScreenshot", params, timeout=30)
    data = base64.b64decode(r.get("data") or "")
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)
