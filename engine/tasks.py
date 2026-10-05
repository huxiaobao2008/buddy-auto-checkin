#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成长任务引擎：拉取 → 判定 → 接取 → 领取 → 回写（串行锁 + 重试 + 结构化日志）。

判定规则（与旧实现行为一致）：
  - 付费任务（paid_tasks 名单/关键词：捐款/公益/充值…）一票否决：不接取、不领取、不扣费
  - 不执行任务（skip_tasks：如 black_cat 夜猫子无积分奖励）同样不接不领
  - 已领取/已锁定/已过期 → 跳过；already_claimed / not completed → 按跳过不算失败
  - 进度未达标 → 跳过并说明「需先在客户端完成」（程序不伪造进度）
  - 仅网络异常(-1)与 5xx 重试；单任务失败不影响其它任务

状态机：
  not_accepted --accept--> accepted --(进度达标)--> in_progress/completed --claim--> claimed
"""

import re
import threading
import time
from datetime import datetime

from core import api as A
from core import config as C
from core import logs as L

SERIAL_LOCK = threading.Lock()

STATUS_LABEL = {
    "not_accepted": "未接取",
    "accepted": "已接取",
    "in_progress": "进行中",
    "completed": "待领取",
    "claimed": "已领取",
}

BENIGN_CLAIM_MSG = (
    "not completed",
    "already",
    "claimed",
    "not arrived",
    "expired",
    "locked",
)

DEFAULT_PAID_CODES = ["Expert_Philanthropy"]
DEFAULT_PAID_KEYWORDS = [
    "捐款",
    "捐赠",
    "捐助",
    "献爱心",
    "公益",
    "付费",
    "充值",
    "支付",
    "购买",
    "消费",
    "下单",
    "开通会员",
    "¥",
]
DEFAULT_SKIP_CODES = ["black_cat"]
DEFAULT_SKIP_KEYWORDS = []

# 服务端任务中文名（对齐客户端「全部任务」页展示的服务端标题）。
# 优先用接口实时返回的 title；本表用于 title 缺失/离线回退等场景的显示兜底。
# 来源：服务端 growth tasks 接口 2026-09 实测返回（logs/latest.json）。
CODE_TITLES = {
    "template_5": "使用 5 个模板",
    "expert_5": "召唤 5 次专家",
    "chat_5": "和 AI 聊天 5 次",
    "skill_1": "尝鲜热门技能",
    "automation_1": "设置自动化任务",
    "first_buddy": "领取一只 Buddy",
    "RichMeow_Chat": "桌面端对话1次",
    "create_canvas": "体验「设计创意模式」",
    "playbook_prompt": "探索优秀灵感",
    "Library_read": "体验「资料库」",
    "Expert_lighthouse": "体验「腾讯轻量云」专家",
    "Expert_Philanthropy": "体验「公益专家」",
    "Expert_team_use_3": "召唤 3 次专家团",
    "Hp_Appearance": "体验「和平精英」主题",
    "Buddy_App": "体验 WorkBuddy 「发现应用」",
    "Buddy_App_QQ": "体验「企鹅教师助手」Buddy 应用",
    "Model_chat_GLM5.2": "体验「GLM-5.2」模型",
    "black_cat": "参与「夜猫子」夜间折扣活动",
    "wb_wechat_oa_subscribe_task": "关注「腾讯 WorkBuddy」官方公众号",
    # 内部阶段代码（审计/日志用）
    "chat_phase": "会话型任务阶段",
    "panel_phase": "面板型任务阶段",
}


def title_of(code):
    """任务显示名：优先接口实时中文名，其次 CODE_TITLES 兜底，最后原样返回 code。"""
    if not code:
        return ""
    t = CODE_TITLES.get(code)
    return t if t else code


def paid_rule(cfg):
    block = (cfg or {}).get("paid_tasks") or {}
    codes = [c.lower() for c in (block.get("codes") or DEFAULT_PAID_CODES)]
    keywords = list(block.get("keywords") or DEFAULT_PAID_KEYWORDS)
    return codes, keywords


def detect_paid(raw, cfg=None):
    codes, keywords = paid_rule(cfg)
    code = (raw.get("task_code") or "").lower()
    if code in codes:
        return True, "命中付费任务名单（%s）" % raw.get("task_code")
    text = " ".join(
        str(raw.get(k) or "")
        for k in ("title", "task_desc", "description", "badge_name")
    )
    for kw in keywords:
        if kw in text:
            return True, "任务文案含「%s」，判定为需付费/捐款" % kw
    return False, ""


def skip_rule(cfg):
    block = (cfg or {}).get("skip_tasks") or {}
    codes = [c.lower() for c in (block.get("codes") or DEFAULT_SKIP_CODES)]
    keywords = list(block.get("keywords") or DEFAULT_SKIP_KEYWORDS)
    return codes, keywords


def detect_skip(raw, cfg=None):
    codes, keywords = skip_rule(cfg)
    code = (raw.get("task_code") or "").lower()
    if code in codes:
        return True, "按用户要求不执行该任务"
    text = " ".join(
        str(raw.get(k) or "")
        for k in ("title", "task_desc", "description", "badge_name")
    )
    for kw in keywords:
        if kw and kw in text:
            return True, "任务文案含「%s」，按用户要求不执行" % kw
    return False, ""


def _expired(valid_end):
    if not valid_end:
        return False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(valid_end)[:19], fmt) < datetime.now()
        except Exception:
            continue
    return False


def normalize(raw, cfg=None):
    """把接口原始任务对象整理成前后端共用结构（页面与执行结果以这一份为准）。"""
    prog = raw.get("progress") or {}
    cur, tgt = prog.get("current"), prog.get("target")
    status = raw.get("accept_status") or "not_accepted"
    expired = _expired(raw.get("valid_end"))
    locked = bool(raw.get("locked"))
    paid, paid_reason = detect_paid(raw, cfg)
    skip, skip_reason = detect_skip(raw, cfg)
    progress_done = bool(tgt and cur is not None and cur >= tgt)
    claimable = (
        (not locked)
        and (not expired)
        and (not paid)
        and (not skip)
        and status != "claimed"
        and (status == "completed" or progress_done)
    )
    item = {
        "code": raw.get("task_code"),
        "title": raw.get("title") or title_of(raw.get("task_code")),
        "desc": raw.get("task_desc") or raw.get("description") or "",
        "status": status,
        "status_label": STATUS_LABEL.get(status, status),
        "done": status == "claimed",
        "progress": {"current": cur, "target": tgt},
        "progress_text": "%s/%s" % (cur, tgt) if (cur is not None and tgt) else "—",
        "reward_credit": raw.get("reward_credit") or 0,
        "reward_energy": raw.get("reward_energy") or 0,
        "locked": locked,
        "expired": expired,
        "valid_end": raw.get("valid_end"),
        "claimable": claimable,
        "accept_needed": status == "not_accepted" and not paid and not skip,
        "paid": paid,
        "paid_reason": paid_reason,
        "skip": skip,
        "skip_reason": skip_reason,
        "jump_url": raw.get("jump_url") or "",
    }
    if paid:
        item["status_label"] = "已跳过（需付费）"
    elif skip:
        item["status_label"] = "已跳过（按要求不执行）"
    return item


def summarize(tasks):
    done = [t for t in tasks if t["done"]]
    undone = [t for t in tasks if not t["done"]]
    return {
        "total": len(tasks),
        "done": len(done),
        "undone": len(undone),
        "paid_skipped": sum(1 for t in tasks if t.get("paid") and not t["done"]),
        "pending_credit": sum(t["reward_credit"] for t in tasks if t.get("claimable")),
        "potential_credit": sum(
            t["reward_credit"]
            for t in undone
            if not t["locked"] and not t["expired"] and not t.get('paid') and not t.get('skip')
        ),
        "claimable": sum(1 for t in tasks if t["claimable"]),
    }


def fetch_tasks(cred, cfg=None):
    """返回 (tasks_normalized, err)。"""
    attempts, backoff = A.retry_params(cfg)
    code, res = A.retry(
        lambda: A.api(cred, A.PATH_GROWTH_TASKS, method="GET"), attempts, backoff
    )
    if A.auth_error(code, res):
        return [], "登录态失效（401/403）"
    if code != 200 or res.get("code") != 0:
        return [], "任务列表拉取失败 http=%s biz=%s msg=%s" % (
            code,
            res.get("code"),
            res.get("msg", ""),
        )
    raw = ((res.get("data") or {}).get("tasks")) or []
    return [normalize(t, cfg) for t in raw], ""


def fetch_profile(cred, cfg=None):
    code, res = A.api(cred, A.PATH_GROWTH_PROFILE, method="GET")
    if code != 200 or res.get("code") != 0:
        return {}, "成长档案拉取失败 http=%s biz=%s" % (code, res.get("code"))
    d = res.get("data") or {}
    return {
        "level": d.get("level"),
        "completed": d.get("completed"),
        "total": d.get("total"),
    }, ""


def accept_task(cred, code, cfg=None):
    attempts, backoff = A.retry_params(cfg)
    c, r = A.retry(
        lambda: A.api(
            cred, A.PATH_GROWTH_ACCEPT, method="POST", body={"task_codes": [code]}
        ),
        attempts,
        backoff,
    )
    if A.auth_error(c, r):
        return False, "登录态失效（401/403）", "auth_expired"
    if c != 200 or r.get("code") != 0:
        return (
            False,
            "接取失败 http=%s biz=%s msg=%s" % (c, r.get("code"), r.get("msg", "")),
            "api_error",
        )
    results = ((r.get("data") or {}).get("results")) or []
    st = results[0].get("status") if results else "accepted"
    return True, "已接取（%s）" % st, "accepted"


def claim_task(cred, code, cfg=None):
    attempts, backoff = A.retry_params(cfg)
    c, r = A.retry(
        lambda: A.api(cred, A.PATH_GROWTH_CLAIM.format(code=code), method="POST"),
        attempts,
        backoff,
    )
    if A.auth_error(c, r):
        return False, "登录态失效（401/403）", "auth_expired"
    if c != 200 or r.get("code") != 0:
        msg = (r.get("msg") or "").lower()
        if any(k in msg for k in BENIGN_CLAIM_MSG):
            return None, "跳过：%s" % r.get("msg"), "no_pending"
        return (
            False,
            "领取失败 http=%s biz=%s msg=%s" % (c, r.get("code"), r.get("msg", "")),
            "api_error",
        )
    d = r.get("data") or {}
    if d.get("already_claimed"):
        return None, "跳过：已领取过", "already_claimed"
    return True, "领取成功 +%s 积分" % d.get("credit", 0), "claimed"


def query_credits(cred):
    """只查询积分与签到状态，不领取。失败返回 err 字段而不是抛异常。"""
    try:
        code, res = A.api(cred, A.PATH_CHECKIN_STATUS)
        if A.auth_error(code, res):
            return {"err": "登录态失效（401/403）"}
        if code != 200 or res.get("code") != 0:
            return {"err": "状态接口异常 http=%s biz=%s" % (code, res.get("code"))}
        d = res.get("data") or {}
        return {
            "err": "",
            "signed": d.get("today_checked_in"),
            "streak_days": d.get("streak_days"),
            "total_credits": d.get("total_credits"),
            "daily_credit": d.get("daily_credit"),
            "active": d.get("active"),
            "end_time": d.get("end_time"),
        }
    except Exception as e:
        return {"err": "%s: %s" % (type(e).__name__, e)}


def _to_float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def query_balance(cred):
    """查询账户剩余额度（Packages[].CycleRemainCapacity 合计）。"""
    try:
        code, res = A.api(cred, A.PATH_RESOURCE_SUMMARY)
    except Exception as e:
        return {"err": "%s: %s" % (type(e).__name__, e)}
    if A.auth_error(code, res):
        return {"err": "登录态失效（401/403）"}
    if code != 200 or res.get("code") != 0:
        return {
            "err": "余额接口异常 http=%s biz=%s msg=%s"
            % (code, res.get("code"), res.get("msg", ""))
        }
    data = res.get("data") or {}
    packages, remain, total, used = [], 0.0, 0.0, 0.0
    for p in data.get("Packages") or []:
        r = _to_float(p.get("CycleRemainCapacity"))
        t = _to_float(p.get("CycleTotalCapacity"))
        u = _to_float(p.get("CycleUsedCapacity"))
        remain += r
        total += t
        used += u
        packages.append(
            {
                "code": p.get("PackageCode", ""),
                "remain": r,
                "total": t,
                "used": u,
                "unit": p.get("CapacityUnit", "credits"),
            }
        )
    return {
        "err": "",
        "remaining": remain,
        "total": total,
        "used": used,
        "is_paid_user": bool(data.get("IsPaidUser")),
        "packages": packages,
    }


def run_tasks(cred, account, cfg=None, do_accept=True, do_claim=True):
    """串行执行一个账号的成长任务（接取+领取）。返回结构化结果。"""
    key = account["key"]
    nick = account.get("nickname") or account.get("label") or key
    steps = []
    skipped_paid, skipped_other, failed_list = [], [], []

    bal_before = query_credits(cred)
    before_val = bal_before.get("total_credits")
    if bal_before.get("err") or before_val is None:
        L.log_step(
            key,
            nick,
            "-",
            "-",
            "balance",
            "fail",
            "执行前积分余额获取失败：%s" % (bal_before.get("err") or "返回为空"),
        )

    tasks, err = fetch_tasks(cred, cfg)
    if err:
        L.log_step(key, nick, "-", "-", "fetch", "fail", err)
        return {
            "status": "fail",
            "reason": err,
            "steps": [{"task": "-", "step": "fetch", "status": "fail", "reason": err}],
            "tasks": [],
            "summary": {},
            "profile": {},
            "balance_before": before_val,
            "balance_after": None,
            "balance_delta": None,
            "balance_error": bal_before.get("err") or "",
            "credit_got": 0,
            "skipped_paid": [],
            "skipped_other": [],
            "failed_tasks": [],
        }

    profile, perr = fetch_profile(cred, cfg)
    claimed_cnt = credit_got = accepted_cnt = failed = 0
    for t in tasks:
        code_, title = t["code"], t["title"]
        try:
            if t.get("paid"):
                reason = "已跳过（需付费）：%s" % t.get("paid_reason")
                steps.append(
                    {
                        "task": code_,
                        "title": title,
                        "step": "skip_paid",
                        "status": "skip",
                        "reason": reason,
                    }
                )
                L.log_step(key, nick, code_, title, "skip_paid", "skip", reason)
                skipped_paid.append({"task": code_, "title": title, "reason": reason})
                continue
            if t.get("skip"):
                reason = "按用户要求不执行：%s" % (t.get("skip_reason") or "")
                steps.append(
                    {
                        "task": code_,
                        "title": title,
                        "step": "skip_required",
                        "status": "skip",
                        "reason": reason,
                    }
                )
                L.log_step(key, nick, code_, title, "skip_required", "skip", reason)
                skipped_other.append({"task": code_, "title": title, "reason": reason})
                continue
            if t["locked"]:
                steps.append(
                    {
                        "task": code_,
                        "title": title,
                        "step": "skip",
                        "status": "skip",
                        "reason": "任务已锁定",
                    }
                )
                L.log_step(key, nick, code_, title, "skip", "skip", "任务已锁定")
                skipped_other.append(
                    {"task": code_, "title": title, "reason": "任务已锁定"}
                )
                continue
            if t["expired"]:
                reason = "已过期（截止 %s）" % t["valid_end"]
                steps.append(
                    {
                        "task": code_,
                        "title": title,
                        "step": "skip",
                        "status": "skip",
                        "reason": reason,
                    }
                )
                L.log_step(key, nick, code_, title, "skip", "skip", reason)
                skipped_other.append({"task": code_, "title": title, "reason": reason})
                continue
            if t["done"]:
                steps.append(
                    {
                        "task": code_,
                        "title": title,
                        "step": "skip",
                        "status": "skip",
                        "reason": "已领取，不重复提交",
                    }
                )
                L.log_step(key, nick, code_, title, "skip", "skip", "已领取")
                continue

            status_now = t["status"]
            if do_accept and t["accept_needed"]:
                ok, msg, _c2 = accept_task(cred, code_, cfg)
                steps.append(
                    {
                        "task": code_,
                        "title": title,
                        "step": "accept",
                        "status": "success"
                        if ok
                        else ("fail" if ok is False else "skip"),
                        "reason": msg,
                    }
                )
                L.log_step(
                    key,
                    nick,
                    code_,
                    title,
                    "accept",
                    "success" if ok else ("fail" if ok is False else "skip"),
                    msg,
                )
                if ok is False:
                    failed += 1
                    failed_list.append({"task": code_, "title": title, "reason": msg})
                    continue
                if ok:
                    accepted_cnt += 1
                    status_now = "accepted"
                    # 接取后进度可能立即刷新；重拉一次用新进度判 claimable，防漏领
                    lst2, _e2 = fetch_tasks(cred, cfg)
                    if not _e2:
                        t2 = next((x for x in lst2 if x["code"] == code_), None)
                        if t2:
                            t = t2

            claimable = status_now == "completed" or (
                t["progress"]["target"]
                and (t["progress"]["current"] or 0) >= t["progress"]["target"]
            )
            if do_claim and claimable:
                ok, msg, _c2 = claim_task(cred, code_, cfg)
                st = "success" if ok else ("fail" if ok is False else "skip")
                steps.append(
                    {
                        "task": code_,
                        "title": title,
                        "step": "claim",
                        "status": st,
                        "reason": msg,
                    }
                )
                L.log_step(key, nick, code_, title, "claim", st, msg)
                if ok:
                    claimed_cnt += 1
                    m = re.search(r"\+\s*(\d+)", msg or "")
                    if m:
                        credit_got += int(m.group(1))
                    else:
                        L.log_step(
                            key,
                            nick,
                            code_,
                            title,
                            "claim",
                            "warn",
                            "领取成功但无法从文案解析积分：%r" % msg,
                        )
                elif ok is False:
                    failed += 1
                    failed_list.append({"task": code_, "title": title, "reason": msg})
            elif not claimable:
                reason = "未完成（%s），需先在客户端完成该操作" % t["progress_text"]
                steps.append(
                    {
                        "task": code_,
                        "title": title,
                        "step": "skip",
                        "status": "skip",
                        "reason": reason,
                    }
                )
                L.log_step(key, nick, code_, title, "skip", "skip", reason)
                skipped_other.append({"task": code_, "title": title, "reason": reason})
        except Exception as e:
            reason = "%s: %s" % (type(e).__name__, e)
            steps.append(
                {
                    "task": code_,
                    "title": title,
                    "step": "error",
                    "status": "fail",
                    "reason": reason,
                }
            )
            L.log_step(key, nick, code_, title, "error", "fail", reason)
            failed += 1
            failed_list.append({"task": code_, "title": title, "reason": reason})
            continue

    fresh, ferr = fetch_tasks(cred, cfg)
    if ferr:
        fresh = tasks

    bal_after = query_credits(cred)
    after_val = bal_after.get("total_credits")
    balance_error = ""
    if bal_after.get("err") or after_val is None:
        balance_error = "执行后积分余额获取失败：%s" % (
            bal_after.get("err") or "返回为空"
        )
        L.log_step(key, nick, "-", "-", "balance", "fail", balance_error)
    delta = None
    if before_val is not None and after_val is not None:
        delta = after_val - before_val

    profile2, _ = fetch_profile(cred, cfg)
    if profile2:
        profile = profile2

    summary = summarize(fresh)
    summary.update(
        {
            "claimed": claimed_cnt,
            "accepted": accepted_cnt,
            "credit_got": credit_got,
            "failed": failed,
            "skipped_paid": len(skipped_paid),
            "skipped_other": len(skipped_other),
        }
    )
    reason = (
        "成长任务 %s/%s 已完成；本次接取 %s 个、领取 %s 个（+%s 积分）、跳过付费 %s 个"
        % (
            summary["done"],
            summary["total"],
            accepted_cnt,
            claimed_cnt,
            credit_got,
            len(skipped_paid),
        )
        + ("、失败 %s 个" % failed if failed else "")
    )
    return {
        "status": "fail"
        if failed and claimed_cnt == 0 and accepted_cnt == 0
        else "success",
        "reason": reason,
        "steps": steps,
        "tasks": fresh,
        "summary": summary,
        "profile": profile,
        "profile_error": perr,
        "balance_before": before_val,
        "balance_after": after_val,
        "balance_delta": delta,
        "balance_error": balance_error,
        "credit_got": credit_got,
        "skipped_paid": skipped_paid,
        "skipped_other": skipped_other,
        "failed_tasks": failed_list,
    }


def run_account_tasks(account, cfg=None, wait_lock=True):
    """带锁入口：拿不到锁返回 busy。"""
    if not SERIAL_LOCK.acquire(
        blocking=bool(wait_lock), timeout=120 if wait_lock else None
    ):
        return {
            "status": "fail",
            "reason": "已有执行在进行中（串行保护），请稍后重试",
            "steps": [],
            "tasks": [],
            "summary": {},
            "profile": {},
        }
    try:
        from core import creds as CR

        cred, err = CR.load_cred(account)
        if err:
            return {
                "status": "fail",
                "reason": err,
                "steps": [],
                "tasks": [],
                "summary": {},
                "profile": {},
            }
        return run_tasks(cred, account, cfg)
    finally:
        SERIAL_LOCK.release()
