#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lighthouse 运维连接器授权 —— 全自动链路（2026-09-23 实测走通）。

真实链路（逐步，动作 → 确认 → 留证）：
  1) 关掉 Edge（这样 shell.openExternal 会把 URL 写进新 Edge 进程命令行）
  2) 客户端点连接器 chip（CDP 真鼠标，9222）
  3) 从 msedge 进程命令行抢 OAuth URL（--single-argument <url>）
  4) 自控 Edge 打开：msedge --user-data-dir=<临时> --remote-debugging-port=9223 <url>
  5) 登录页「微信登录」在 iframe(open.weixin.qq.com) 里 → 进 iframe 点「微信快捷登录」
  6) 微信 PC 弹「腾讯云 申请使用…」确认框 → 窗口级点绿色「允许」
  7) 回 cloud.tencent.com/open/authorize 点蓝色「授权」
  8) app 收到 workbuddy:// 回调 → chip 变已连接（不带 --idle）

  python engine/connector_oauth.py            # 走完整授权（含复检 chip）
  python engine/connector_oauth.py --watch    # 只到「URL 抓到/自控Edge打开」为止
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import cdp as C   # noqa: E402

CLIENT_PORT = 9222
EDGE_PORT = 9223
TEMP = os.environ.get("TEMP", r"C:\Temp")
PROFILE = os.path.join(TEMP, "wb_conn_ctl")   # 固定 profile：腾讯云认过一次就不再要安全验证
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"

# ---------------------------------------------------------------- 客户端：定位 chip
LOCATE_CONNECTOR = """
(() => {
  const T = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 12 && r.height > 12; };
  const chip = Array.from(document.querySelectorAll('button.wb-connector-dep-chip')).find(vis);
  const conn = Array.from(document.querySelectorAll('button')).filter(vis).find(b => T(b) === '连接');
  const target = chip || conn;
  if (!target) return { ok: false, evidence: { err: '没有连接器 chip' } };
  const r = target.getBoundingClientRect();
  return { ok: true, evidence: { name: T(target).slice(0, 24), idle: String(target.className).indexOf('--idle') !== -1,
                                 x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) } };
})()
"""

CHIP_STATE = """
(() => {
  const c = document.querySelector('button.wb-connector-dep-chip');
  if (!c) return { chip: false, connected: null };
  return { chip: true, connected: String(c.className).indexOf('--idle') === -1 };
})()
"""

# 通用：在某页面找按钮（叶节点文本匹配），返回中心坐标
FIND_LEAF = """
(() => {
  const T = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 6 && r.height > 6; };
  const words = __WORDS__;
  const btns = Array.from(document.querySelectorAll('button')).filter(vis);
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
      .filter(e => vis(e) && e.children.length === 0);
  let hit = null, how = null;
  for (const w of words) { hit = btns.find(e => T(e) === w); if (hit) { how = 'btn:' + w; break; } }
  if (!hit) { for (const w of words) { hit = els.find(e => T(e) === w); if (hit) { how = 'exact:' + w; break; } } }
  if (!hit) { for (const w of words) { hit = els.find(e => T(e).indexOf(w) !== -1 && T(e).length <= 20); if (hit) { how = 'contains:' + w; break; } } }
  if (!hit) return { ok: false, have: els.map(e => T(e).slice(0, 16)).filter(Boolean).slice(0, 20) };
  const r = hit.getBoundingClientRect();
  return { ok: true, t: T(hit).slice(0, 20), how: how, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
})()
"""


def _mouse(cdp, x, y):
    for typ in ("mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent",
                 {"type": typ, "x": int(x), "y": int(y), "button": "left",
                  "buttons": 1 if typ == "mousePressed" else 0, "clickCount": 1}, timeout=15)
        time.sleep(0.12)


def _page(ws):
    c = C.CDP(ws)
    try:
        c.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=8)
    except Exception:
        pass
    return c


