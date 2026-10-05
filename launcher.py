#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Buddy 工作台启动器。

职责：
  1. 找一个可用的 Python 解释器（优先 config.json/python_path，再 known paths，最后 where）。
  2. 清理 8765-8775 范围内的旧 server 进程。
  3. 确保 WorkBuddy 客户端带 CDP 调试端口 9222 运行（没有就重启）。
  4. 在本机起一个可用端口（从 8765 开始顺延），启动 web/server。
  5. 浏览器打开 server 实际监听的端口。

返回值：0 成功；1 缺少 Python / 客户端起不来 / server 起不来
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "launcher.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def find_python():
    cfg = load_config()
    cfg_py = cfg.get("python_path")
    if cfg_py and os.path.exists(cfg_py) and os.path.isfile(cfg_py):
        return cfg_py
    known = [
        r"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe",
        r"D:\empire_tools\green_tools\python\python.exe",
    ]
    for p in known:
        if os.path.exists(p):
            return p
    try:
        out = subprocess.run(["where", "python"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            p = line.strip()
            if not p or "WindowsApps" in p:
                continue
            try:
                r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and "Python" in r.stdout:
                    return p
            except Exception:
                continue
    except Exception:
        pass
    return None


def run_py(py, script, *args, wait=True, timeout=None, **popen_kw):
    cmd = [py, os.path.join(BASE_DIR, script)] + list(args)
    log("RUN: %s" % " ".join(cmd))
    if wait:
        return subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True,
                              timeout=timeout, **popen_kw)
    return subprocess.Popen(cmd, cwd=BASE_DIR, **popen_kw)


def _proc_cmdline(pid):
    """拿进程命令行（用于兜底识别：是不是我们自己的 web server）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").CommandLine" % pid],
            capture_output=True, text=True, timeout=8)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def port_clean():
    """清掉 8765-8775 范围内的旧 server。

    2026-09-23 修：旧 server 忙时 /api/state 可能 1s 内不回，原来的「1 秒单次探测」
    会误判"不是我们的 server"而不杀 → 于是又起第二个（端口漂移到 8766，用户看到两个 server）。
    现在：① /api/state 探测加长到 4s 并重试 2 次；② 兜底按"命令行含 server.py"识别后杀。
    """
    killed = []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True).stdout
    except Exception as e:
        log("  [warn] netstat 失败：%s" % e)
        return killed
    me = os.getpid()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        try:
            port = int(parts[1].rsplit(":", 1)[1])
            pid = int(parts[4])
        except (ValueError, IndexError):
            continue
        if not (8765 <= port <= 8775) or pid == me:
            continue
        mine = False
        for _ in range(2):
            try:
                r = urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=4)
                data = json.loads(r.read().decode("utf-8", "replace"))
                if isinstance(data, dict) and ("accounts" in data or "totals" in data):
                    mine = True
                    break
            except Exception:
                time.sleep(1.0)
        if not mine:
            cl = _proc_cmdline(pid)
            if "server.py" in cl and ("web" in cl.lower() or "buddy" in cl.lower()):
                mine = True
                log("  按命令行识别到旧 server（/api/state 未响应）：pid %d" % pid)
        if mine:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
            killed.append((port, pid))
            log("  清掉旧 server：端口 %d (pid %d)" % (port, pid))
    return killed


def cdp_ready(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2)
            if r.status == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def free_port(preferred=8765, count=20):
    for p in range(preferred, preferred + count):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def server_ready(port, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=2)
            if r.status == 200:
                data = json.loads(r.read().decode("utf-8", "replace"))
                if isinstance(data, dict) and ("accounts" in data or "totals" in data):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def open_browser(url):
    import webbrowser
    try:
        # 加时间戳参数，避免浏览器缓存旧页面
        sep = '&' if '?' in url else '?'
        url = url + sep + 't=' + str(int(time.time()))
        webbrowser.open(url)
        log("浏览器已打开：%s" % url)
        return True
    except Exception as e:
        log("打开浏览器失败：%s" % e)
        return False


def main():
    log("=" * 50)
    log("Buddy 工作台启动器")
    log("=" * 50)

    py = find_python()
    if not py:
        log("[ERROR] 找不到可用的 Python 解释器")
        return 1
    log("Python: %s" % py)

    log("[1/4] 清理旧 web server...")
    port_clean()

    log("[2/4] 检查 WorkBuddy 客户端 CDP...")
    if not cdp_ready(timeout=3):
        log("客户端未就绪，尝试重启（带 CDP 调试端口）...")
        run_py(py, "client_cdp.py", "--restart", "--wait", "120",
               wait=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not cdp_ready(timeout=90):
            log("[ERROR] 客户端未能启动或 CDP 端口 9222 未就绪")
            log("请手动双击 restart-cdp.cmd 后再试。")
            return 1
    log("CDP 就绪 ✓")

    log("[3/4] 启动本地管理工作台...")
    port = free_port(8765)
    if not port:
        log("[ERROR] 找不到可用端口（8765-8784 都被占用）")
        return 1
    log("使用端口：%d" % port)
    # 2026-09-25: 把 server.py 的输出写到日志文件里，方便调试
    server_log = os.path.join(LOG_DIR, "server.log")
    server_f = open(server_log, "wb")
    # 2026-09-25: 加 -u 参数（unbuffered），否则输出被缓冲，看不到日志
    cmd = [py, "-u", os.path.join(BASE_DIR, "web", "server.py"), str(port), "--no-open"]
    log("RUN: %s" % " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=server_f, stderr=subprocess.STDOUT)

    if not server_ready(port, timeout=120):  # 2026-09-25: 从60秒改成120秒，启动初始化要很久
        log("[ERROR] server 未能在 120 秒内就绪")
        return 1
    url = "http://127.0.0.1:%d/" % port
    log("管理工作台就绪：%s" % url)

    log("[4/4] 打开浏览器...")
    open_browser(url)

    log("=" * 50)
    log("启动完成。请保持本窗口运行，关闭窗口即停止服务。")
    log("=" * 50)

    try:
        # 2026-09-25: stdout 已重定向到 server.log 文件，这里直接等待进程结束
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        # 关闭 server.log 文件
        try:
            server_f.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        log("[FATAL] 启动器发生未捕获异常：")
        tb = traceback.format_exc()
        for line in tb.rstrip().splitlines():
            log("  " + line)
        sys.exit(1)
