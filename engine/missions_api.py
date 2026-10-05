#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 日常任务（纯接口，不碰客户端）：签到 / 成长伙伴 / 出行奖励 / 盲盒 / 猫猫旅行。

每个任务返回统一结构：{status: success|skip|fail, code, reason, data?}
- success: 实际完成了动作（或状态本来就 OK）
- skip:    良性跳过（已签/未到达/能量不足/活动结束），不算失败
- fail:    真实失败（登录态失效/接口异常/动作失败）
"""
import os
import random
import time

from core import api as A
from core import config as C
from . import tasks as T

OK = "success"
SKIP = "skip"
FAIL = "fail"


def task_checkin(cred, account=None, check_only=False):
    """Buddy加油站每日签到：查状态 → 未签才领，已签/10001 幂等跳过。"""
    code, res = A.api(cred, A.PATH_CHECKIN_STATUS)
    if A.auth_error(code, res):
        return {"status": FAIL, "code": "auth_expired",
                "reason": "登录态失效（401/403），需重新登录客户端并抓取凭证"}
    if code != 200 or res.get("code") != 0:
        return {"status": FAIL, "code": "api_error",
                "reason": "状态接口异常 http=%s biz=%s msg=%s" % (code, res.get("code"), res.get("msg", ""))}
    d = res.get("data") or {}
    info = {"active": d.get("active"), "today_checked_in": d.get("today_checked_in"),
            "streak_days": d.get("streak_days"), "total_credits": d.get("total_credits"),
            "daily_credit": d.get("daily_credit"), "end_time": d.get("end_time")}
    if not d.get("active"):
        return {"status": SKIP, "code": "activity_ended",
                "reason": "本期活动已结束（截止 %s）" % d.get("end_time"), "data": info}
    if check_only:
        return {"status": SKIP if not d.get("today_checked_in") else OK, "code": "check_only",
                "reason": "仅查询：今日%s，连续 %s 天，累计 %s 积分"
                          % ("已签" if d.get("today_checked_in") else "未签",
                             d.get("streak_days"), d.get("total_credits")), "data": info}
    if d.get("today_checked_in"):
        return {"status": OK, "code": "already_signed",
                "reason": "今日已签，连续 %s 天，累计 %s 积分" % (d.get("streak_days"), d.get("total_credits")),
                "data": info}
    code2, res2 = A.api(cred, A.PATH_CHECKIN_CLAIM)
    if A.auth_error(code2, res2):
        return {"status": FAIL, "code": "auth_expired",
                "reason": "领取时登录态失效，需重新登录客户端并抓取凭证", "data": info}
    biz = res2.get("code")
    if biz == 10001:
        return {"status": OK, "code": "already_signed",
                "reason": "接口返回已签，连续 %s 天，累计 %s 积分" % (d.get("streak_days"), d.get("total_credits")),
                "data": info}
    if code2 != 200 or biz != 0:
        return {"status": FAIL, "code": "claim_failed",
                "reason": "领取失败 http=%s biz=%s msg=%s" % (code2, biz, res2.get("msg", "")), "data": info}
    d2 = res2.get("data") or {}
    gained = d2.get("today_credit") or d.get("daily_credit") or 0
    info.update({"gained": gained,
                 "streak_days": d2.get("streak_days") or d.get("streak_days") or 0,
                 "total_credits": d2.get("total_credits") or d.get("total_credits") or 0})
    return {"status": OK, "code": "claimed",
            "reason": "签到成功 +%s 积分，连续 %s 天，累计 %s" % (info["gained"], info["streak_days"], info["total_credits"]),
            "data": info}


def task_growth_info(cred, account=None, check_only=False):
    """成长伙伴状态同步。"""
    code, res = A.api(cred, A.PATH_GROWTH_INFO, method="GET")
    if A.auth_error(code, res):
        return {"status": FAIL, "code": "auth_expired", "reason": "登录态失效（401/403）"}
    if code != 200 or res.get("code") != 0:
        return {"status": FAIL, "code": "api_error",
                "reason": "成长接口异常 http=%s biz=%s msg=%s" % (code, res.get("code"), res.get("msg", ""))}
    buddy = (res.get("data") or {}).get("buddy") or {}
    if not buddy:
        return {"status": SKIP, "code": "no_buddy", "reason": "该账号暂无成长伙伴"}
    return {"status": OK, "code": "ok",
            "reason": "成长伙伴 %s（%s）状态正常" % (buddy.get("name"), buddy.get("rarity")),
            "data": {"name": buddy.get("name"), "rarity": buddy.get("rarity"),
                     "instance_id": buddy.get("instance_id")}}


def task_growth_travel_claim(cred, account=None, check_only=False):
    """出行到达奖励：not arrived / no unclaimed 等良性返回按跳过处理。"""
    if check_only:
        return {"status": SKIP, "code": "check_only", "reason": "仅查询模式，未尝试领取"}
    code, res = A.api(cred, A.PATH_GROWTH_TRAVEL_CLAIM)
    if A.auth_error(code, res):
        return {"status": FAIL, "code": "auth_expired", "reason": "登录态失效（401/403）"}
    biz = res.get("code")
    msg = res.get("msg", "")
    BENIGN = ("not arrived", "no unclaimed", "already claimed", "already received",
              "nothing to claim", "no reward")
    if biz != 0 or code != 200:
        if any(k in msg.lower() for k in BENIGN):
            return {"status": SKIP, "code": "no_pending", "reason": "暂无可领奖励（%s）" % msg}
        return {"status": FAIL, "code": "claim_failed",
                "reason": "领取失败 http=%s biz=%s msg=%s" % (code, biz, msg)}
    return {"status": OK, "code": "claimed", "reason": "出行到达奖励领取成功"}


def task_buddy_blindbox(cred, account=None, check_only=False):
    """开盲盒：能量够才开（按配额上限），能量不足如实跳过，绝不硬开。"""
    try:
        q = A.growth_api(cred, "/activity/growth/buddy/quota")
        d = (q or {}).get("data") or {}
        cost = int(d.get("cost_per_open") or 0)
        bal = int(d.get("balance") or 0)
        affordable = int(d.get("affordable") or 0)
        max_open = int(d.get("max_open_count") or 1)
        if check_only:
            return {"status": OK, "code": "quota", "data": d,
                    "reason": "能量 %d，单次消耗 %d，当前可开 %d 次" % (bal, cost, affordable)}
        if affordable <= 0:
            return {"status": OK, "code": "not_enough_energy", "data": d,
                    "reason": "能量不足（%d/%d），本次不开盲盒" % (bal, cost)}
        opened = []
        for _ in range(min(affordable, max_open)):
            r = A.growth_api(cred, "/activity/growth/buddy/open", "POST", {})
            od = (r or {}).get("data") or {}
            opened.append({"name": (od.get("buddy") or {}).get("name") or od.get("name"),
                           "rarity": (od.get("buddy") or {}).get("rarity") or od.get("rarity"),
                           "code": r.get("code")})
            if (r or {}).get("code") not in (0, None):
                break
        return {"status": OK, "code": "opened", "data": {"opened": opened},
                "reason": "开启盲盒 %d 次（消耗 %d 能量）" % (len(opened), cost * len(opened))}
    except Exception as e:
        return {"status": FAIL, "code": "exception",
                "reason": "%s: %s" % (type(e).__name__, str(e)[:160])}


def task_buddy_travel_dispatch(cred, account=None, check_only=False):
    """派猫猫旅行：先领已到达奖励，再在「空闲且未达每日上限」时派出。"""
    def _status():
        r = A.growth_api(cred, "/activity/growth/buddy/travel/status")
        return (r or {}).get("data") or {}

    try:
        d = _status()
        state = d.get("state")
        notes = []
        if state == "arrived":
            if check_only:
                notes.append("已到达待领取（本次只查询未领取）")
            else:
                cr = A.growth_api(cred, "/activity/growth/buddy/travel/claim", "POST", {})
                notes.append("领取到达奖励（code=%s）" % (cr or {}).get("code"))
                d = _status()
                state = d.get("state")
        if state == "idle":
            if d.get("daily_limit_reached"):
                notes.append("今日派遣已达上限，跳过")
            elif check_only:
                notes.append("空闲可派（本次只查询未派遣）")
            else:
                loc = int(os.environ.get("WB_TRAVEL_LOCATION") or random.randint(1, 4))
                dr = A.growth_api(cred, "/activity/growth/buddy/travel/depart", "POST",
                                  {"location_id": loc})
                notes.append("已派往地点 %d（code=%s）" % (loc, (dr or {}).get("code")))
                d = _status()
        elif state == "traveling":
            notes.append("旅行中，等待到达（无召回接口）")
        return {"status": OK, "code": state or "unknown", "data": d,
                "reason": "；".join(notes) or "状态 %s" % state}
    except Exception as e:
        return {"status": FAIL, "code": "exception",
                "reason": "%s: %s" % (type(e).__name__, str(e)[:160])}


TASKS = {
    "checkin": ("每日签到", task_checkin),
    "growth_tasks": ("成长任务清单", None),   # 由 run_account 特判走 engine.tasks
    "growth_info": ("成长伙伴状态", task_growth_info),
    "growth_travel_claim": ("出行到达奖励", task_growth_travel_claim),
    "growth_active": ("成长热度点亮", None),  # 由 run_account 特判走 engine.active
    "buddy_blindbox": ("开启盲盒", task_buddy_blindbox),
    "buddy_travel_dispatch": ("派猫猫旅行", task_buddy_travel_dispatch),
}


def run_account(account, cfg=None, check_only=False):
    """对一个账号跑全部 API 任务（签到/伙伴/出行/热度/盲盒/猫猫 + 成长任务清单）。"""
    from core import creds as CR
    from . import tasks as T
    from . import active as ACT

    cfg = cfg or C.load_config()
    cred, err = CR.load_cred(account)
    label = "%s(%s)" % (account.get("label") or account["key"], account["key"])
    if err:
        return {"account": account["key"], "label": account.get("label", ""),
                "nickname": account.get("nickname", ""), "status": FAIL,
                "tasks": {}, "reason": err}

    exp = CR.cred_expiry_days(cred)
    results = {}
    # 阶段一标准执行顺序（最终真机确认版）：
    # 每日签到 → 开启盲盒 → 派猫猫旅行 → 出行到达奖励领取 → 成长伙伴状态检查 → 成长热度点亮 → 成长任务接取/领取
    STANDARD_ORDER = [
        "checkin",
        "buddy_blindbox",
        "buddy_travel_dispatch",
        "growth_travel_claim",
        "growth_info",
        "growth_active",
        "growth_tasks",
    ]
    account_tasks = account.get("tasks") or {}
    for name in STANDARD_ORDER:
        enabled = account_tasks.get(name, True)
        if not enabled or name not in TASKS:
            continue
        try:
            if name == "growth_tasks":
                r = T.run_tasks(cred, account, cfg=cfg,
                                do_accept=not check_only, do_claim=not check_only)
                out = {"status": r["status"], "code": "tasks_done",
                       "reason": r["reason"], "steps": r["steps"]}
                for k in ("balance_before", "balance_after", "balance_delta", "balance_error",
                          "credit_got", "skipped_paid", "skipped_other", "failed_tasks", "summary"):
                    out[k] = r.get(k)
                results[name] = out
                if not check_only:
                    from core import logs as L
                    L.save_task_state({account.get("key", "unknown"): r["tasks"]})
            elif name == "growth_active":
                results[name] = ACT.task_growth_active(cred, account=account, check_only=check_only)
            else:
                results[name] = TASKS[name][1](cred, account=account, check_only=check_only)
        except Exception as e:
            results[name] = {"status": FAIL, "code": "exception",
                             "reason": "%s: %s" % (type(e).__name__, e)}

    failed = [k for k, v in results.items() if v["status"] == FAIL]
    overall = FAIL if failed else OK
    summary = "；".join("%s:%s" % (TASKS[k][0], v["reason"]) for k, v in results.items())
    return {
        "account": account["key"], "label": account.get("label", ""),
        "nickname": (cred.get("account") or {}).get("nickname", ""),
        "status": overall, "tasks": results, "reason": summary,
        "cred_expiry_days": round(exp, 1) if exp is not None else None,
    }