def _list(port):
    od = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with od.open("http://127.0.0.1:%d/json/list" % port, timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception:
        return []


def _client_main():
    C.PORT = CLIENT_PORT
    return C.find_workbuddy_page()


# ---------------------------------------------------------------- Win32：微信确认框
u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32
k32 = ctypes.windll.kernel32
u32.SetProcessDPIAware()
u32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
u32.SetCursorPos.argtypes = [ctypes.c_long, ctypes.c_long]
u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
u32.GetForegroundWindow.restype = ctypes.c_void_p
u32.GetDC.argtypes = [ctypes.c_void_p]
u32.GetDC.restype = ctypes.c_void_p
g32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
g32.CreateCompatibleDC.restype = ctypes.c_void_p
g32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
g32.CreateCompatibleBitmap.restype = ctypes.c_void_p
g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
g32.SelectObject.restype = ctypes.c_void_p
g32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
g32.DeleteObject.argtypes = [ctypes.c_void_p]
g32.DeleteDC.argtypes = [ctypes.c_void_p]
u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
g32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
_ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
_GR = (7, 193, 96)


def _wins():
    out = []

    def cb(h, _):
        n = u32.GetWindowTextLengthW(h)
        if not n or not u32.IsWindowVisible(h):
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(h, b, n + 1)
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        r = wt.RECT()
        u32.GetWindowRect(h, ctypes.byref(r))
        out.append({"h": h, "t": b.value, "pid": pid.value,
                    "w": r.right - r.left, "h2": r.bottom - r.top, "left": r.left, "top": r.top})
        return True

    u32.EnumWindows(_ENUM(cb), None)
    return out


def _weixin_pids():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                           capture_output=True)
        txt = r.stdout.decode("gbk", "ignore")
        pids = set()
        for x in txt.splitlines():
            parts = [p.strip('"') for p in x.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(int(parts[1]))
        return pids
    except Exception:
        return set()


def _capture(left, top, w, h):
    sdc = u32.GetDC(None)
    mdc = g32.CreateCompatibleDC(sdc)
    bmp = g32.CreateCompatibleBitmap(sdc, w, h)
    g32.SelectObject(mdc, bmp)
    g32.BitBlt(mdc, 0, 0, w, h, sdc, left, top, 0x00CC0020)
    bmi = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    bb = ctypes.create_string_buffer(bmi, len(bmi))
    got = g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bb), 0)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mdc)
    u32.ReleaseDC(None, sdc)
    return buf.raw if got == h else None


def _green(raw, w, h):
    rows = {}
    for y in range(0, h, 2):
        base = y * w * 4
        xs = []
        for x in range(0, w, 2):
            o = base + x * 4
            b, g, r = raw[o], raw[o + 1], raw[o + 2]
            if abs(r - _GR[0]) < 45 and abs(g - _GR[1]) < 45 and abs(b - _GR[2]) < 45:
                xs.append(x)
        if xs:
            rows[y] = xs
    if not rows:
        return None
    ys = sorted(rows)
    clusters, cur = [], [ys[0]]
    for a, b in zip(ys, ys[1:]):
        (cur.append(b) if b - a <= 20 else (clusters.append(cur), cur.clear(), cur.append(b)))
    clusters.append(cur)
    best = None
    for cl in clusters:
        xs_all = [x for y in cl for x in rows[y]]
        if not xs_all:
            continue
        minx, maxx, miny, maxy = min(xs_all), max(xs_all), cl[0], cl[-1]
        if len(xs_all) < 80 or (maxx - minx) < 50 or (maxy - miny) < 12:
            continue
        if best is None or len(xs_all) > best[2]:
            best = ((minx + maxx) // 2, (miny + maxy) // 2, len(xs_all))
    return best


def _fg(h):
    u32.ShowWindow(h, 9)
    time.sleep(0.2)
    for _ in range(8):
        if u32.GetForegroundWindow() == h:
            return True
        fp = wt.DWORD()
        ft = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(fp))
        me = k32.GetCurrentThreadId()
        if ft and ft != me:
            u32.AttachThreadInput(me, ft, True)
            u32.BringWindowToTop(h)
            u32.SetForegroundWindow(h)
            u32.AttachThreadInput(me, ft, False)
        else:
            u32.SetForegroundWindow(h)
        time.sleep(0.3)
    return u32.GetForegroundWindow() == h


def _win_click_wechat_allow(timeout=20, log=print):
    """找微信(Weixin.exe)的确认窗口，点它的绿色「允许」。"""
    wx = _weixin_pids()
    t0 = time.time()
    while time.time() - t0 < timeout:
        cands = [w for w in _wins() if w["w"] > 250 and w["h2"] > 150 and ((w["pid"] in wx) or (w["t"] == "微信"))]
        for w in cands:
            if not _fg(w["h"]):
                continue
            raw = _capture(w["left"], w["top"], w["w"], w["h2"])
            if not raw:
                continue
            blob = _green(raw, w["w"], w["h2"])
            if blob:
                cx, cy, n = blob
                sx, sy = w["left"] + cx, w["top"] + cy
                u32.SetCursorPos(int(sx), int(sy))
                time.sleep(0.2)
                u32.mouse_event(2, 0, 0, 0, 0)
                time.sleep(0.08)
                u32.mouse_event(4, 0, 0, 0, 0)
                time.sleep(1.0)
                return {"win": w["t"][:24], "green": n, "at": (sx, sy)}
        time.sleep(1.5)
    return None


# ---------------------------------------------------------------- URL 抓取 / 自控 Edge
def _kill_edge():
    subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"], capture_output=True)


