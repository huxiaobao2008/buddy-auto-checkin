#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键全自动：签到 + 成长任务 + 领积分 + 出行奖励，覆盖所有启用账号。

四阶段：
  1) 确保客户端带 CDP 调试端口（没有就拉起 client_cdp --restart）。
  2) API 管线（纯接口，不碰客户端）：签到 / 出行到达奖励 / 成长任务清单 / 成长热度。
  3) UI 类任务：逐账号切号驱动（会话型并行 + 面板型串行 + 专家团），并当场领取。
  4) 收尾领取：claim_all 扫一遍所有账号，把漏领的积分领到手。

可作为模块被 web/server 在后台线程调用（传入 progress 回调拿实时进度）。
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

from core import config as CFG
from core import logproto as LP
from engine import missions_api as MA
from engine import switch as AS
from engine import tasks as T
from . import batch as B

CDP_PORT = CFG.CDP_PORT

# 2026-09-25: 断点续传——记录今天已完成的账号，下次跳过
PROGRESS_FILE = os.path.join(CFG.LOG_DIR, "progress.json")

# 客户端占用令牌是否由本进程自行申请（决定结束时要不要释放）
_TOKEN_HELD = False


def _read_progress():
    """读今天的进度。返回 {date, done_accounts, started_at, finished_at}。"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("date") == today:
            return data
    except Exception:
        pass
    # 新的一天，或没有记录，从头开始
    return {
        "date": today,
        "done_accounts": [],
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
    }


def _write_progress(data):
    """写进度。"""
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _mark_account_done(key):
    """标记一个账号已完成（该账号所有任务均已执行完毕）。"""
    data = _read_progress()
    if key not in data["done_accounts"]:
        data["done_accounts"].append(key)
        _write_progress(data)


def _mark_task_done(key, task_code):
    """标记一个账号的某个任务已完成。"""
    data = _read_progress()
    tasks_done = data.setdefault("tasks_done", {})
    if key not in tasks_done:
        tasks_done[key] = []
    if task_code not in tasks_done[key]:
        tasks_done[key].append(task_code)
        _write_progress(data)


def _is_task_done(key, task_code):
    """检查一个账号的某个任务是否已完成。"""
    data = _read_progress()
    tasks_done = data.get("tasks_done", {})
    return key in tasks_done and task_code in tasks_done[key]


def _get_account_tasks_done(key):
    """获取一个账号已完成的任务列表。"""
    data = _read_progress()
    tasks_done = data.get("tasks_done", {})
    return tasks_done.get(key, [])


def _add_credit_today(key, n):
    """累加「今日已累计领取积分」（跨多次执行持久化在 progress.json）。

    2026-10-17 新增：汇总报告里的「本次实得」只算本轮执行；一天跑多轮时，
    后面的轮次全部 +0，用户会误以为「领了积分日志却报 0」。
    这里把每轮实得累计起来，报告同时展示「本次 / 今日累计」两个口径。"""
    if not n:
        return
    data = _read_progress()
    ct = data.setdefault("credit_today", {})
    ct[key] = int(ct.get(key) or 0) + int(n)
    _write_progress(data)


def _credit_today(key):
    """读「今日累计领取积分」。"""
    try:
        return int((_read_progress().get("credit_today") or {}).get(key) or 0)
    except Exception:
        return 0


PY = (
    CFG.load_config().get("python_path")
    or os.environ.get("PYTHON", "")
    or sys.executable
)


def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stop():
    return CFG.stop_requested()


def _switch_back_main(p, restore_key):
    """把客户端交还给「运行前正在用的那个账号」（无论 UI 任务成功/超时/异常）。"""
    if not restore_key:
        return
    try:
        back = AS.switch_to(restore_key, reload=False)
        p("ui", "已切回运行前账号 %s（ok=%s）" % (restore_key, back.get("ok")))
    except Exception as e:
        p(
            "ui",
            "切回原账号 %s 失败：%s —— 客户端可能已关闭或 CDP 不可达"
            % (restore_key, e),
        )


def ensure_client(timeout=150):
    """确保客户端带 CDP 端口启动；已开则跳过，未开则拉起。"""
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/json/version" % CDP_PORT, timeout=2)
        return True
    except Exception:
        pass
    script = os.path.join(CFG.BASE_DIR, "client_cdp.py")
    if not os.path.exists(script):
        return False
    print(
        "[%s] 客户端未带调试端口，正在拉起（restart --wait %d）…" % (_ts(), timeout),
        flush=True,
    )
    try:
        subprocess.run(
            [PY, script, "--restart", "--wait", str(timeout)],
            cwd=CFG.BASE_DIR,
            timeout=timeout + 30,
        )
    except Exception as e:
        print("[%s] 拉起客户端失败：%s" % (_ts(), e), flush=True)
        return False
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/json/version" % CDP_PORT, timeout=2)
        return True
    except Exception:
        return False


def _ui_done_for_account(account, cfg):
    """返回账号的可自动化 UI 任务完成状态：(_auto_done, _auto_total)。
    只统计 batch.UI_CODES 中的任务（排除 skip_codes + UNAUTOMATABLE_CODES）。"""
    from core import creds as CR
    from . import batch as B

    try:
        cred, _ = CR.load_cred(account)
        if not cred:
            return (0, 0)
        _m, _err = B.fetch_map(account["key"])
        if _err:
            return (0, 0)
        _skip = B._skip_codes() | set(B.UNAUTOMATABLE_CODES)
        _auto_codes = [k for k in _m.keys() if k not in _skip]
        _auto_done = sum(1 for k in _auto_codes if _m[k].get("done"))
        _auto_total = len(_auto_codes)
        return (_auto_done, _auto_total)
    except Exception:
        return (0, 0)


def order_accounts_by_need(accounts, cfg, p=None):
    """UI 管线账号排序：有可自动化 UI 任务未完成的优先；全部完成的排最后。
    只统计 batch.UI_CODES 中的任务（排除 skip_codes + UNAUTOMATABLE_CODES），
    而非全部 19 个任务——因为付费/扫码等不可自动化任务永远完不成，不应影响排序。
    9/19 的实验账号 account_r 强制排在最后。"""
    from core import creds as CR

    say = p or (lambda *a, **k: None)
    scored = []
    for a in accounts:
        auto_done, auto_total = _ui_done_for_account(a, cfg)
        pending = auto_total - auto_done
        scored.append((pending, auto_done, auto_total, a))
    # 排序：有未完成 UI 任务的优先（pending 小→大），全部完成的排最后（pending=0）
    # pending=0 时按完成数从高到低（16/16 在 0/0 前面）
    # 不硬编码任何账号顺序——全部由可自动化任务的完成情况决定（用户 2026-09-26 要求）
    scored.sort(key=lambda x: (x[0] == 0, x[0], -x[1]))
    ordered = [x[3] for x in scored]
    lines = []
    for pending, auto_done, auto_total, a in scored:
        key = a.get("key", "?")
        label = a.get("label", "")
        disp = "%s·%s" % (key, label) if label else key
        status = "已完成" if pending == 0 else "待完成 %d" % pending
        lines.append(
            "%s: UI 可自动化 %d/%d（%s）" % (disp, auto_done, auto_total, status)
        )
    say(
        "order",
        "UI 管线执行顺序（可自动化 UI 任务有未完成的优先，全部完成的排最后）：\n  "
        + "\n  ".join(lines),
    )
    return ordered


def run_all(progress=None):
    """跑完整流程。progress(stage, msg) 可选回调。返回汇总 dict。

    串行铁则：本函数是「操作客户端」的主执行体，需持有客户端占用令牌。
    - Web 启动（子进程）：父进程可能已登记令牌，这里检测到则沿用
    - 命令行直跑：自行申请令牌，跑完释放
    """
    from . import claim as CLAIM

    global _TOKEN_HELD
    # 已是持有者（Web 父进程登记过）则沿用，否则自行申请
    _TOKEN_HELD = False
    try:
        from core import client_lock as _CL

        if not _CL.holder():
            got, who = _CL.try_acquire("一键全自动", "全部启用账号 · 全流程")
            if not got:
                raise RuntimeError("%s 正在执行中，拒绝启动" % who)
            _TOKEN_HELD = True
    except ImportError:
        pass

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    logpath = os.path.join(CFG.LOG_DIR, "run_all_%s.log" % ts)
    os.makedirs(CFG.LOG_DIR, exist_ok=True)
    f = open(logpath, "a", encoding="utf-8")

    CFG.clear_stop()

    summary = {
        "ts": ts,
        "log": logpath,
        "accounts": [],
        "claim_total": 0,
        "client_ok": False,
        "ui_results": {},
        "claim_results": [],
    }

    # 每账号积分台账：本次实得积分（按阶段拆分）+ 领取到积分的任务标题
    credit_map = {}
    ui_state = {}  # key -> done / stopped / skipped_done / not_run / error:...

    def _cm(key):
        return credit_map.setdefault(
            key,
            {
                "api": 0,
                "api_tasks": [],
                "ui": 0,
                "ui_tasks": [],
                "claim": 0,
                "claim_tasks": [],
            },
        )

    def p(stage, msg, account=None):
        """输出统一格式日志行。

        修复点：原先时间戳与 area 直接由调用方拼装，且stage 允许内嵌
        ``"ui:account_x"``。现统一交给 logproto 归一化：
        - 时间戳固定 HH:MM:SS（与前端正则一致）
        - area 归入受控集合，未知值降级为 start，不再污染前端分流
        """
        area, acc = LP.normalize_stage(stage, account)
        line = LP.format_line(area, msg, account=acc)
        print(line, flush=True)
        try:
            f.write(line + "\n")
            f.flush()
        except Exception:
            pass
        if progress:
            try:
                progress(area, msg)
            except Exception:
                pass

    try:
        cfg = CFG.load_config()
        accounts = [a for a in cfg["accounts"] if a.get("enabled", True)]
        p(
            "start",
            "启用账号 %d 个：%s"
            % (len(accounts), ", ".join(a["key"] for a in accounts)),
        )
        accounts = order_accounts_by_need(accounts, cfg, p)

        # 2026-09-25: 断点续传——跳过今天已完成的账号
        prog = _read_progress()
        done_set = set(prog["done_accounts"])
        if done_set:
            p(
                "start",
                "断点续传：今天已完成 %d 个账号，跳过：%s"
                % (len(done_set), ", ".join(done_set)),
            )
            accounts = [a for a in accounts if a["key"] not in done_set]
            if not accounts:
                _ct = prog.get("credit_today") or {}
                _ct_total = sum(int(v or 0) for v in _ct.values())
                p(
                    "start",
                    "今天所有账号都已完成，直接结束（今日已累计领取 %d 积分）"
                    % _ct_total,
                )
                prog["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _write_progress(prog)
                return summary
        else:
            p("start", "今天第一次执行，从头开始")

        # 1) 客户端
        summary["client_ok"] = ensure_client()
        if summary["client_ok"]:
            p("client", "客户端已就绪（CDP 端口 %d）" % CDP_PORT)
        else:
            p(
                "client",
                "客户端未就绪（CDP %d 不可达）：UI 类任务将跳过，仅跑 API 管线"
                % CDP_PORT,
            )

        restore_key = None
        if summary["client_ok"]:
            try:
                restore_key = AS.current_key()[0]
            except Exception:
                restore_key = None

        # 2) API 管线
        p("api", "===== 阶段一：API 管线（签到 / 出行奖励 / 成长任务 / 热度）=====")
        for a in accounts:
            try:
                r = MA.run_account(a, cfg)
                st = r.get("status")
                p("api:%s" % a["key"], "%s | %s" % (st, r.get("reason")))
                gt = (r.get("tasks") or {}).get("growth_tasks") or {}

                # 2026-09-26: 为控制台流水线输出每个 API 任务的卡片状态
                def _api_task_card(code, title, status_tag):
                    p("api:%s" % a["key"], "  - %s [%s]" % (title, status_tag))

                for _tc in [
                    "checkin",
                    "buddy_blindbox",
                    "buddy_travel_dispatch",
                    "growth_travel_claim",
                    "growth_info",
                    "growth_active",
                    "growth_tasks",
                ]:
                    _tr = (r.get("tasks") or {}).get(_tc) or {}
                    _tst = _tr.get("status", "skip")
                    _tsr = str(_tr.get("reason", ""))
                    if _tst == "success":
                        _tag = (
                            "claimed"
                            if ("成功" in _tsr or "已" in _tsr or "领取" in _tsr)
                            else "done"
                        )
                    elif _tst == "skip":
                        _tag = "skip"
                    elif _tst == "fail":
                        _tag = "err"
                    else:
                        _tag = "pend"
                    _api_task_card(
                        _tc,
                        MA.TASKS.get(_tc, [_tc])[0] if _tc in MA.TASKS else _tc,
                        _tag,
                    )
                cm = _cm(a["key"])
                # 修复（2026-10-17）：每日签到的实得积分（+100/号）此前从不计入
                # 「本次实得」——签到是最主要的积分来源，导致用户看到
                # 「明明签了到/领了积分，汇总报告却全是 0」。现计入 API 侧台账。
                ck = (r.get("tasks") or {}).get("checkin") or {}
                ck_gain = int(((ck.get("data") or {}).get("gained")) or 0)
                cm["api"] = int(gt.get("credit_got") or 0) + ck_gain
                cm["api_tasks"] = [
                    s.get("title")
                    for s in (gt.get("steps") or [])
                    if s.get("step") == "claim" and "领取成功" in str(s.get("reason"))
                ]
                if ck_gain:
                    cm["api_tasks"].append("每日签到 +%d" % ck_gain)
                summary["accounts"].append(
                    {"key": a["key"], "api": st, "reason": r.get("reason")}
                )
            except Exception as e:
                p("api:%s" % a["key"], "异常：%s: %s" % (type(e).__name__, e))
                summary["accounts"].append(
                    {
                        "key": a["key"],
                        "api": "fail",
                        "reason": "%s: %s" % (type(e).__name__, e),
                    }
                )

        # 3) UI 类任务（逐账号切号驱动）
        ui_claim_total = 0
        if summary["client_ok"]:
            # 2026-09-26: 阶段二标记必须输出——前端 renderRawLog 依此把日志分流到
            # UI 区域（api/ui 两区严格分离）。这里只打阶段标记，不打账号内任务全量清单。
            p("ui", "===== 阶段二：UI 管线（CDP 驱动·成长任务）=====")
            # 每账号动态获取其真实未达标任务（2026-09-24：不再硬编码全量清单，
            # 已完成 15/19 的账号只跑剩余项，日志不再打印全部阶段标题）
            codes = None
            try:
                for a in accounts:
                    if _stop():
                        p("ui", "[停止] 收到停止指令，中止账号循环")
                        for a2 in accounts:
                            ui_state.setdefault(a2["key"], "not_run")
                        ui_state[a["key"]] = "not_run"
                        break
                    # 智能跳过：按「可自动化 UI 任务」判断该账号是否已完成
                    # 2026-09-26（用户要求）：判断单位是 UI_CODES 排除 skip/unautomatable 后的
                    # 可自动化任务，而不是全部 19 个任务。可自动化任务全部完成（pending=0），
                    # 该账号就是「已完成账号」，不切号、不驱动 UI，直接跳过留给 claim 收尾。
                    _au_done, _au_total = _ui_done_for_account(a, cfg)
                    if _au_total and _au_done >= _au_total:
                        p(
                            "ui:%s" % a["key"],
                            "可自动化 UI 任务已全部完成（%d/%d），视为已完成账号，跳过 UI 驱动（不切号）"
                            % (_au_done, _au_total),
                        )
                        ui_state[a["key"]] = "skipped_done"
                        continue
                    try:
                        out = B.run_account(
                            a["key"],
                            codes,
                            do_claim=True,
                            do_delete=False,
                            dry=False,
                            log=lambda m: p("ui:%s" % a.get("key", "?"), m),
                            stop=_stop,
                        )
                        err = out.get("err")
                        sw = (out.get("switch") or {}).get("ok")
                        ui_got = int((out.get("claim") or {}).get("credit_got") or 0)
                        ui_claim_total += ui_got
                        cm = _cm(a["key"])
                        cm["ui"] = ui_got
                        cm["ui_tasks"] = (out.get("claim") or {}).get(
                            "claimed_titles"
                        ) or []
                        ui_state[a["key"]] = (
                            ("error:%s" % err)
                            if err
                            else ("stopped" if _stop() else "done")
                        )
                        bug_list = out.get("bugs") or []
                        if bug_list:
                            summary.setdefault("bugs", {})[a["key"]] = bug_list
                            p(
                                "ui:%s" % a["key"],
                                "⚠ 完成审计发现 %d 项疑似程序BUG（已记 tasks.jsonl 并告警）：%s"
                                % (
                                    len(bug_list),
                                    "、".join(T.title_of(b["code"]) for b in bug_list),
                                ),
                            )
                        p("ui:%s" % a["key"], "切号 ok=%s | %s" % (sw, err or "完成"))
                        summary["ui_results"][a["key"]] = err or "ok"
                        # 2026-09-25: 标记这个账号已完成（不管成功还是失败，都算跑完了）
                        # 2026-09-26: 判断口径改为「可自动化 UI 任务」——全部完成即视为已完成账号，
                        # 可标记断点续传；付费/扫码等不可自动化任务不计入，避免永远标不完。
                        try:
                            _au_done2, _au_total2 = _ui_done_for_account(a, cfg)
                            if _au_total2 and _au_done2 >= _au_total2:
                                _mark_account_done(a["key"])
                        except Exception:
                            pass

                        # 输出最终完成情况（补做逻辑已在run_account内部自动完成，全部计分后才会退出）
                        _skip = B._skip_codes() | set(B.UNAUTOMATABLE_CODES)
                        _m, _err = B.fetch_map(a["key"])
                        if not _err:
                            _auto_codes = [k for k in _m.keys() if k not in _skip]
                            _auto_done = sum(
                                1 for k in _auto_codes if _m[k].get("done")
                            )
                            _auto_total = len(_auto_codes)
                            p(
                                "ui:%s" % a["key"],
                                "能自动完成的任务：%d/%d" % (_auto_done, _auto_total),
                            )
                    except Exception as e:
                        p("ui:%s" % a["key"], "异常：%s: %s" % (type(e).__name__, e))
                        ui_state[a["key"]] = "error:%s: %s" % (
                            type(e).__name__,
                            str(e)[:80],
                        )
                        summary["ui_results"][a["key"]] = "异常：%s" % e
            finally:
                _switch_back_main(p, restore_key)
        else:
            p("ui", "跳过 UI 类任务（客户端未就绪）")

        # 4) 收尾领取
        p("claim", "===== 阶段三：收尾领取（claim_all，领所有可领积分）=====")
        claim_all_total = 0
        for a in accounts:
            if _stop():
                p("claim", "[停止] 收到停止指令，中止收尾领取")
                break
            try:
                r = CLAIM.claim_account(a, cfg, quiet=True)
                got = int(r.get("credit_got") or 0)
                claim_all_total += got
                cm = _cm(a["key"])
                cm["claim"] = got
                cm["claim_tasks"] = r.get("claimed_titles") or []
                summary["claim_results"].append(
                    {
                        "key": a["key"],
                        "claimed": r.get("claimed"),
                        "credit_got": got,
                        "status": r.get("status"),
                    }
                )
                p(
                    "claim:%s" % a["key"],
                    "领取 %s 项 | 本次到账 %s 积分" % (r.get("claimed"), got),
                )
            except Exception as e:
                p("claim:%s" % a["key"], "异常：%s: %s" % (type(e).__name__, e))
                summary["claim_results"].append(
                    {
                        "key": a["key"],
                        "credit_got": 0,
                        "status": "fail",
                        "reason": str(e)[:200],
                    }
                )
        # 5) 汇总报告：无论正常结束还是手动停止，都报告每账号实得积分与剩余任务清单
        stopped = _stop()
        p(
            "report",
            "===== 本次执行汇总报告%s =====" % ("（被手动停止）" if stopped else ""),
        )
        from core import creds as CR2

        for a in accounts:
            key = a["key"]
            cm = _cm(key)
            total_got = cm["api"] + cm["ui"] + cm["claim"]
            label = a.get("label") or a.get("nickname") or key
            # 剩余任务清单（实时的服务端状态）
            done_cnt = total_cnt = None
            remaining = []
            try:
                cred, _ = CR2.load_cred(a)
                if cred:
                    tl, _e = T.fetch_tasks(cred, cfg)
                    if not _e:
                        ts3 = T.summarize(tl)
                        done_cnt, total_cnt = ts3.get("done"), ts3.get("total")
                        for t in tl:
                            pgs = t.get("progress") or {}
                            if t.get("status") in ("claimed", "completed"):
                                continue
                            if (
                                pgs.get("target")
                                and (pgs.get("current") or 0) >= pgs["target"]
                            ):
                                continue
                            remaining.append(
                                "%s（%s/%s）"
                                % (
                                    t.get("title") or T.title_of(t.get("code")),
                                    pgs.get("current"),
                                    pgs.get("target"),
                                )
                            )
            except Exception as e:
                remaining = ["（状态读取失败：%s）" % str(e)[:50]]
            got_tasks = cm["api_tasks"] + cm["ui_tasks"] + cm["claim_tasks"]
            # 累计「今日已领」后再报告：一天跑多轮时，后几轮「本次实得 +0」是
            # 正常的（前面轮次已领），必须把今日累计一起报出来，否则用户看到
            # 全 0 会误以为「领了积分日志却报 0」（2026-10-17 用户反馈）。
            _add_credit_today(key, total_got)
            today_got = _credit_today(key)
            today_txt = (
                "；今日累计 +%d" % today_got if today_got != total_got else ""
            )
            p(
                "report:%s" % key,
                "%s：本次实得 +%d 积分（API %d / UI %d / 收尾 %d）%s"
                % (label, total_got, cm["api"], cm["ui"], cm["claim"], today_txt),
            )
            if got_tasks:
                p(
                    "report:%s" % key,
                    "  领取到积分的任务：%s"
                    % "、".join(T.title_of(c) for c in got_tasks),
                )
            bug_list = (summary.get("bugs") or {}).get(key) or []
            if bug_list:
                p(
                    "report:%s" % key,
                    "  ⚠ 疑似程序BUG %d 项（已告警）：%s"
                    % (
                        len(bug_list),
                        "、".join(
                            "%s（进度 %s）"
                            % (T.title_of(b["code"]), b.get("progress", "-"))
                            for b in bug_list
                        ),
                    ),
                )
            if done_cnt is not None:
                p(
                    "report:%s" % key,
                    "  任务进度 %s/%s；未完成 %d 项：%s"
                    % (
                        done_cnt,
                        total_cnt,
                        len(remaining),
                        "、".join(remaining[:6]) + ("…" if len(remaining) > 6 else ""),
                    ),
                )
            state_txt = {
                "done": "完成",
                "stopped": "做任务途中被手动停止",
                "not_run": "未执行（手动停止）",
                "skipped_done": "已全部完成，跳过",
            }
            if not summary["client_ok"]:
                st_disp = "未执行（客户端未就绪）"
            else:
                st_key = ui_state.get(key, "not_run" if stopped else "done")
                if st_key in state_txt:
                    st_disp = state_txt[st_key]
                elif st_key.startswith("error:"):
                    st_disp = "异常：%s" % st_key[6:]
                else:
                    st_disp = st_key
            p("report:%s" % key, "  状态：%s" % st_disp)
        summary["credit_map"] = credit_map
        summary["ui_state"] = ui_state
        summary["stopped"] = stopped

        summary["claim_total"] = ui_claim_total + claim_all_total
        summary["ui_claim_total"] = ui_claim_total
        summary["claim_all_total"] = claim_all_total
        _today_all = sum(
            int(v or 0)
            for v in ((_read_progress().get("credit_today") or {}).values())
        )
        p(
            "done",
            "全部完成。阶段二领取 %d 积分，阶段三领取 %d 积分，本次共领取 %d 积分（今日累计 %d）。日志：%s"
            % (ui_claim_total, claim_all_total, summary["claim_total"], _today_all, logpath),
        )
        # 2026-09-25: 写 finished_at，表示今天全部跑完了
        prog = _read_progress()
        prog["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_progress(prog)
        p("done", "已写进度：今天全部完成，下次再跑会直接跳过")

        # 5.5) 积分台账：签到后自动刷新「可用积分 + 过期倒计时」。
        # 纯旁路——用各账号凭证快照直接调官方 billing 接口，不需要切号、不碰客户端；
        # 任何失败只记一行日志，绝不影响本次签到的结果与退出码。
        try:
            from core import credits as CRE

            def _clog(msg):
                p("report", msg)

            CRE.snapshot(accounts, log=_clog)
        except Exception as e:
            p(
                "report",
                "积分台账刷新失败（不影响本次签到）：%s: %s" % (type(e).__name__, e),
            )
        return summary
    finally:
        try:
            f.close()
        except Exception:
            pass
        # 归还客户端占用令牌（串行铁则）：
        # run_all 可能是 Web 启动的子进程（令牌已由它持有），必须在此释放，
        # 否则 Web 层会一直显示被占用，后续任务全被拒绝。
        # 命令行直跑时 _TOKEN_HELD 为 False，不影响他人。
        if _TOKEN_HELD:
            try:
                from core import client_lock as _CL

                _CL.release()
            except Exception:
                pass


def main():
    run_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
