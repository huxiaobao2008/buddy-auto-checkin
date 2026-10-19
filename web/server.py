#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""buddy-auto-v2 本地网页管理界面（零第三方依赖，标准库 http.server）。

只监听 127.0.0.1，不对外暴露；任何响应都不含 token。
启动：python -m web.server [--port 8765]
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as CFG
from core import client_lock as CL
from core import creds as CR
from core import logproto as LP
from engine import missions_api as MA
from engine import switch as AS
from engine import tasks as T
from orchestrator import batch as B
from orchestrator import claim as CLAIM

BASE_DIR = CFG.BASE_DIR
UI_FILE = os.path.join(BASE_DIR, "web", "ui.html")
LOCK = threading.Lock()

STATE_TTL = 5.0
STATE_CACHE = {"ts": 0.0, "data": None}
STATE_LOADING = False  # 后台线程是否正在加载数据

ACTIVE = {"key": ""}
WATCH = {"running": False, "interval": 30, "last_scan": "", "last_added": []}
RUN_ALL_PROC = None
RUN_ALL_LOG = None
# 串行互斥统一由 core/client_lock.py 的「客户端占用令牌」负责，
# 不要再在本文件另立标志位——多套并存正是此前串行铁则失效的根因。
RUN_ALL_LOG_PATH = os.path.join(CFG.LOG_DIR, "run_all_server.log")

ACCT_JOB = {
    "running": False,
    "key": "",
    "label": "",
    "tail": "",
    "result": None,
    "started": "",
    "finished": "",
}
ACCT_JOB_LOG_PATH = os.path.join(CFG.LOG_DIR, "run_account_server.log")

UI_CODE_JOB = {
    "running": False,
    "key": "",
    "code": "",
    "label": "",
    "tail": "",
    "result": None,
    "started": "",
    "finished": "",
}
UI_CODE_JOB_LOG_PATH = os.path.join(CFG.LOG_DIR, "run_ui_code_server.log")


def _state_bust():
    """让缓存过期（后台线程随即重建），但保留旧数据继续展示。

    修复（2026-10-17）：原实现把 data 一起清空，导致任何操作（续期/启停/增删/
    单账号执行完成）之后，前端拿到的是「空状态」——账号列表消失、统计归零、
    客户端登录条显示「未检测到」，要等后台重建（20 个账号逐一查 API，约 30~60 秒）
    才恢复。这正是用户看到的「工作台闪空/高亮时有时无」的主因之一。
    保留旧数据、只重置时间戳：后台重建完成后自动换新，期间页面不闪空。
    """
    STATE_CACHE["ts"] = 0.0


def _infer_area(msg):
    """按内容关键词推断日志所属区域（供只有单一 log() 回调的旧调用点使用）。

    修复点：此前单账号/单任务路径把所有日志硬编码为 area=ui，
    使「阶段一 API」相关行被前端错投到 UI 区域。此处按关键词归类。
    """
    s = str(msg or "")
    # 修复（2026-10-17）：先判 API 关键词再判「领取」——API 汇总行
    # 「签到/API: …本次接取 0 个、领取 0 个（+0 积分）」自带「领取」二字，
    # 原先会被错标成 claim 区。调整顺序后：含签到/API 等词的归 api，
    # 只有真正的收尾领取行（无 API 词、含领取/到账）才归 claim。
    if any(k in s for k in ("阶段一", "API", "签到", "盲盒", "猫猫", "热度", "出行")):
        return LP.AREA_API
    if any(k in s for k in ("收尾领取", "领取", "claim", "积分到账")):
        return LP.AREA_CLAIM
    return LP.AREA_UI


def _line_area(line):
    """从一行日志中解析出受控 area；解析失败返回 None。"""
    m = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\[([a-z]+)(?::[^\]]+)?\]", str(line or ""))
    return m.group(1) if m else None


def _pmap(fn, items, workers=4):
    items = list(items)
    if not items:
        return []
    w = max(1, min(workers, len(items)))
    if w == 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=w) as ex:
        return list(ex.map(fn, items))