def _capture_oauth_url(timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" "
                  "| Select-Object -ExpandProperty CommandLine")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, timeout=8).stdout.decode("utf-8", "ignore")
            for ln in out.splitlines():
                m = re.search(r"--single-argument\s+(\S+)", ln) or re.search(r"(https://\S+)", ln)
                if m and "tencent.com" in m.group(1):
                    return m.group(1).strip()
        except Exception:
            pass
        time.sleep(0.3)
    return None


def _launch_controlled(url):
    os.makedirs(PROFILE, exist_ok=True)
    subprocess.Popen([EDGE, "--remote-debugging-port=%d" % EDGE_PORT, "--user-data-dir=" + PROFILE,
                      "--no-first-run", "--no-default-browser-check", "--window-size=1200,900", url])


def _wait_page(substr, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for p in _list(EDGE_PORT):
            if p.get("type") == "page" and substr in (p.get("url") or ""):
                return p
        time.sleep(1)
    return None


def _find_iframe_target(substr="open.weixin.qq.com"):
    for p in _list(EDGE_PORT):
        if p.get("type") == "iframe" and substr in (p.get("url") or ""):
            return p
    return None


def _iframe_offset(main_ws):
    c = _page(main_ws)
    try:
        return c.evaluate("(()=>{const f=document.querySelector('iframe');if(!f)return null;const r=f.getBoundingClientRect();return {left:Math.round(r.left),top:Math.round(r.top)};})()", timeout=15)
    finally:
        c.close()


def _find_leaf(cdp, words):
    js = FIND_LEAF.replace("__WORDS__", json.dumps(words, ensure_ascii=False))
    return cdp.evaluate(js, timeout=20)


# 穿透 shadow DOM 找按钮（Edge 自己的弹窗是 WebUI + shadow DOM，普通选择器看不见）
DEEP_FIND = """
(() => {
  const all = (root, out) => { for (const e of root.querySelectorAll('*')) { out.push(e); if (e.shadowRoot) all(e.shadowRoot, out); } return out; };
  const words = __WORDS__;
  const T = e => (e.innerText || e.textContent || '').trim();
  const els = all(document, []);
  let hit = null, how = null;
  for (const w of words) { hit = els.find(e => T(e) === w && e.getBoundingClientRect().width > 4); if (hit) { how = 'exact:' + w; break; } }
  if (!hit) { for (const w of words) { hit = els.find(e => T(e).indexOf(w) !== -1 && T(e).length <= 16 && e.getBoundingClientRect().width > 4); if (hit) { how = 'contains:' + w; break; } } }
  if (!hit) return { ok: false };
  const r = hit.getBoundingClientRect();
  return { ok: true, t: T(hit).slice(0, 14), how: how, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
})()
"""


def _find_deep(cdp, words):
    return cdp.evaluate(DEEP_FIND.replace("__WORDS__", json.dumps(words, ensure_ascii=False)), timeout=15)


def _dismiss_edge_dialogs():
    """点掉 Edge 自己的对话框（权限询问 / 同步提示）——WebUI+shadow DOM，必须穿透找按钮。"""
    acted = []
    for p in _list(EDGE_PORT):
        u = p.get("url") or ""
        if p.get("type") != "page" or not u.startswith("edge://"):
            continue
        try:
            c = _page(p["webSocketDebuggerUrl"])
            r = _find_deep(c, ["允许", "明白", "继续", "确定"])
            if r and r.get("ok"):
                _mouse(c, r["x"], r["y"])
                acted.append({"url": u[:46], "btn": r.get("t")})
            c.close()
        except Exception:
            pass
    return acted


# ---------------------------------------------------------------- 主链路
def authorize(watch=False, timeout=180, log=print):
    out = {"ok": False, "steps": []}

    def say(tag, ev, ok=None):
        out["steps"].append({"step": tag, "ok": ok, "evidence": ev})
        if log:
            log("[%s] %s" % (tag, json.dumps(ev, ensure_ascii=False)[:400]))

    # S0 复检
    m = _client_main()
    c = _page(m["webSocketDebuggerUrl"])
    try:
        st0 = c.evaluate(CHIP_STATE, timeout=20)
        say("S0_复检", st0, ok=True)
        if st0.get("connected"):
            out["ok"] = True
            out["note"] = "连接器已连接"
            return out
    finally:
        c.close()

    # S1 关 Edge + 点连接器
    _kill_edge()
    time.sleep(2)
    c = _page(m["webSocketDebuggerUrl"])
    try:
        r1 = c.evaluate(LOCATE_CONNECTOR, timeout=30)
        say("S1_定位连接器", r1.get("evidence") or r1, ok=bool(r1.get("ok")))
        if not r1.get("ok"):
            out["err"] = "点不到连接器"
            return out
        _mouse(c, r1["evidence"]["x"], r1["evidence"]["y"])
        time.sleep(2.5)
    finally:
        c.close()

    # S2 抢 OAuth URL
    url = _capture_oauth_url(timeout=25)
    say("S2_抓URL", {"url": (url or "")[:120]}, ok=bool(url))
    if not url:
        out["err"] = "点连接器后没抢到 OAuth URL"
        return out
    if watch:
        out["note"] = "已到 URL，watch 停"
        return out

    # S3 自控 Edge 打开
    _launch_controlled(url)
    say("S3_自控Edge", {"profile": PROFILE, "port": EDGE_PORT}, ok=True)
    main = _wait_page("cloud.tencent.com", timeout=40)
    say("S4_登录页", {"url": (main or {}).get("url", "")[:110]}, ok=bool(main))
    if not main:
        out["err"] = "自控 Edge 没打开腾讯云登录页"
        return out
    time.sleep(3)
    dl = _dismiss_edge_dialogs()
    say("S4b_Edge对话框", {"acted": dl}, ok=True)
    time.sleep(1)

    # S5 先点「微信登录」标签，再进 iframe 点「微信快捷登录」
    mc0 = _page(main["webSocketDebuggerUrl"])
    try:
        rt = _find_leaf(mc0, ["微信登录"])
        if rt and rt.get("ok"):
            _mouse(mc0, rt["x"], rt["y"])
            say("S5a_点微信登录标签", {"how": rt.get("how")}, ok=True)
    finally:
        mc0.close()
    time.sleep(3)
    done = False
    for _ in range(12):
        _dismiss_edge_dialogs()
        ifr = _find_iframe_target()
        if not ifr:
            time.sleep(3)
            continue
        ic = _page(ifr["webSocketDebuggerUrl"])
        try:
            r = _find_leaf(ic, ["微信快捷登录", "快捷登录", "微信登录", "微信"])
        finally:
            ic.close()
        if r and r.get("ok"):
            off = _iframe_offset(main["webSocketDebuggerUrl"]) or {"left": 0, "top": 0}
            mc = _page(main["webSocketDebuggerUrl"])
            try:
                _mouse(mc, off["left"] + r["x"], off["top"] + r["y"])
            finally:
                mc.close()
            say("S5_点微信快捷登录", {"btn": r.get("t"), "how": r.get("how")}, ok=True)
            done = True
            break
        time.sleep(2)
    if not done:
        say("S5_点微信快捷登录", {"ok": False, "note": "iframe 里没找到微信快捷登录"}, ok=False)

    # S6 微信 PC 确认框 → 点允许
    time.sleep(2)
    wc = _win_click_wechat_allow(timeout=25, log=log)
    say("S6_微信点允许", wc or {"ok": False}, ok=bool(wc))
    time.sleep(4)

    # S7 浏览器点「授权」
    authorized = False
    sel_done = False
    for _ in range(5):
        main = _wait_page("cloud.tencent.com", timeout=6) or main
        mc = _page(main["webSocketDebuggerUrl"])
        try:
            if not sel_done:
                rs = _find_deep(mc, ["全选"])
                if rs and rs.get("ok"):
                    _mouse(mc, rs["x"], rs["y"])
                    sel_done = True
                    say("S7a_全选", {"how": rs.get("how")}, ok=True)
                    time.sleep(1)
            r = mc.evaluate("(()=>{const a=document.querySelector('a.J-btnAuthorize')||Array.from(document.querySelectorAll('a,button')).find(x=>/授权/.test(x.innerText||'')&&x.getBoundingClientRect().width>80);if(!a)return {ok:false};const b=a.getBoundingClientRect();return {ok:true,t:(a.innerText||'').trim().slice(0,10),how:'btn:J-btnAuthorize',x:Math.round(b.left+b.width/2),y:Math.round(b.top+b.height/2)};})()", timeout=15)
            if r and r.get("ok"):
                _mouse(mc, r["x"], r["y"])
                authorized = True
                say("S7_点授权", {"btn": r.get("t"), "how": r.get("how")}, ok=True)
                break
        finally:
            mc.close()
        time.sleep(3)
    if not authorized:
        say("S7_点授权", {"ok": False}, ok=False)

    # S8 复检 chip
    time.sleep(6)
    m = _client_main()
    c = _page(m["webSocketDebuggerUrl"])
    try:
        st = c.evaluate(CHIP_STATE, timeout=20)
    finally:
        c.close()
    say("S8_复检chip", st, ok=bool(st.get("connected")))
    out["ok"] = bool(st.get("connected"))
    if not out["ok"]:
        out["err"] = "走完链路但 chip 仍未连接"
    return out


def main():
    watch = "--watch" in sys.argv
    r = authorize(watch=watch)
    print(json.dumps({k: v for k, v in r.items() if k != "steps"}, ensure_ascii=False)[:600])
    for s in r.get("steps", []):
        print("  -", s["step"], "ok=%s" % s["ok"])
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
