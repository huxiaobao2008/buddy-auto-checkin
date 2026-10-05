#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 客户端 CDP 调试端口管理。

原理（已在客户端 app.asar 内核实，非猜测）：
    WorkBuddy 主进程读取环境变量 WORKBUDDY_REMOTE_DEBUGGING_PORT，
    当它是合法数字端口时，自动给 Electron 追加 --remote-debugging-port=<port>。
    官方自带外部自动化入口，不需任何第三方插件。

用法：
    python client_cdp.py --check            # 探测 9222 是否已就绪
    python client_cdp.py --restart --detach # 派出独立进程重启（推荐，避免自杀）
    python client_cdp.py --restart          # 就地重启（会杀掉自己所在进程树，慎用）

注意：--restart 会先结束当前 WorkBuddy 进程，正在执行的会话会被中断。
从 WorkBuddy 会话内部无法可靠完成重启（子进程处于 kill-on-close 的 Job 中），
正确做法是由 WorkBuddy 之外的进程执行（双击 restart-cdp.cmd）。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

EXE = r"D:\Program Files\WorkBuddy\WorkBuddy.exe"
INSTALL_DIR = os.path.dirname(EXE)
PORT = 9222
ENV_NAME = "WORKBUDDY_REMOTE_DEBUGGING_PORT"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
CDP_LOG = os.path.join(LOG_DIR, "client_cdp.log")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(CDP_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _get(path, timeout=1.5):
    try:
        r = _OPENER.open("http://127.0.0.1:%d%s" % (PORT, path), timeout=timeout)
        # 修复：原先不关闭响应体，反复探测会累积占用连接。
        try:
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            try:
                r.close()
            except Exception:
                pass
    except Exception:
        return None, None


def probe():
    code, body = _get("/json/version")
    if code != 200 or not body:
        return False, None, []
    try:
        ver = json.loads(body)
    except Exception:
        ver = {"raw": body[:200]}
    _, lb = _get("/json/list")
    targets = []
    if lb:
        try:
            targets = json.loads(lb)
        except Exception:
            targets = []
    return True, ver, targets


def cmd_check(as_json=False):
    ok, ver, targets = probe()
    if not ok:
        if as_json:
            print(json.dumps({"ready": False, "port": PORT, "env": ENV_NAME}, ensure_ascii=False))
        else:
            log("CDP 未就绪：127.0.0.1:%d 无响应" % PORT)
            log("→ 需要带环境变量 %s=%d 重启客户端（python client_cdp.py --restart）" % (ENV_NAME, PORT))
        return 1
    pages = [t for t in targets if t.get("type") == "page"]
    if as_json:
        print(json.dumps({
            "ready": True, "port": PORT,
            "browser": ver.get("Browser") if isinstance(ver, dict) else None,
            "target_count": len(targets),
            "pages": [{"title": p.get("title"), "url": (p.get("url") or "")[:160],
                       "ws": p.get("webSocketDebuggerUrl")} for p in pages],
        }, ensure_ascii=False, indent=2))
    else:
        log("CDP 就绪 ✓  %s" % (ver.get("Browser") if isinstance(ver, dict) else ""))
        log("可驱动页面 %d 个：" % len(pages))
        for i, p in enumerate(pages):
            print("  [%d] %-28s %s" % (i, (p.get("title") or "")[:28], (p.get("url") or "")[:110]))
    return 0


def _taskkill():
    """只按镜像名结束，绝不用 /T（本脚本自身可能就是 WorkBuddy 的子进程）。"""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
    try:
        subprocess.run([exe, "/IM", "WorkBuddy.exe", "/F"],
                       capture_output=True, timeout=30)
    except Exception as e:
        log("taskkill 异常：%s" % e)


def _still_running():
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tasklist.exe")
    try:
        out = subprocess.run([exe, "/FI", "IMAGENAME eq WorkBuddy.exe"],
                             capture_output=True, timeout=20).stdout
        for enc in ("gbk", "utf-8"):
            try:
                if "WorkBuddy.exe" in out.decode(enc):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _launch_detached():
    env = dict(os.environ)
    env[ENV_NAME] = str(PORT)
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
        flags |= getattr(subprocess, name, 0)
    try:
        p = subprocess.Popen([EXE], cwd=INSTALL_DIR, env=env,
                             creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("已启动客户端 pid=%d（带 %s=%d）" % (p.pid, ENV_NAME, PORT))
        return True
    except Exception as e:
        log("启动失败：%s" % e)
        try:
            subprocess.Popen([EXE], cwd=INSTALL_DIR, env=env, close_fds=True,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("已用兜底方式启动客户端")
            return True
        except Exception as e2:
            log("兜底启动也失败：%s" % e2)
            return False


def spawn_detached_restart(delay, wait):
    """派出一个脱离 Job 的独立进程去执行重启，本进程立刻返回。"""
    args = [sys.executable, os.path.abspath(__file__),
            "--restart", "--delay", str(delay), "--wait", str(wait)]
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
        flags |= getattr(subprocess, name, 0)
    try:
        p = subprocess.Popen(args, cwd=BASE_DIR, creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as e:
        log("派出独立重启进程失败：%s" % e)
        return 2
    log("已派出独立重启进程 pid=%d，%d 秒后执行。" % (p.pid, delay))
    log("本会话届时会被中断；重启进度与结果全部写入：%s" % CDP_LOG)
    log("回来后执行 `python client_cdp.py --check` 确认端口就绪。")
    return 0


def cmd_restart(delay=0, wait=90):
    if delay > 0:
        log("延迟 %d 秒后重启……" % delay)
        time.sleep(delay)
    ok, _, _ = probe()
    if ok:
        log("CDP 已在 %d 端口就绪，无需重启。" % PORT)
        return cmd_check()
    log("[1/3] 结束现有 WorkBuddy 进程……")
    _taskkill()
    for _ in range(15):
        if not _still_running():
            break
        time.sleep(1)
    if _still_running():
        log("警告：仍有 WorkBuddy 进程残留，继续尝试启动。")
    log("[2/3] 带调试端口重新启动……")
    if not _launch_detached():
        return 2
    log("[3/3] 等待 CDP 就绪（最多 %d 秒）……" % wait)
    t0 = time.time()
    while time.time() - t0 < wait:
        ok, ver, _ = probe()
        if ok:
            log("CDP 就绪 ✓ 用时 %.1f 秒" % (time.time() - t0))
            return cmd_check()
        time.sleep(2)
    log("超时：CDP 未就绪。请检查是否有安全软件拦截，或手动确认客户端已启动。")
    return 3


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 客户端 CDP 调试端口管理")
    ap.add_argument("--check", action="store_true", help="探测 CDP 是否就绪并列出页面")
    ap.add_argument("--restart", action="store_true", help="关掉客户端并带调试端口重启")
    ap.add_argument("--detach", action="store_true",
                    help="配合 --restart：派出独立进程执行，本进程立即返回（推荐，避免自杀）")
    ap.add_argument("--delay", type=int, default=0, help="重启前延迟秒数")
    ap.add_argument("--wait", type=int, default=90, help="等待 CDP 就绪的最长秒数")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    a = ap.parse_args()
    if a.restart:
        if a.detach:
            sys.exit(spawn_detached_restart(a.delay, a.wait))
        sys.exit(cmd_restart(a.delay, a.wait))
    if a.check:
        sys.exit(cmd_check(a.json))
    sys.exit(cmd_check(a.json))


if __name__ == "__main__":
    main()