# --------------------------------------------------------------------------- #
# 后台任务状态
# --------------------------------------------------------------------------- #
def run_all_status():
    tail = ""
    # 无论任务是否在执行，都读取日志尾部：
    # 任务正在跑时前端要看到实时进度；任务结束后前端要能看到最终结果
    # （并据此判定「已手动停止」），否则停止状态永远显示不出来。
    try:
        with open(RUN_ALL_LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            tail = "\n".join(fh.read().splitlines()[-200:])
    except Exception:
        pass
    running = bool(RUN_ALL_PROC and RUN_ALL_PROC.poll() is None)
    # server 重启后 RUN_ALL_PROC 会丢失，但 run_all 子进程可能仍在写日志：
    # 以日志文件最后写入新鲜度（60 秒内）作为补充判据，保证前端状态真实。
    if not running:
        try:
            mtime = os.path.getmtime(RUN_ALL_LOG_PATH)
            running = (time.time() - mtime) <= 60.0
        except OSError:
            running = False
    # 修复（2026-10-17）：日志已写「完成标记」就不能再判 running——
    # 任务收尾写盘与进程退出之间有时间差，新鲜度判据会让已完成任务
    # 在长达 60 秒内被前端显示成「运行中」（用户反馈：任务都做完了还像在进行）。
    # 注意：只认日志末尾的 [done] 受控标记；不能用「全部完成」做子串匹配——
    # 排序日志里有「全部完成的排最后」，会误命中把整个运行期判成未运行。
    if running and tail:
        _lines = [x for x in tail.strip().split("\n") if x.strip()]
        for line in reversed(_lines[-8:]):
            if _line_area(line) == LP.AREA_DONE:
                running = False
                break
    # 判断是不是被手动停止的。
    # 修复点：原逻辑只要最后 10 行里出现「停止」二字就判定 stopped，
    # 而「[停止] 收到停止指令」本身也会写入日志、正常完成的汇总里也可能
    # 出现「停止」字样，导致已完成任务被误显示为「已停止」。
    # 现改为只认「进程已退出 且 日志以停止收尾 且 无完成标记」。
    stopped = False
    if not running:
        lines = [x for x in tail.strip().split("\n") if x.strip()]
        # 从后往前找真正的收尾行（跳过启动/切号等噪声行）
        for line in reversed(lines):
            area = _line_area(line)
            # 完成标记优先级最高：出现即代表正常结束
            # （「全部完成。」带句号，避免误中排序日志里的「全部完成的排最后」）
            if area == LP.AREA_DONE or "全部完成。" in line:
                stopped = False
                break
            if area in (LP.AREA_UI, LP.AREA_CLAIM, LP.AREA_API) and (
                "收到停止指令" in line or "中止" in line
            ):
                stopped = True
                break
    return {
        "running": running,
        "stopped": stopped,
        "log_path": RUN_ALL_LOG_PATH,
        "tail": tail,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def action_stop(payload):
    """停止任务：写跨进程停止标记；进程不肯退出时短暂等待后强杀兜底。"""
    # 注意：不要第一行就 _state_bust() 清空缓存！用户正在看的日志和状态会瞬间消失。
    try:
        CFG.write_stop()
    except Exception as e:
        return {"ok": False, "error": "写停止标记失败：%s: %s" % (type(e).__name__, e)}
    killed = []
    global RUN_ALL_PROC
    # 修复：原代码在 wait 循环里直接调用 RUN_ALL_PROC.poll()，
    # 而 terminate() 之后若进程对象已被回收/置空会抛 AttributeError，
    # 导致整个停止流程 500 且停止标记写了但前端无反馈。此处逐次判空。
    proc = RUN_ALL_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        if proc.poll() is None:
            try:
                proc.kill()
                killed.append("run_all")
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
    if ACCT_JOB.get("running"):
        # 单账号任务在后台线程里跑，只能靠停止标记让其自行退出
        CFG.write_stop()  # 发停止指令
        for _ in range(60):
            if not ACCT_JOB.get("running"):
                break
            time.sleep(0.5)
        if ACCT_JOB.get("running"):
            killed.append("单账号任务(等待线程退出)")
    if UI_CODE_JOB.get("running"):
        CFG.write_stop()
        for _ in range(40):
            if not UI_CODE_JOB.get("running"):
                break
            time.sleep(0.25)
        if UI_CODE_JOB.get("running"):
            killed.append("单任务(等待线程退出)")
    # 兜底：子进程被强杀时不会执行自己的释放，这里由 Web 侧清一次，
    # 避免令牌永久占用导致后续所有任务都被拒绝。
    try:
        if CL.holder():
            CL.release()
    except Exception:
        pass
    return {
        "ok": True,
        "stopping": True,
        "killed": killed,
        "message": "已发出停止指令"
        + ("，正在中止：%s" % "、".join(killed) if killed else ""),
    }


def action_run_all(payload):
    """一键全自动：后台启动 run_all 模块（独立子进程）。"""
    global RUN_ALL_PROC, RUN_ALL_LOG
    # 串行铁则：统一用「客户端占用令牌」互斥，不再各自维护状态标志。
    # 令牌跨进程有效（run_all 是子进程），这是 threading.Lock 做不到的。
    got, who = CL.try_acquire("一键全自动", "全部启用账号 · 全流程")
    if not got:
        return {
            "ok": False,
            "error": "%s 正在执行中，请等待完成后再启动" % who,
            "holder": CL.holder(),
        }
    try:
        # 一键全自动开始前，先清空缓存和旧日志
        _state_bust()
        CFG.clear_stop()
        cfg = CFG.load_config()
        py = cfg.get("python_path") or sys.executable
        os.makedirs(CFG.LOG_DIR, exist_ok=True)
        # 修复：上一轮的日志句柄从未关闭，反复执行会持续泄漏句柄；
        # 且 Windows 上未关闭的句柄会锁住文件，导致本轮 truncate 失败。
        if RUN_ALL_LOG is not None:
            try:
                RUN_ALL_LOG.close()
            except Exception:
                pass
            RUN_ALL_LOG = None
        f = open(RUN_ALL_LOG_PATH, "wb")
        f.seek(0)
        f.truncate(0)
        RUN_ALL_LOG = f
        # 令牌移交子进程持有：父进程只负责启动，真正的执行方是子进程。
        # 父进程在此释放自己的登记，由 run_all() 入口以自身 pid 重新申请，
        # 这样「持有者」始终是真正在干活的那个进程，僵尸判定才准确。
        CL.release()
        RUN_ALL_PROC = subprocess.Popen(
            [py, "-c", "from orchestrator.run_all import run_all; run_all()"],
            cwd=CFG.BASE_DIR,
            stdout=f,
            stderr=subprocess.STDOUT,
        )
        return {
            "ok": True,
            "running": True,
            "message": "已启动一键全自动，进度见下方",
            "pid": RUN_ALL_PROC.pid,
        }
    except Exception as e:
        CL.release()
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def acct_job_status():
    tail = ""
    try:
        with open(ACCT_JOB_LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            tail = "\n".join(fh.read().splitlines()[-200:])
    except Exception:
        pass
    return {
        "running": bool(ACCT_JOB.get("running")),
        "key": ACCT_JOB.get("key", ""),
        "label": ACCT_JOB.get("label", ""),
        "tail": tail,
        "result": ACCT_JOB.get("result"),
        "started": ACCT_JOB.get("started", ""),
        "finished": ACCT_JOB.get("finished", ""),
    }


def _should_skip_ui_tasks(acc, cfg):
    try:
        cred, err = CR.load_cred(acc)
        if not cred:
            return False, ""
        tl, _ = T.fetch_tasks(cred, cfg)
        s = T.summarize(tl)
        if (
            s.get("total")
            and s.get("done") == s.get("total")
            and not s.get("pending_credit")
        ):
            return True, "成长任务已全部完成（%d/%d），跳过 UI 任务驱动" % (
                s["done"],
                s["total"],
            )
    except Exception as e:
        return False, "任务状态读取失败：%s" % e
    return False, ""


def _run_account_job(key):
    """后台线程：对单个账号跑完整流水线（API 任务 + UI 任务 + 领取），再切回原账号。"""
    global ACCT_JOB
    try:
        cfg = CFG.load_config()
        acc = CFG.find_account(cfg, key)
        if not acc:
            ACCT_JOB["result"] = {"ok": False, "error": "配置里没有账号 %s" % key}
            return
        ACCT_JOB["label"] = acc.get("label", key)
        ACCT_JOB["started"] = CFG.now_str()

        restore_key = None
        try:
            restore_key = AS.current_key()[0]
        except Exception:
            restore_key = None

        f = open(ACCT_JOB_LOG_PATH, "wb")
        f.seek(0)
        f.truncate(0)

        # 修复：日志行统一走 logproto，保证时间戳为 HH:MM:SS、area 受控。
        # 此前固定写 "[ui:key]"，导致 API 阶段日志被错标进 UI 区，
        # 且 now_str() 的 "YYYY-MM-DD HH:MM:SS" 与前端正则锚定不匹配。
        def _emit(area, m):
            line = LP.format_line(area, m, account=key)
            try:
                f.write((line + "\n").encode("utf-8"))
                f.flush()
            except Exception:
                pass
            ACCT_JOB["tail"] = line

        def log(m):
            # 保持旧调用签名 log(msg)：按内容关键词推断area，
            # 避免大面积改调用点，同时让分流恢复正确。
            _emit(_infer_area(m), m)

        def log_api(m):
            _emit(LP.AREA_API, m)

        def log_ui(m):
            _emit(LP.AREA_UI, m)

        def log_claim(m):
            _emit(LP.AREA_CLAIM, m)

        try:
            # 先检查一下有没有能自动完成的任务未完成（仅作日志展示）
            # 修复（2026-10-17）：原先这里「全部完成就直接 return」，会把阶段一的
            # 每日签到/盲盒/出行也一起跳过——成长任务做完不等于今天签过到，
            # 15/15 的账号点「执行本账号」会漏掉当天签到积分。
            # UI 阶段的跳过由下方 _should_skip_ui_tasks 负责，这里不再短路。
            _cred, _e = CR.load_cred(acc)
            if _cred:
                _lst, _le = T.fetch_tasks(_cred, cfg)
                if not _le:
                    _s = T.summarize(_lst)
                    log("服务端进度 %s/%s" % (_s.get("done"), _s.get("total")))
            log("阶段一：签到 / 出行 / API 成长任务 / 热度 / 盲盒 / 猫猫")
            r = MA.run_account(acc, cfg)
            log("签到/API: %s | %s" % (r.get("status"), r.get("reason")))
            # 修复（2026-10-17）：API 阶段实得（成长任务领取 + 每日签到 gained）
            # 此前完全没计入结果，toast「本次领取 X 积分」漏掉签到的大头。
            _gt = (r.get("tasks") or {}).get("growth_tasks") or {}
            _ck = (r.get("tasks") or {}).get("checkin") or {}
            api_got = int(_gt.get("credit_got") or 0) + int(
                ((_ck.get("data") or {}).get("gained")) or 0
            )

            skip, why = _should_skip_ui_tasks(acc, cfg)
            ui_got = 0
            if skip:
                log(why)
            else:
                # 动态获取该账号真实未达标任务（2026-09-24：已完成/跳过的不再进入执行清单）
                codes = None
                out = B.run_account(
                    key,
                    codes,
                    do_claim=True,
                    do_delete=True,
                    dry=False,
                    log=log,
                    stop=CFG.stop_requested,
                )
                ui_got = int((out.get("claim") or {}).get("credit_got") or 0)
                log(
                    "UI 切号 ok=%s | %s"
                    % ((out.get("switch") or {}).get("ok"), out.get("err") or "完成")
                )

            cr = CLAIM.claim_account(acc, cfg, quiet=True)
            claim_got = int(cr.get("credit_got") or 0)
            log("收尾领取 %s 项 | 到账 %s 积分" % (cr.get("claimed"), claim_got))

            _res = {
                "ok": True,
                "api": r.get("status"),
                "skipped_ui": skip,
                "skip_reason": why,
                "credit_got": api_got + ui_got + claim_got,
                "api_got": api_got,
                "ui_got": ui_got,
                "claim_got": claim_got,
            }
            ACCT_JOB["result"] = _res
            try:
                _cred, _e = CR.load_cred(acc)
                if _cred:
                    _lst, _le = T.fetch_tasks(_cred, cfg)
                    if not _le:
                        _s = T.summarize(_lst)
                        log("服务端进度 %s/%s" % (_s.get("done"), _s.get("total")))
                        _res["server_done"] = _s.get("done")
                        _res["server_total"] = _s.get("total")
            except Exception as _e:
                log("服务端进度对账失败：%s" % str(_e)[:80])
        finally:
            if restore_key:
                try:
                    AS.switch_to(restore_key, reload=False)
                    log("已切回原账号 %s" % restore_key)
                except Exception as e:
                    log("切回原账号失败：%s" % e)
            try:
                f.close()
            except Exception:
                pass
    except Exception as e:
        ACCT_JOB["result"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        ACCT_JOB["running"] = False
        ACCT_JOB["finished"] = CFG.now_str()
        # 归还客户端占用令牌，否则其他入口会被永久拒绝
        try:
            CL.release()
        except Exception:
            pass
        _state_bust()  # 执行完任务后清空缓存，刷新页面数据


def action_run_account(payload):
    """点账号「执行本账号」：后台跑该账号完整流水线。"""
    key = (payload or {}).get("key", "")
    if not key:
        return {"ok": False, "error": "缺少 key"}
    with LOCK:
        cfg = CFG.load_config()
        acc = CFG.find_account(cfg, key)
    if not acc:
        return {"ok": False, "error": "列表里没有 %s" % key}
    # 串行铁则：与一键全自动、单任务、手动切号共用同一把令牌
    got, who = CL.try_acquire("单账号执行", acc.get("label", key))
    if not got:
        return {
            "ok": False,
            "error": "%s 正在执行中，请等待完成后再启动" % who,
            "holder": CL.holder(),
        }
    CFG.clear_stop()
    ACCT_JOB.update(
        {
            "running": True,
            "key": key,
            "label": acc.get("label", key),
            "result": None,
            "tail": "启动中…",
            "started": CFG.now_str(),
            "finished": "",
        }
    )
    threading.Thread(target=_run_account_job, args=(key,), daemon=True).start()
    return {
        "ok": True,
        "message": "已启动 %s 的完整执行（签到+成长任务+领积分）"
        % acc.get("label", key),
        "key": key,
    }


def ui_code_job_status():
    tail = ""
    try:
        with open(UI_CODE_JOB_LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            tail = "\n".join(fh.read().splitlines()[-200:])
    except Exception:
        pass

    return {
        "running": bool(UI_CODE_JOB.get("running")),
        "key": UI_CODE_JOB.get("key", ""),
        "code": UI_CODE_JOB.get("code", ""),
        "title": T.title_of(UI_CODE_JOB.get("code", "")),
        "label": UI_CODE_JOB.get("label", ""),
        "tail": tail,
        "result": UI_CODE_JOB.get("result"),
        "started": UI_CODE_JOB.get("started", ""),
        "finished": UI_CODE_JOB.get("finished", ""),
    }


def _run_ui_code_job(key, code):
    """后台线程：对单个账号执行单个 UI code，切号 → 执行 → 切回原账号。"""
    global UI_CODE_JOB
    try:
        cfg = CFG.load_config()
        acc = CFG.find_account(cfg, key)
        if not acc:
            UI_CODE_JOB["result"] = {"ok": False, "error": "配置里没有账号 %s" % key}
            return
        UI_CODE_JOB["label"] = acc.get("label", key)
        UI_CODE_JOB["started"] = CFG.now_str()

        restore_key = None
        try:
            restore_key = AS.current_key()[0]
        except Exception:
            restore_key = None

        f = open(UI_CODE_JOB_LOG_PATH, "wb")
        f.seek(0)
        f.truncate(0)

        # 修复：同_run_account_job，统一走 logproto（HH:MM:SS + 受控 area）。
        def _emit(area, m):
            line = LP.format_line(area, m, account=key)
            try:
                f.write((line + "\n").encode("utf-8"))
                f.flush()
            except Exception:
                pass
            UI_CODE_JOB["tail"] = line

        def log(m):
            # 单任务执行本质上都是 UI 阶段任务
            _emit(LP.AREA_UI, m)

        try:
            log("执行 UI 任务 %s -> %s" % (key, code))
            r = B.run_ui_code_once(
                key, code, do_delete=True, log=log, stop=CFG.stop_requested
            )
            log("结果：%s" % (r.get("error") or r.get("skipped") or "ok"))
            UI_CODE_JOB["result"] = r
        except Exception as e:
            log("异常：%s: %s" % (type(e).__name__, e))
            UI_CODE_JOB["result"] = {
                "ok": False,
                "error": "%s: %s" % (type(e).__name__, e),
            }
        finally:
            if restore_key:
                try:
                    AS.switch_to(restore_key, reload=False)
                    log("已切回原账号 %s" % restore_key)
                except Exception as e:
                    log("切回原账号失败：%s" % e)
            try:
                f.close()
            except Exception:
                pass
    except Exception as e:
        UI_CODE_JOB["result"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        UI_CODE_JOB["running"] = False
        UI_CODE_JOB["finished"] = CFG.now_str()
        # 归还客户端占用令牌
        try:
            CL.release()
        except Exception:
            pass
        _state_bust()


def action_run_ui_code(payload):
    """从任务列表点「执行」：对单个账号执行一个 UI 类成长任务。"""
    global UI_CODE_JOB
    key = (payload or {}).get("key", "")
    code = (payload or {}).get("code", "")
    if not key or not code:
        return {"ok": False, "error": "缺少 key 或 code"}
    with LOCK:
        cfg = CFG.load_config()
        acc = CFG.find_account(cfg, key)
    if not acc:
        return {"ok": False, "error": "列表里没有 %s" % key}
    if code not in B.UI_CODES:
        return {"ok": False, "error": "「%s」不是 UI 驱动任务" % T.title_of(code)}
    # 串行铁则：与其他入口共用同一把令牌。
    # 若已有任务在跑，先请求停止并等它真正退出（带超时），
    # 退出后原持有方会归还令牌，这里再重新申请。
    if CL.holder():
        CFG.write_stop()
        deadline = time.time() + 30
        while CL.holder() and time.time() < deadline:
            time.sleep(0.25)
        if CL.holder():
            return {
                "ok": False,
                "error": "上一个任务仍在停止中（%s），请稍后再试" % CL.holder_text(),
                "holder": CL.holder(),
            }
    got, who = CL.try_acquire("单任务执行", "%s / %s" % (acc.get("label", key), T.title_of(code)))
    if not got:
        return {
            "ok": False,
            "error": "%s 正在执行中，请等待完成后再启动" % who,
            "holder": CL.holder(),
        }
    CFG.clear_stop()
    UI_CODE_JOB.update(
        {
            "running": True,
            "key": key,
            "code": code,
            "label": acc.get("label", key),
            "result": None,
            "tail": "启动中…",
            "started": CFG.now_str(),
            "finished": "",
            }
        )
    threading.Thread(target=_run_ui_code_job, args=(key, code), daemon=True).start()
    return {
        "ok": True,
        "message": "已启动 %s 的「%s」任务" % (acc.get("label", key), T.title_of(code)),
        "key": key,
        "code": code,
        "title": T.title_of(code),
    }


def action_set_mode(payload):
    with LOCK:
        cfg = CFG.load_config()
        cfg["auto_mode"] = bool((payload or {}).get("auto", False))
        CFG.save_config(cfg)
    # 切换模式后，手动更新 STATE_CACHE 里的 auto_mode
    if STATE_CACHE.get("data"):
        STATE_CACHE["data"]["auto_mode"] = cfg["auto_mode"]
        STATE_CACHE["ts"] = time.time()
    return {
        "ok": True,
        "auto_mode": cfg["auto_mode"],
        "message": "已切到%s模式" % ("自动" if cfg["auto_mode"] else "手动"),
    }


def watch_new_accounts(interval=30):
    WATCH.update({"running": True, "interval": interval})
    while True:
        try:
            added, skipped = CR.auto_sync_accounts()
            WATCH["last_scan"] = CFG.now_str()
            if added:
                WATCH["last_added"] = added
                print(
                    "[%s] 自动入库 %d 个新账号：%s"
                    % (CFG.now_str(), len(added), "、".join(a["label"] for a in added))
                )
        except Exception as e:
            WATCH["last_scan"] = "%s（巡检异常：%s）" % (CFG.now_str(), e)
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# 状态组装
# --------------------------------------------------------------------------- #
def _acc_label(acc, cred=None):
    """config 账号的展示名：**凭证里的真实手机号/昵称优先**，再回退配置 label/nickname，最后 uin/uid。

    只认 config 静态值是不够的：账号入库时若客户端还没写入可读昵称，配置里会残留
    uin（一串数字），UI 就一直显示它、永不校正。凭证已解密时以凭证为准。
    """
    real = CR.cred_display_name(cred) if cred else ""
    name = CR.plain(acc.get("label") or acc.get("nickname")) or CR.plain(
        acc.get("nickname")
    )
    # 配置里的值若只是一串编号（uin/uid/数字），就用凭证里的真实账号名顶替；
    # 用户自己写的中英文备注原样保留。
    if CR.looks_like_id(name):
        # 凭证解密出的就是账号本身的显示名——纯数字（手机号）也算有效值，直接采用
        name = real or name
    if not name:
        name = str(acc.get("uin") or acc.get("uid") or "")
    return name


def account_state(acc, with_tasks=True):
    cred, err = CR.load_cred(acc)
    label = _acc_label(acc, cred)
    item = {
        "key": acc["key"],
        "label": label,
        "uid": acc.get("uid", ""),
        "nickname": label,
        "enabled": acc.get("enabled", True),
        "times": acc.get("times", []),
        "cred_ok": cred is not None,
        "cred_error": err or "",
        "credits": {},
        "balance": {},
        "tasks": [],
        "task_summary": {},
        "profile": {},
    }
    if cred:
        days = CR.cred_expiry_days(cred)
        item["cred_expiry_days"] = round(days, 1) if days is not None else None
        item["cred_expired"] = bool(days is not None and days <= 0)
        item["credits"] = T.query_credits(cred)
        item["balance"] = T.query_balance(cred)
        if with_tasks:
            cfg = CFG.load_config()
            tl, terr = T.fetch_tasks(cred, cfg)
            prof, perr = T.fetch_profile(cred, cfg)
            item["tasks"] = tl
            item["task_summary"] = T.summarize(tl)
            item["profile"] = prof
            item["task_error"] = terr or perr or ""
    return item


def discovered_state(cfg):
    warnings = []
    if not os.path.isdir(CFG.AUTH_DIR):
        return [], [
            "没找到 WorkBuddy 登录态目录，本机可能从未登录过客户端：" + CFG.AUTH_DIR
        ]
    in_cfg = {a.get("uid"): a["key"] for a in cfg["accounts"]}

    def one(d):
        err = ""
        credits = {"err": ""}
        try:
            data = CR.read_auth_file(d["path"])
            if not data:
                credits = {"err": "登录态文件解析失败"}
            else:
                # 客户端新版本会把 accessToken 也加密成信封对象，文件里的 token 不能直接用；
                # 若该 uid 在 config 里有明文 token 的凭证快照，则用它来查积分/余额。
                query_cred = data
                tok = (data.get("auth") or {}).get("accessToken")
                if CR.is_encrypted(tok):
                    for a in cfg["accounts"]:
                        if a.get("uid") == d["uid"]:
                            c, _e = CR.load_cred(a)
                            if c and not CR.is_encrypted(
                                (c.get("auth") or {}).get("accessToken")
                            ):
                                query_cred = c
                            break
                credits = T.query_credits(query_cred)
        except Exception as e:
            credits = {"err": "%s: %s" % (type(e).__name__, e)}
            err = "%s 读取失败：%s" % (d["nickname"] or d["uid"], e)
        row = {
            "uid": d["uid"],
            "nickname": d["nickname"],
            "phone": d["phone"],
            "saved_at": d["saved_at"],
            "expiry_days": d["expiry_days"],
            "expired": d["expired"],
            "credits": credits,
            "in_config": d["uid"] in in_cfg,
            "config_key": in_cfg.get(d["uid"], ""),
        }
        return row, err

    results = _pmap(one, CR.discover_accounts(), workers=12)
    out = [r for r, _ in results]
    warnings = [e for _, e in results if e]
    if not out:
        warnings.append(
            "登录态目录里没读到任何可用账号（文件损坏或字段缺失）：%s" % CFG.AUTH_DIR
        )
    return out, warnings


def client_current_state():
    """客户端当前登录：登录文件 uid 为主，CDP 界面昵称为辅。

    修复（2026-10-17）：原实现完全依赖 CDP 读界面昵称——CDP 短暂抖动就整体
    判为不可达，前端高亮/登录条随之消失（用户反馈「有时候显示有时候不显示」）。
    现改为：能读到登录文件 uid 即视为可达（客户端身份以登录文件为准），
    CDP 只用于补充界面昵称展示；昵称缺省时回退 config 里的账号名。
    """
    key, menu, uid_live, err = "", "", "", ""
    try:
        j = CR.read_auth_file(CFG.CURRENT_AUTH) or CFG.load_json(CFG.CURRENT_AUTH) or {}
        uid_live = ((j or {}).get("account") or {}).get("uid") or ""
    except Exception:
        pass
    try:
        k2, m2 = AS.current_key()
        key, menu = k2 or "", m2 or ""
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    # uid 读到了但没匹配上 config（current_key 内部另有兜底）：这里再按 uid 直接比一次
    if uid_live and not key:
        try:
            cfg = CFG.load_config()
            for a in cfg.get("accounts") or []:
                if a.get("uid") == uid_live:
                    key = a.get("key") or ""
                    break
        except Exception:
            pass
    # 昵称缺失时用 config 里的账号名补展示（label 优先，那是用户认得的备注）
    if key and not menu:
        try:
            cfg = CFG.load_config()
            acc = CFG.find_account(cfg, key)
            if acc:
                menu = acc.get("label") or acc.get("nickname") or ""
        except Exception:
            pass
    reachable = bool(uid_live) or bool(menu)
    return {
        "reachable": reachable,
        "key": key,
        "menu": menu,
        "uid": uid_live,
        "error": "" if reachable else (err or "未读到客户端登录文件"),
    }


def _build_state_sync():
    """同步构建完整状态：查询所有账号的 API（积分/余额/任务/资料）。
    第一次启动时会很慢，所以放在后台线程里跑。"""
    with LOCK:
        cfg = CFG.load_config()
    accounts = _pmap(lambda a: account_state(a), cfg["accounts"], workers=12)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_disc = ex.submit(discovered_state, cfg)
        f_cc = ex.submit(client_current_state)
        discovered, warnings = f_disc.result()
        client_current = f_cc.result()
    for a in accounts:
        a["is_current_client"] = bool(client_current.get("reachable")) and a[
            "key"
        ] == client_current.get("key")

    # 积分台账：读签到流程自动生成的快照（logs/credits_report.json），零额外请求。
    # 行内只展示决策字段（可用积分 / 最近过期 / 倒计时 / 7天内将过期），
    # 完整明细（过期批次、时间轴、优先消耗顺序）在独立报告页 /credits 查看。
    ledger = {}
    ledger_ts = ""
    try:
        from core import credits as CRE

        _rep = CRE.load_report()
        ledger_ts = _rep.get("generated_at") or ""
        for _r in _rep.get("accounts") or []:
            if _r.get("err"):
                continue
            _n = (_r.get("buckets") or [None])[0]
            _b = CRE._bands(_r)
            ledger[_r.get("key")] = {
                "alive": round(_r.get("alive_sum") or 0, 2),
                "next_expire": _n["expire"] if _n else "",
                "next_countdown": CRE.human(_n["secs"]) if _n else "",
                "next_secs": _n["secs"] if _n else CRE.FAR,
                "expiring_7d": round(_b["d1"] + _b["d3"] + _b["d7"], 2),
                "expiring_30d": _b["d30"],
                "packages_with_remain": _r.get("packages_with_remain") or 0,
                "packages_total": _r.get("packages_total") or 0,
            }
    except Exception:
        ledger, ledger_ts = {}, ""
    for a in accounts:
        a["credit_ledger"] = ledger.get(a.get("key")) or {}

    total = sum((a["credits"] or {}).get("total_credits") or 0 for a in accounts)
    pending = sum(
        (a.get("task_summary") or {}).get("pending_credit") or 0 for a in accounts
    )
    potential = sum(
        (a.get("task_summary") or {}).get("potential_credit") or 0 for a in accounts
    )
    signed = sum(1 for a in accounts if (a["credits"] or {}).get("signed"))
    remaining = 0.0
    remaining_unknown = []
    for a in accounts:
        bal = a.get("balance") or {}
        if bal.get("err"):
            remaining_unknown.append(a["label"])
        else:
            remaining += bal.get("remaining") or 0.0
        if not a["cred_ok"]:
            warnings.append("%s 无有效凭证：%s" % (a["label"], a["cred_error"]))
        elif a.get("cred_expired"):
            warnings.append("%s 凭证已过期，请点「登录/续期」" % a["label"])
        elif a.get("cred_expiry_days") is not None and a["cred_expiry_days"] < 7:
            warnings.append(
                "%s 凭证 %.0f 天后过期" % (a["label"], a["cred_expiry_days"])
            )

    account_names = {
        a["key"]: (a.get("phone") or a.get("label") or a["key"]) for a in accounts
    }
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "accounts": accounts,
        "account_names": account_names,
        "discovered": discovered,
        "warnings": warnings,
        "totals": {
            "accounts": len(accounts),
            "signed_today": signed,
            "total_credits": total,
            # 修复（2026-10-17）：原先 pending_credits 错填成 potential（上面算好的
            # pending 变量根本没被用上），「总待领积分」永远显示的是
            # 「未完成任务的未来可得积分」而非「当前可领取积分」。
            "pending_credits": pending,
            "potential_credits": potential,
            "remaining_credits": round(remaining, 2),
            "remaining_unknown": remaining_unknown,
            # 积分台账（资源包口径，与签到积分是两套账）
            "ledger_total": round(sum(v["alive"] for v in ledger.values()), 2),
            "ledger_expiring_7d": round(
                sum(v["expiring_7d"] for v in ledger.values()), 2
            ),
            "ledger_ts": ledger_ts,
        },
        "client_exe": next(
            (p for p in CFG.CLIENT_EXE_CANDIDATES if p and os.path.exists(p)), ""
        ),
        "schedule": cfg.get("schedule", {}),
        "client_current": client_current,
        # 客户端占用令牌（串行铁则）：谁正在占用客户端
        "holder": CL.holder(),
        "holder_text": CL.holder_text(),
        "run_all": run_all_status(),
        "acct_job": acct_job_status(),
        "uicode_job": ui_code_job_status(),
        "auto_mode": bool(cfg.get("auto_mode", False)),
        "skip_codes": list(cfg.get("skip_tasks", {}).get("codes", [])),
        "ui_codes": list(B.UI_CODES),
    }
    return data


# 后台状态线程的代际编号：只有最新一代线程能写缓存，
# 旧线程即使最终完成也不会覆盖新缓存。
STATE_GEN = 0
STATE_LOADING_TS = 0.0
STATE_LOADING_MAX = 90.0  # 后台线程最长时间，超时视为卡死，强制重启


def _build_state_background():
    """后台线程：慢慢加载真实数据，加载完成后更新缓存。
    带看门狗与代际守卫：卡死的旧线程不影响新线程工作。"""
    global STATE_LOADING, STATE_GEN, STATE_LOADING_TS
    STATE_LOADING = True
    STATE_LOADING_TS = time.time()
    gen = STATE_GEN
    try:
        data = _build_state_sync()
        # 只有最新一代线程允许写缓存
        if gen == STATE_GEN:
            STATE_CACHE["data"] = data
            STATE_CACHE["ts"] = time.time()
    except Exception as e:
        print("[警告] 后台加载状态失败：%s" % e, flush=True)
    finally:
        if gen == STATE_GEN:
            STATE_LOADING = False


def build_state(use_cache=True):
    global STATE_LOADING
    # 第一次启动时，如果没有缓存，先返回空数据，让 server 马上能响应
    # 然后在后台线程里一次性加载所有账号的数据
    if STATE_CACHE["data"] is None:
        # 修复（2026-10-17）：空状态也要带上「客户端当前登录」（只读登录文件 uid，
        # 不碰 CDP，毫秒级）。否则首次启动的 30~60 秒里登录条/高亮一直显示
        # 「未检测到客户端登录状态」，与真实状态不符。
        cc_quick = {"reachable": False, "key": "", "menu": "", "error": ""}
        try:
            _j = CR.read_auth_file(CFG.CURRENT_AUTH) or CFG.load_json(CFG.CURRENT_AUTH) or {}
            _uid = ((_j or {}).get("account") or {}).get("uid") or ""
            if _uid:
                cc_quick["reachable"] = True
                cc_quick["uid"] = _uid
                for _a in CFG.load_config().get("accounts") or []:
                    if _a.get("uid") == _uid:
                        cc_quick["key"] = _a.get("key") or ""
                        cc_quick["menu"] = _a.get("label") or _a.get("nickname") or ""
                        break
        except Exception:
            pass
        empty = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": [],
            "account_names": {},
            "discovered": [],
            "client_current": cc_quick,
            "warnings": ["正在加载账号数据，请稍候..."],
            "totals": {
                "accounts": 0,
                "signed_today": 0,
                "total_credits": 0,
                "claimed_today": 0,
            },
            "loading": True,
            "stopping": CFG.stop_requested(),
            # 客户端占用令牌（串行铁则）：让「谁在占用」对用户可见，
            # 冲突时能看懂原因，而不是只看到一个笼统的失败提示。
            "holder": CL.holder(),
            "holder_text": CL.holder_text(),
            "run_all": run_all_status(),
            "acct_job": acct_job_status(),
            "uicode_job": ui_code_job_status(),
        }
        STATE_CACHE["data"] = empty
        STATE_CACHE["ts"] = time.time()
        threading.Thread(target=_build_state_background, daemon=True).start()
        return empty

    # 缓存过期且没有后台线程在跑：启动后台重建
    if (time.time() - STATE_CACHE["ts"]) >= STATE_TTL and not STATE_LOADING:
        threading.Thread(target=_build_state_background, daemon=True).start()
    # 看门狗：后台线程卡死超过 STATE_LOADING_MAX 秒，强制重建（旧线程结果丢弃）
    elif STATE_LOADING and (time.time() - STATE_LOADING_TS) > STATE_LOADING_MAX:
        print(
            "[看门狗] 后台状态线程疑似卡死（%.0fs），强制重启重建"
            % (time.time() - STATE_LOADING_TS),
            flush=True,
        )
        STATE_LOADING = False
        threading.Thread(target=_build_state_background, daemon=True).start()

    # 实时字段注入：任务日志/任务状态不经过缓存，
    # 前端每 3 秒轮询都能拿到此时最新的 run_all 日志，不再被缓存冻结挡住。
    data = dict(STATE_CACHE["data"])
    data["stopping"] = CFG.stop_requested()
    data["run_all"] = run_all_status()
    data["acct_job"] = acct_job_status()
    data["uicode_job"] = ui_code_job_status()
    return data


def action_add(payload):
    uid = (payload or {}).get("uid", "").strip()
    if not uid:
        return {"ok": False, "error": "缺少 uid"}
    with LOCK:
        acc, msg = CR.add_account_by_uid(uid, label=(payload or {}).get("label"))
    if not acc:
        return {"ok": False, "error": msg}
    return {"ok": True, "message": msg, "key": acc["key"]}


def action_run(payload):
    """执行签到与成长任务：不传 key 则执行全部。"""
    with LOCK:
        cfg = CFG.load_config()
        key = (payload or {}).get("key")
        results = []
        for a in cfg["accounts"]:
            if not a.get("enabled", True):
                continue
            if key and a["key"] != key:
                continue
            r = MA.run_account(a, cfg)
            r["ts"] = CFG.now_str()
            r["date"] = CFG.today_str()
            r["trigger"] = "ui"
            from core import logs as L

            L.append_run(r)
            cred, _ = CR.load_cred(a)
            tl = T.fetch_tasks(cred, cfg)[0] if cred else []
            gt = r.get("tasks", {}).get("growth_tasks") or {}
            results.append(
                {
                    "key": r["account"],
                    "label": r.get("label", ""),
                    "status": r["status"],
                    "reason": r.get("reason", ""),
                    "steps": gt.get("steps", []),
                    "task_summary": T.summarize(tl),
                    "balance": {
                        "before": gt.get("balance_before"),
                        "after": gt.get("balance_after"),
                        "delta": gt.get("balance_delta"),
                        "error": gt.get("balance_error", ""),
                        "credit_got": gt.get("credit_got", 0),
                    },
                    "skipped_paid": gt.get("skipped_paid", []),
                    "failed_tasks": gt.get("failed_tasks", []),
                    "credits": T.query_credits(cred) if cred else {},
                }
            )
        CFG.save_json(CFG.LATEST_JSON, {"ts": CFG.now_str(), "results": results})
    if not results:
        return {"ok": False, "error": "没有可执行的启用账号"}
    return {
        "ok": True,
        "message": "已执行 %d 个账号" % len(results),
        "results": results,
    }


def action_capture(payload):
    payload = payload or {}
    with LOCK:
        cfg = CFG.load_config()
        acc = CFG.find_account(cfg, payload.get("key"))
        if not acc:
            return {"ok": False, "error": "列表里没有 %s" % payload.get("key")}
        # 在该 uid 的全部本地快照里逐个探活抓取：优先「真能用的」，而不是「理论上
        # 有效期最长的」（旧长命会话可能已被服务端清掉，2026-10-18 d/e/f 事故）。
        rc, msg = CR.capture_best(acc)
        return {
            "ok": rc == 0,
            "error": "" if rc == 0 else msg,
            "message": msg,
            "status": {0: "已续期", 3: "已过期"}.get(rc, "失败"),
            "expired": rc == 3,
            "need_login": rc != 0,
            "hint": "本机没有该账号的可用登录态，请打开客户端登录该账号后重试" if rc != 0 else "",
        }


def action_capture_all(payload):
    with LOCK:
        rows = CR.renew_all_with_status()
    renewed = [r["key"] for r in rows if r["rc"] == 0]
    failed = [r["key"] for r in rows if r["rc"] != 0]
    return {
        "ok": len(failed) == 0,
        "renewed": renewed,
        "failed": failed,
        "rows": rows,
        "message": "已续期 %d 个，失败 %d 个" % (len(renewed), len(failed)),
    }


def action_open_client(payload):
    for p in CFG.CLIENT_EXE_CANDIDATES:
        if p and os.path.exists(p):
            try:
                subprocess.Popen(
                    [p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                return {"ok": True, "message": "已启动客户端：%s" % p}
            except Exception as e:
                return {"ok": False, "error": "启动客户端失败：%s" % e}
    return {"ok": False, "error": "没找到 WorkBuddy 客户端，请手动打开"}


def action_remove(payload):
    with LOCK:
        ok, msg = CR.remove_account((payload or {}).get("key", ""))
    return {"ok": ok, "message": msg, "error": "" if ok else msg}


def action_toggle(payload):
    payload = payload or {}
    key = (payload.get("key") or "").strip()
    # 修复：缺 key 时原先仍会调用 set_enabled，错误信息为「列表里没有 」（空名），
    # 用户看不懂。这里前置拦截并给出明确提示。
    if not key:
        return {"ok": False, "message": "缺少账号 key", "error": "缺少账号 key"}
    with LOCK:
        ok, msg = CR.set_enabled(key, payload.get("enabled", True))
    return {"ok": ok, "message": msg, "error": "" if ok else msg}


def action_task(payload):
    """单个成长任务的操作：接取(accept) / 领取(claim) / 两者(both)。"""
    payload = payload or {}
    key, code = payload.get("key", ""), payload.get("code", "")
    action = payload.get("action", "both")
    if not key or not code:
        return {"ok": False, "error": "缺少 key 或 code"}
    if not T.SERIAL_LOCK.acquire(blocking=True, timeout=60):
        return {"ok": False, "error": "已有执行在进行中（串行保护），请稍后重试"}
    try:
        with LOCK:
            cfg = CFG.load_config()
            acc = CFG.find_account(cfg, key)
        if not acc:
            return {"ok": False, "error": "列表里没有 %s" % key}
        cred, err = CR.load_cred(acc)
        if err:
            return {"ok": False, "error": err, "need_login": True}
        steps = []
        if action in ("accept", "both"):
            ok, msg, _ = T.accept_task(cred, code, cfg)
            steps.append(
                {
                    "task": code,
                    "step": "accept",
                    "status": "success" if ok else ("fail" if ok is False else "skip"),
                    "reason": msg,
                }
            )
        if action in ("claim", "both"):
            ok, msg, _ = T.claim_task(cred, code, cfg)
            steps.append(
                {
                    "task": code,
                    "step": "claim",
                    "status": "success" if ok else ("fail" if ok is False else "skip"),
                    "reason": msg,
                }
            )
        failed = [s for s in steps if s["status"] == "fail"]
        return {
            "ok": not failed,
            "steps": steps,
            "message": "；".join("%s:%s" % (s["step"], s["reason"]) for s in steps),
            "error": "；".join(s["reason"] for s in failed),
        }
    finally:
        T.SERIAL_LOCK.release()


def action_switch(payload):
    """点账号行：程序内选中（展开任务面板），不切客户端登录。"""
    payload = payload or {}
    key = payload.get("key", "")
    if not key:
        ACTIVE["key"] = ""
        return {"ok": True, "message": "已收起", "info": {}}
    ACTIVE["key"] = key
    return {"ok": True, "message": "已选中 %s" % key, "info": {"key": key}}


def action_client_switch(payload):
    """真正切换客户端登录账号：原子替换登录文件 + 轮询校验界面昵称。"""
    payload = payload or {}
    key = (payload or {}).get("key", "")
    if not key:
        return {"ok": False, "error": "缺少 key"}
    # 串行铁则（此前完全缺失）：切号是破坏性操作——
    # 若有任务正在操作客户端，任务会把工作做到新账号头上。
    # 此处先抢令牌；切号本身耗时约 6 秒（等界面昵称变化），
    # 刻意不持有全局 LOCK，避免把整个 Web 服务阻塞住。
    if CL.holder():
        return {
            "ok": False,
            "error": "%s 正在执行中，此时切号会导致任务做到别的账号头上，请先停止任务"
            % CL.holder_text(),
            "holder": CL.holder(),
        }
    got, who = CL.try_acquire("手动切号", key)
    if not got:
        return {
            "ok": False,
            "error": "%s 正在执行中，请稍候" % who,
            "holder": CL.holder(),
        }
    try:
        r = _switch_client_impl(key)
    finally:
        # 切号无论成败都归还令牌，否则后续所有任务都会被永久拒绝
        try:
            CL.release()
        except Exception:
            pass
    return r


def _switch_client_impl(key):
    """真正执行切号：替换登录文件 + 轮询校验界面昵称。

    耗时约 6 秒（等待界面昵称变化），故不持有全局 LOCK。
    状态缓存的更新才需要锁，单独用 with LOCK 保护。
    """
    try:
        r = AS.switch_to(key, reload=False, verify=True, verify_max_sec=6)
    except Exception as e:
        return {
            "ok": False,
            "need_client": True,
            "error": "切换失败：客户端可能未打开或未带调试端口（%s: %s）"
            % (type(e).__name__, e),
            "hint": "请先启动带调试端口的客户端，或用「打开客户端登录」扫码登录后再切换",
        }
    if r.get("ok"):
        after = r.get("after") or {}

        # 切换成功后，马上更新缓存里的字段
        # 这样前端刷新时马上就能看到新账号的高亮和客户端当前登录，不用等缓存过期
        # 修复：原先整个切号过程持锁（含 6 秒等待），导致 Web 服务全程阻塞。
        # 现仅在改缓存这一瞬间持锁。
        with LOCK:
            if STATE_CACHE["data"]:
                target_key = key
                for acc in STATE_CACHE["data"]["accounts"]:
                    acc["is_current_client"] = acc["key"] == target_key
                STATE_CACHE["data"]["client_current"] = {
                    "reachable": True,
                    "key": target_key,
                    "menu": after.get("menu") or "",
                    "uid": after.get("uid") or "",
                }

        if r.get("menu_pending"):
            msg = r.get("msg") or "登录已切换，界面昵称可能稍后刷新"
        else:
            msg = "已切换并校验成功（界面昵称：%s）" % (after.get("menu") or "—")
        return {
            "ok": True,
            "message": msg,
            "error": "",
            "info": {
                k: r.get(k)
                for k in ("target", "want_uid", "before", "after", "wrote_uid")
            },
        }
    return {
        "ok": False,
        "error": r.get("err") or "切换后界面昵称未变，校验失败",
        "info": {k: r.get(k) for k in ("target", "before", "after", "wrote_uid")},
        "hint": "登录文件已写入但界面昵称未变，请稍等后点「刷新」确认",
    }


def action_sync(payload):
    """手动触发一次新账号自动入库。"""
    with LOCK:
        added, skipped = CR.auto_sync_accounts()
        found = CR.discover_accounts()
        cfg = CFG.load_config()
    _state_bust()
    dirs = CFG.auth_scan_dirs()
    return {
        "ok": True,
        "message": "新入库 %d 个账号（本机登录态共扫到 %d 个账号）"
        % (len(added), len(found)),
        "added": added,
        "skipped": skipped,
        "found": [
            {
                "uid": d["uid"],
                "nickname": d["nickname"],
                "phone": d["phone"],
                "file": d["file"],
            }
            for d in found
        ],
        "scan_dirs": dirs,
        "configured": len(cfg["accounts"]),
    }


ACTIONS = {
    "add": action_add,
    "run": action_run,
    "capture": action_capture,
    "capture-all": action_capture_all,
    "open-client": action_open_client,
    "remove": action_remove,
    "toggle": action_toggle,
    "task": action_task,
    "switch": action_switch,
    "client-switch": action_client_switch,
    "run-all": action_run_all,
    "run-account": action_run_account,
    "run-ui-code": action_run_ui_code,
    "set-mode": action_set_mode,
    "sync": action_sync,
    "stop": action_stop,
}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # 客户端在响应写出前/中已断开（刷新 / 关页 / 网络抖动）：属正常，
            # 静默忽略，避免 ConnectionAbortedError / BrokenPipeError 刷屏堆栈。
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            # 返回空的 204 No Content，避免 404 错误
            self.send_response(204)
            self.end_headers()
            return
        if path in ("/", "/ui", "/index.html"):
            try:
                with open(UI_FILE, encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "界面文件读取失败：%s" % e, "text/plain; charset=utf-8")
            return
        if path in ("/credits", "/credits.html"):
            # 完整积分台账报告（含过期批次明细 / 30 天时间轴 / 优先消耗顺序）
            try:
                from core import credits as CRE

                _p = CRE.REPORT_HTML
                if not os.path.exists(_p):
                    self._send(
                        404,
                        "还没有积分台账，请先执行 python credit_report.py 或跑一次签到",
                        "text/plain; charset=utf-8",
                    )
                    return
                with open(_p, encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "报告读取失败：%s" % e, "text/plain; charset=utf-8")
            return
        if path == "/api/state":
            try:
                self._json(build_state())
            except Exception as e:
                self._json(
                    {
                        "ok": False,
                        "error": "读取状态失败：%s: %s" % (type(e).__name__, e),
                    },
                    500,
                )
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        name = path[len("/api/") :]
        fn = ACTIONS.get(name)
        if not fn:
            self._json({"error": "未知动作 %s" % name}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            except Exception:
                payload = {}
        try:
            result = fn(payload)
            if name in ("add", "remove", "toggle", "capture", "capture-all", "sync"):
                _state_bust()
            self._json(result)
        except Exception as e:
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)


def free_port(preferred):
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


class _QuietServer(ThreadingHTTPServer):
    """客户端提前断开（刷新 / 关页 / WinError 10053）时不要刷屏堆栈。

    socketserver 默认的 handle_error 会把 ConnectionAbortedError 的完整堆栈打到控制台；
    这对"刷新页面就断连"这种正常现象太吵。这里把 OSError 系（含 ConnectionAborted /
    BrokenPipe / ConnectionReset）静默吞掉，其它异常照常打印。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):
            return
        super().handle_error(request, client_address)


def serve(port=8765, open_browser=True):
    port = free_port(port)
    if not port:
        print("[失败] 找不到可用端口")
        return 1
    # 检查是否已有 run_all 进程在跑（仅打印提示，不接管 RUN_ALL_PROC）：
    # ❗ 旧实现把 psutil.Process 对象直接赋给 RUN_ALL_PROC，而 psutil.Process 没有
    # poll() 方法，后续 run_all_status()/action_stop() 里 RUN_ALL_PROC.poll() 必崩；
    # 且 httpd 被缩进在 except ImportError 块里，psutil 可用时 httpd 未定义，
    # serve_forever() 直接 UnboundLocalError。修复：psutil 只做探测提示，
    # RUN_ALL_PROC 保持 Popen 语义，httpd 无条件创建。
    try:
        import psutil

        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if any("run_all.py" in str(arg) for arg in cmdline):
                    print(
                        "[提示] 检测到已有 run_all 进程（pid=%s），状态以日志新鲜度为准"
                        % proc.pid
                    )
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        # psutil 没装就跳过，不影响正常启动
        pass
    # 修复（2026-10-17）：启动时清掉历史停止标记。STOP_ALL 是跨进程文件，
    # 点过「停止任务」后一直留在磁盘上，导致新启动的工作台 states 里
    # stopping 恒为 True（前端出现「停止中/已停止」残留提示，像任务卡在进行中）。
    # 停止标记只对「正在跑的任务」有意义；服务刚启动时没有本进程发起的任务在跑，
    # 若恰好有 run_all 子进程在跑，它启动时会自行 clear_stop，不受影响。
    try:
        CFG.clear_stop()
    except Exception:
        pass
    httpd = _QuietServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port

    try:
        added, _ = CR.auto_sync_accounts()
        if added:
            print(
                "[%s] 自动入库 %d 个新账号：%s"
                % (CFG.now_str(), len(added), "、".join(a["label"] for a in added))
            )
    except Exception as e:
        print("[警告] 自动入库巡检启动失败：%s" % e)
    threading.Thread(target=watch_new_accounts, args=(30,), daemon=True).start()

    print("Buddy 账号管理工作台已启动：%s" % url)
    print("（只监听本机，关掉这个窗口即可停止）")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    _args = sys.argv[1:]
    _port = next((int(a) for a in _args if a.isdigit()), 8765)
    _open = "--no-open" not in _args
    serve(_port, open_browser=_open)
