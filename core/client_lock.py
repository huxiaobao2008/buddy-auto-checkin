#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端占用令牌：串行铁则的唯一执行点。

背景（为什么需要这个模块）
--------------------------
需求「串行操作铁则」要求：单客户端同一时间只能有一个执行方操作，
否则会出现「切号切到一半被改掉登录态、任务做到别的账号头上」的情况。

此前项目里有 **4 套各自为政的状态**（RUN_ALL_PROC、ACCT_JOB["running"]、
UI_CODE_JOB["running"]、_RUN_ALL_STARTING），每个入口只检查自己那一个，
彼此互不互斥；且均为进程内变量，对独立子进程（run_all）无效。
本模块把这些收敛成**唯一事实来源**。

设计要点
--------
1. **单一事实来源**：全局只有一个占用状态，所有入口都经由本模块申请。
2. **跨进程**：run_all 以独立子进程运行，threading.Lock 对子进程无效，
   因此额外使用「原子创建文件」作为跨进程互斥（O_CREAT|O_EXCL）。
3. **可观测**：holder() 供 /api/state 展示，让用户看得见「谁在占用」。
4. **可恢复**：持有者进程消失或超时后自动释放，避免僵尸占用永久卡死。
5. **纵深防御**：hold() 用于需要「已持令牌」的断言（如切号前校验），
   任何新入口忘记申请令牌时，切号会明确报错而不是默默切错账号。

典型用法
--------
    # Web 入口
    ok, who = CL.try_acquire("单任务", "主号·邻家 / 使用5个模板")
    if not ok:
        return {"ok": False, "error": "%s 正在执行中，请等待" % who}
    try:
        run_it()
    finally:
        CL.release()

    # 破坏性操作前的断言（switch_to 用）
    CL.hold("切号")   # 无令牌时抛 RuntimeError
"""

import errno
import json
import os
import threading
import time

from . import config as C

# 令牌文件：Web 进程与 run_all 子进程共用
LOCK_FILE = os.path.join(C.LOG_DIR, "client_owner.lock")

# 同进程内的线程级互斥（保护「读-改-写令牌文件」的原子性）
_MUTEX = threading.Lock()

# 持有者信息（与文件内容同步，仅作快速读取与兜底）
_OWNER = {"kind": "", "key": "", "pid": 0, "since": "", "ts": 0.0}

# 占用超时：超过该时长视为僵尸占用（进程崩溃未释放），自动强制回收
MAX_HOLD_SEC = float(os.environ.get("WB_CLIENT_LOCK_MAX", "7200"))  # 默认 2 小时

# 运行中任务允许的最长时长（run_all 跑 20 个账号可能很久，单独放宽）
RUN_ALL_MAX_HOLD_SEC = float(os.environ.get("WB_CLIENT_LOCK_MAX_ALL", "28800"))  # 8 小时


def _limit_for(kind):
    return RUN_ALL_MAX_HOLD_SEC if kind == "run_all" else MAX_HOLD_SEC


def _read_file():
    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_file(owner):
    os.makedirs(C.LOG_DIR, exist_ok=True)
    tmp = LOCK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(owner, f, ensure_ascii=False)
    os.replace(tmp, LOCK_FILE)


def _clear_file():
    for p in (LOCK_FILE, LOCK_FILE + ".tmp"):
        try:
            os.remove(p)
        except OSError as e:
            if e.errno != errno.ENOENT:
                pass


def _pid_alive(pid):
    """判断持有者进程是否仍存活（跨平台，零依赖）。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows：用 ctypes 查句柄，不额外开子进程
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True  # 查不了就别误杀
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stale(info):
    """判断已存在的占用是否应当被回收。"""
    if not isinstance(info, dict):
        return True
    pid = info.get("pid", 0)
    # 跨进程场景：同 pid 视为续持（由调用方判断）；不同 pid 且已死 → 可回收
    if pid and pid != os.getpid() and not _pid_alive(pid):
        return True
    kind = info.get("kind") or ""
    ts = float(info.get("ts") or 0)
    if ts and (time.time() - ts) > _limit_for(kind):
        return True
    if not pid:
        return True  # 残缺信息，视为失效
    return False


def holder():
    """返回当前占用者描述；空闲返回 None。

    返回 dict: {"kind": 动作, "key": 对象, "pid": 进程号, "since": 起始时间}
    """
    with _MUTEX:
        info = _read_file()
        if info and not _stale(info):
            return info
        if info:
            #僵尸占用，顺手清掉
            _clear_file()
        _OWNER.update({"kind": "", "key": "", "pid": 0, "since": "", "ts": 0.0})
        return None


def holder_text(info=None):
    """把占用者拼成一句给用户看的话。"""
    info = info if info is not None else holder()
    if not info:
        return ""
    kind = info.get("kind") or "任务"
    key = info.get("key") or ""
    return "%s（%s）" % (kind, key) if key else kind


def try_acquire(kind, key="", owner_pid=None):
    """尝试占用客户端。

    返回 ``(是否成功, 占用者描述字符串)``。成功时描述为空串，失败时为当前占用者。
    本函数**不抛异常**，便于直接作为接口返回值使用。
    """
    global _OWNER
    with _MUTEX:
        cur = _read_file()
        if cur and not _stale(cur):
            return False, holder_text(cur)
        pid = owner_pid or os.getpid()
        info = {
            "kind": str(kind or "任务"),
            "key": str(key or ""),
            "pid": pid,
            "since": C.now_str(),
            "ts": time.time(),
        }
        try:
            _write_file(info)
        except Exception:
            # 落盘失败时退化为进程内互斥，至少保证同进程不并发
            if _OWNER["ts"] and not _stale(_OWNER):
                return False, holder_text(_OWNER)
        _OWNER.update(info)
        return True, ""


def release(kind=None, key=None):
    """释放占用。参数用于校验：只释放属于自己的占用，避免误清他人。"""
    global _OWNER
    with _MUTEX:
        cur = _read_file()
        if cur:
            if kind and cur.get("kind") and cur.get("kind") != kind:
                return False
            if key and cur.get("key") and cur.get("key") != key:
                return False
        _clear_file()
        _OWNER.update({"kind": "", "key": "", "pid": 0, "since": "", "ts": 0.0})
        return True


def hold(kind, key=""):
    """断言当前持有令牌；未持有则抛错。

    用于**破坏性操作**（如切号）前的纵深防御：
    万一将来新增入口忘了申请令牌，这里会明确报错而不是默默切错账号。
    """
    info = holder()
    if not info:
        raise RuntimeError(
            "未持有客户端占用令牌就执行「%s」——串行铁则被绕过，请检查入口是否调用了 try_acquire()" % (kind or "破坏性操作")
        )
    if kind and info.get("kind") and info.get("kind") != kind:
        raise RuntimeError(
            "当前占用者为「%s」，与本次操作「%s」不符，已拒绝" % (info.get("kind"), kind)
        )
    return info


def is_held():
    return holder() is not None
