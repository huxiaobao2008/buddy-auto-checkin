#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""积分台账：查询可用积分与过期倒计时、记录历史、渲染报告。

设计原则：
  - 纯旁路模块。被签到主流程（orchestrator.run_all / auto_buddy.cmd_run）在收尾时
    调用，任何异常都只记录、绝不向上抛，绝不影响签到本身的执行结果。
  - 只依赖 core.*（api/creds/config），不反向依赖 orchestrator / web，避免循环导入。

接口来源（实测确认）：
  POST /billing/meter/get-user-resource-summary        总可用额度 / 分类
  POST /billing/meter/get-user-resource-free-packages  资源包明细（含过期时间）
  POST /billing/meter/get-user-resource-paid-packages  付费包明细
  POST /v2/billing/meter/checkin-activity-status       签到积分与活动期
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime

from . import api as A
from . import config as C
from . import creds as CR

PATH_FREE = "/billing/meter/get-user-resource-free-packages"
PATH_PAID = "/billing/meter/get-user-resource-paid-packages"

REPORT_JSON = os.path.join(C.LOG_DIR, "credits_report.json")
REPORT_HTML = os.path.join(C.LOG_DIR, "credits_report.html")
HISTORY_JSONL = os.path.join(C.LOG_DIR, "credits_history.jsonl")

DAY = 86400.0
FAR = 9e18  # 无期限


def f(v, default=0.0):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def parse_time(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def ms_to_dt(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0)
    except (TypeError, ValueError):
        return None


def eff_expire(acc):
    """实际失效时间：ExpiredTime 与 CycleEndTime 取更早者（ExpiredTime 常为空）。"""
    cands = []
    for v in (
        acc.get("ExpiredTime"),
        acc.get("CycleEndTime"),
        ms_to_dt(acc.get("DeductionEndTime")),
    ):
        d = v if isinstance(v, datetime) else parse_time(v)
        if d:
            cands.append(d)
    return min(cands) if cands else None


def human(secs):
    if secs is None or secs > 8e17:
        return "无期限"
    if secs <= 0:
        return "已过期"
    d = int(secs // DAY)
    h = int((secs % DAY) // 3600)
    m = int((secs % 3600) // 60)
    return "%d天%d小时%d分" % (d, h, m)


def band(secs):
    if secs > 8e17:
        return "far"
    if secs <= DAY:
        return "danger"
    if secs <= 3 * DAY:
        return "warn"
    if secs <= 7 * DAY:
        return "soon"
    if secs <= 30 * DAY:
        return "month"
    return "far"


def fetch_packages(cred, pkg_codes):
    """拉取资源包明细（免费包 + 付费包），分页拉全。"""
    out = []
    for path in (PATH_FREE, PATH_PAID):
        page = 1
        while page <= 20:
            body = {"PackageCodes": pkg_codes, "PageNumber": page, "PageSize": 200}
            try:
                code, res = A.api(cred, path, method="POST", body=body, timeout=25)
            except Exception:
                break
            if A.auth_error(code, res):
                raise RuntimeError("登录态失效（401/403）")
            if code != 200 or res.get("code") != 0:
                break
            d = res.get("data") or {}
            accs = d.get("Accounts") or []
            out.extend(accs)
            total = int(d.get("TotalCount") or 0)
            if not accs or len(out) >= total or len(accs) < 200:
                break
            page += 1
    return out


def query_account(acct):
    """查单个账号的积分与过期分布。返回 dict（含 err 字段表示失败原因）。"""
    rec = {
        "key": acct.get("key"),
        "label": acct.get("label") or acct.get("key"),
        "nickname": acct.get("nickname") or "",
        "uid": acct.get("uid") or "",
        "err": "",
        "ts": C.now_str(),
    }
    cred, err = CR.load_cred(acct)
    if err:
        rec["err"] = err
        return rec

    code, res = A.api(cred, A.PATH_RESOURCE_SUMMARY)
    if A.auth_error(code, res):
        rec["err"] = "登录态失效（401/403）"
        return rec
    if code != 200 or res.get("code") != 0:
        rec["err"] = "额度接口异常 http=%s biz=%s" % (code, res.get("code"))
        return rec
    d = res.get("data") or {}
    rec["total_remain"] = f(d.get("TotalCredits"))
    rec["is_paid_user"] = bool(d.get("IsPaidUser"))
    rec["plan_name"] = d.get("SubscriptionPackageName", "")
    rec["credit_groups"] = [
        {
            "name": g.get("Name"),
            "kind": g.get("Kind"),
            "remain": f(g.get("CycleRemainCapacity")),
            "total": f(g.get("CycleTotalCapacity")),
            "used": f(g.get("CycleUsedCapacity")),
        }
        for g in (d.get("CreditGroups") or [])
        if f(g.get("CycleTotalCapacity")) or f(g.get("CycleRemainCapacity"))
    ]
    pkg_codes = [
        p.get("PackageCode") for p in (d.get("Packages") or []) if p.get("PackageCode")
    ]

    pkgs = fetch_packages(cred, pkg_codes) if pkg_codes else []
    now = datetime.now()
    buckets = defaultdict(lambda: {"amount": 0.0, "count": 0, "names": set()})
    alive_sum = 0.0
    expired_lost = 0.0
    expired_lost_count = 0
    rows = []
    for p in pkgs:
        remain = f(p.get("CycleCapacityRemainPrecise") or p.get("CycleCapacityRemain"))
        size = f(p.get("CycleCapacitySizePrecise") or p.get("CycleCapacitySize"))
        exp = eff_expire(p)
        status = p.get("Status")
        secs = (exp - now).total_seconds() if exp else FAR
        rows.append(
            {
                "name": p.get("PackageName"),
                "size": size,
                "remain": remain,
                "used": f(p.get("CycleCapacityUsedPrecise") or p.get("CycleCapacityUsed")),
                "expire": exp.strftime("%Y-%m-%d %H:%M:%S") if exp else "",
                "countdown": human(secs),
                "secs": secs,
                "status": status,
                "cycle_start": p.get("CycleStartTime"),
            }
        )
        if remain <= 0:
            continue
        if status == 3 or secs <= 0:
            expired_lost += remain
            expired_lost_count += 1
            continue
        alive_sum += remain
        k = exp.strftime("%Y-%m-%d %H:%M") if exp else "无期限"
        buckets[k]["amount"] += remain
        buckets[k]["count"] += 1
        buckets[k]["names"].add(p.get("PackageName") or "")

    rec["packages_total"] = len(pkgs)
    rec["packages_with_remain"] = sum(1 for r in rows if r["remain"] > 0)
    rec["alive_sum"] = round(alive_sum, 2)
    rec["expired_lost"] = round(expired_lost, 2)
    rec["expired_lost_count"] = expired_lost_count
    rec["rows"] = sorted(rows, key=lambda r: (r["remain"] <= 0, r["secs"]))
    rec["buckets"] = [
        {
            "expire": k,
            "amount": round(v["amount"], 2),
            "count": v["count"],
            "names": sorted(v["names"]),
            "secs": (parse_time(k) - now).total_seconds() if parse_time(k) else FAR,
        }
        for k, v in sorted(buckets.items(), key=lambda kv: (parse_time(kv[0]) or now))
    ]

    try:
        code, res = A.api(cred, A.PATH_CHECKIN_STATUS)
        if code == 200 and res.get("code") == 0:
            cd = res.get("data") or {}
            rec["checkin"] = {
                "signed": bool(cd.get("today_checked_in")),
                "streak_days": cd.get("streak_days"),
                "daily_credit": f(cd.get("daily_credit")),
                "checkin_total_credits": f(cd.get("total_credits")),
                "activity_end": cd.get("end_time") or "",
                "activity_name": cd.get("theme_name") or "",
            }
    except Exception:
        pass
    return rec


# ---------------- 历史与报告 ----------------


def _bands(rec):
    b = {"d1": 0.0, "d3": 0.0, "d7": 0.0, "d30": 0.0, "far": 0.0}
    for bk in rec.get("buckets") or []:
        s = bk.get("secs", FAR)
        amt = bk.get("amount", 0.0)
        if s > 8e17:
            b["far"] += amt
        elif s <= DAY:
            b["d1"] += amt
        elif s <= 3 * DAY:
            b["d3"] += amt
        elif s <= 7 * DAY:
            b["d7"] += amt
        elif s <= 30 * DAY:
            b["d30"] += amt
        else:
            b["far"] += amt
    return {k: round(v, 2) for k, v in b.items()}


def load_report():
    try:
        with open(REPORT_JSON, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {"generated_at": "", "account_count": 0, "accounts": []}


def append_history(recs):
    """把本次快照追加进历史（jsonl），返回追加前的「上一次」值映射。"""
    prev = last_history_map()
    os.makedirs(C.LOG_DIR, exist_ok=True)
    try:
        with open(HISTORY_JSONL, "a", encoding="utf-8") as fp:
            for r in recs:
                if r.get("err"):
                    continue
                nxt = (r.get("buckets") or [None])[0]
                fp.write(
                    json.dumps(
                        {
                            "ts": r.get("ts") or C.now_str(),
                            "key": r["key"],
                            "label": r.get("label"),
                            "alive": r.get("alive_sum", 0.0),
                            "bands": _bands(r),
                            "next_expire": nxt["expire"] if nxt else "",
                            "next_secs": nxt["secs"] if nxt else FAR,
                            "packages_with_remain": r.get("packages_with_remain", 0),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    except Exception:
        pass
    return prev


def last_history_map():
    """读历史，返回 {key: 最后一次 alive}。"""
    out = {}
    try:
        with open(HISTORY_JSONL, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("key"):
                    out[o["key"]] = o
    except FileNotFoundError:
        pass
    return out


def snapshot(accounts, log=None, refresh_html=True):
    """查询一批账号并合并进报告。

    accounts: config 里的账号 dict 列表（通常就是本次签到跑过的账号）。
    只覆盖这些账号，报告里其他账号保留上次数据。
    返回 (results, summary_dict)；任何异常都被吞掉并写 err，不影响调用方。
    """
    t0 = time.time()
    results = []
    for a in accounts:
        try:
            rec = query_account(a)
        except Exception as e:
            rec = {
                "key": a.get("key"),
                "label": a.get("label") or a.get("key"),
                "nickname": a.get("nickname") or "",
                "uid": a.get("uid") or "",
                "err": "%s: %s" % (type(e).__name__, e),
                "ts": C.now_str(),
            }
        results.append(rec)
        if log:
            if rec.get("err"):
                log("[积分] %s 查询失败：%s" % (rec["label"], rec["err"]))
            else:
                nxt = (rec.get("buckets") or [None])[0]
                log(
                    "[积分] %s 可用 %.2f｜最近过期 %s（%s）"
                    % (
                        rec["label"],
                        rec["alive_sum"],
                        nxt["expire"] if nxt else "无",
                        human(nxt["secs"]) if nxt else "无",
                    )
                )
    try:
        prev = append_history(results)
        old = load_report()
        by_key = {r["key"]: r for r in (old.get("accounts") or [])}
        for r in results:
            by_key[r["key"]] = r
        merged = {
            "generated_at": C.now_str(),
            "account_count": len(by_key),
            "accounts": list(by_key.values()),
            "last_batch": [r["key"] for r in results],
        }
        os.makedirs(C.LOG_DIR, exist_ok=True)
        with open(REPORT_JSON, "w", encoding="utf-8") as fp:
            json.dump(merged, fp, ensure_ascii=False, indent=2)
        if refresh_html:
            render_html_to_file(merged, prev)
        summ = {
            "ok": True,
            "total": round(sum(r.get("alive_sum", 0) for r in by_key.values()), 2),
            "failed": [r["label"] for r in results if r.get("err")],
            "elapsed": round(time.time() - t0, 1),
            "report_html": REPORT_HTML,
        }
        if log:
            log(
                "[积分] 台账已更新：全部账号可用合计 %.2f（本次耗时 %.1fs）"
                % (summ["total"], summ["elapsed"])
            )
        return results, summ
    except Exception as e:
        if log:
            log("[积分] 台账写入失败（不影响签到）：%s: %s" % (type(e).__name__, e))
        return results, {"ok": False, "err": str(e)}


# ---------------- HTML 渲染 ----------------

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--line:#e5e7eb;--txt:#1f2328;--dim:#6b7280;
--danger:#d92d20;--warn:#e07b00;--soon:#b78103;--month:#2563eb;--far:#059669;}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--txt);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:20px;margin:0 0 4px}
.sub2{color:var(--dim);font-size:13px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:22px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#f1f3f5;font-size:12px;color:var(--dim);font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.big{font-weight:600;font-size:15px}
.dim{color:var(--dim);font-size:12px}
.up{color:var(--far);font-weight:600}
.down{color:var(--danger);font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;white-space:nowrap}
.pillbig{display:inline-block;padding:2px 9px;border-radius:6px;font-size:12.5px;white-space:nowrap}
.danger{background:#fee4e2;color:var(--danger)}
.warn{background:#fef0c7;color:var(--warn)}
.soon{background:#fef7c3;color:var(--soon)}
.month{background:#dbeafe;color:var(--month)}
.far{background:#d1fae5;color:var(--far)}
table.sub{margin:6px 0 10px;border:none;font-size:12.5px}
table.sub td{padding:5px 8px;border-bottom:1px solid #f0f1f3}
details summary{cursor:pointer;color:var(--month);font-size:12.5px}
h2{font-size:16px;margin:26px 0 10px}
.barrow{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.barlabel{width:96px;font-size:12.5px;font-variant-numeric:tabular-nums}
.bartrack{flex:1;background:#eef0f3;border-radius:6px;height:18px;overflow:hidden}
.barfill{height:100%;border-radius:6px}
.barval{width:80px;text-align:right;font-size:12.5px;font-variant-numeric:tabular-nums}
.note{margin-top:18px;padding:12px 14px;background:#fff;border:1px solid var(--line);
border-radius:10px;color:var(--dim);font-size:12.5px}
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>WorkBuddy 账号积分与过期报告</title>
<style>__CSS__</style></head><body>
<h1>WorkBuddy 账号积分与过期报告</h1>
<div class='sub2'>生成时间：__GEN__ · 账号 __N__ 个 · 本次快照 __BATCH__ 个 ·
数据来源：官方 billing 接口（随每日签到自动刷新）</div>
<div class='cards'>
  <div class='card'><div class='k'>当前可用积分合计</div><div class='v'>__TOTAL__</div></div>
  <div class='card'><div class='k'>24 小时内过期</div><div class='v' style='color:var(--danger)'>__D1__</div></div>
  <div class='card'><div class='k'>1–3 天内过期</div><div class='v' style='color:var(--warn)'>__D3__</div></div>
  <div class='card'><div class='k'>3–7 天内过期</div><div class='v' style='color:var(--soon)'>__D7__</div></div>
  <div class='card'><div class='k'>7–30 天内过期</div><div class='v' style='color:var(--month)'>__D30__</div></div>
  <div class='card'><div class='k'>30 天以后过期</div><div class='v' style='color:var(--far)'>__FAR__</div></div>
</div>
<h2>优先消耗顺序（按最近过期时间升序，越靠前越该先用）</h2>
<div class='cards' style='display:block;background:var(--card);border:1px solid var(--line);padding:14px 16px'>
<table><thead><tr><th>#</th><th>账号</th><th class='num'>可用积分</th>
<th>最近一批过期</th><th class='num'>7天内将过期</th><th class='num'>较上次</th></tr></thead>
<tbody>__PRIORITY__</tbody></table>
</div>
<h2>各账号明细（按可用积分降序）</h2>
<table>
<thead><tr>
<th>账号</th><th class='num'>可用积分</th><th class='num'>较上次</th><th>最近一批过期时间</th>
<th class='num'>3天内<br>将过期</th><th class='num'>3-7天<br>将过期</th><th class='num'>7-30天<br>将过期</th>
<th class='num'>有余额<br>包数</th><th>今日签到</th><th>过期批次</th>
</tr></thead>
<tbody>__ROWS__</tbody></table>
<h2>未来 30 天全局过期时间轴（全部账号合计）</h2>
<div class='cards' style='display:block;background:var(--card);border:1px solid var(--line);padding:14px 16px'>
__BARS__
</div>
<div class='note'>
说明：<br>
1. 「可用积分」= 各资源包 CycleCapacityRemain 之和，与官方总览 TotalCredits 一致。<br>
2. 「过期时间」取每个资源包 ExpiredTime 与 CycleEndTime 中更早的一个（ExpiredTime 常为空，此时以周期结束时间为准）。<br>
3. 已过期的资源包余额服务端已清零，本期「已过期残留」为 __LOST__。<br>
4. 签到积分单独计列：Buddy 加油站每日 100 积分，活动期至 __ACTEND__。<br>
5. 本页由每日签到流程自动刷新，也可手动执行 <code>python credit_report.py</code> 立即重查。<br>
6. __FAIL__
</div>
</body></html>"""


def fmt(n):
    try:
        return "{:,.2f}".format(float(n))
    except (TypeError, ValueError):
        return str(n)


def render_html(data, prev=None):
    prev = prev or {}
    accounts = [a for a in (data.get("accounts") or []) if not a.get("err")]
    failed = [a for a in (data.get("accounts") or []) if a.get("err")]
    now = datetime.now()
    batch = set(data.get("last_batch") or [])

    total_alive = sum(a.get("alive_sum", 0) for a in accounts)
    total_lost = sum(a.get("expired_lost", 0) for a in accounts)
    g = {"d1": 0.0, "d3": 0.0, "d7": 0.0, "d30": 0.0, "far": 0.0}
    for a in accounts:
        b = _bands(a)
        for k in g:
            g[k] += b[k]
    g = {k: round(v, 2) for k, v in g.items()}

    act_end = ""
    for a in accounts:
        ae = (a.get("checkin") or {}).get("activity_end")
        if ae:
            act_end = ae
            break

    def delta_cell(a):
        p = prev.get(a["key"])
        if not p or a["key"] not in batch:
            return "<td class='num dim'>—</td>"
        d = a.get("alive_sum", 0) - float(p.get("alive") or 0)
        if abs(d) < 0.005:
            return "<td class='num dim'>0</td>"
        cls = "up" if d > 0 else "down"
        return "<td class='num %s'>%s%s</td>" % (cls, "+" if d > 0 else "", fmt(d))

    def delta_text(a):
        p = prev.get(a["key"])
        if not p or a["key"] not in batch:
            return "<td class='num dim'>—</td>"
        d = a.get("alive_sum", 0) - float(p.get("alive") or 0)
        if abs(d) < 0.005:
            return "<td class='num dim'>0</td>"
        cls = "up" if d > 0 else "down"
        return "<td class='num %s'>%s%s</td>" % (cls, "+" if d > 0 else "", fmt(d))

    rows = []
    for a in sorted(accounts, key=lambda x: -x.get("alive_sum", 0)):
        nxt = (a.get("buckets") or [None])[0]
        nxt_txt = (nxt["expire"] + " · 剩 " + human(nxt["secs"])) if nxt else "—"
        cls = band(nxt["secs"]) if nxt else "far"
        b = _bands(a)
        buckets_html = "".join(
            "<tr><td>%s</td><td class='num'>%s</td><td class='num'>%d</td>"
            "<td><span class='pill %s'>%s</span></td><td class='dim'>%s</td></tr>"
            % (
                bk["expire"],
                fmt(bk["amount"]),
                bk["count"],
                band(bk["secs"]),
                human(bk["secs"]),
                " / ".join(bk["names"])[:40],
            )
            for bk in (a.get("buckets") or [])
        )
        ck = a.get("checkin") or {}
        fresh = " ●" if a["key"] in batch else ""
        rows.append(
            "<tr><td><b>%s</b>%s<div class='dim'>%s</div></td>"
            "<td class='num big'>%s</td>%s"
            "<td><span class='pillbig %s'>%s</span></td>"
            "<td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
            "<td class='num dim'>%d / %d</td><td class='dim'>%s</td>"
            "<td><details><summary>查看</summary><table class='sub'>%s</table></details></td></tr>"
            % (
                a.get("label") or a["key"],
                fresh,
                a.get("nickname") or "",
                fmt(a.get("alive_sum", 0)),
                delta_cell(a),
                cls,
                nxt_txt,
                fmt(b["d1"] + b["d3"]),
                fmt(b["d7"]),
                fmt(b["d30"]),
                a.get("packages_with_remain", 0),
                a.get("packages_total", 0),
                ("已签·连%s天" % ck.get("streak_days")) if ck.get("signed") else ("未签" if ck else "—"),
                buckets_html or "<tr><td colspan='5'>无</td></tr>",
            )
        )

    # 优先消耗顺序：最近过期时间升序
    prio = [a for a in accounts if a.get("buckets")]
    prio.sort(key=lambda x: (x["buckets"][0]["secs"], -x.get("alive_sum", 0)))
    prio_rows = []
    for i, a in enumerate(prio[:10], 1):
        nxt = a["buckets"][0]
        b = _bands(a)
        prio_rows.append(
            "<tr><td class='dim'>%d</td><td><b>%s</b></td><td class='num'>%s</td>"
            "<td><span class='pillbig %s'>%s · 剩 %s</span></td>"
            "<td class='num'>%s</td>%s</tr>"
            % (
                i,
                a.get("label") or a["key"],
                fmt(a.get("alive_sum", 0)),
                band(nxt["secs"]),
                nxt["expire"],
                human(nxt["secs"]),
                fmt(b["d1"] + b["d3"] + b["d7"]),
                delta_text(a),
            )
        )

    by_day = defaultdict(float)
    for a in accounts:
        for bk in a.get("buckets") or []:
            if bk["secs"] > 30 * DAY:
                continue
            by_day[bk["expire"][:10]] += bk["amount"]
    days = sorted(by_day.items())
    max_day = max((v for _, v in days), default=1)
    bars = "".join(
        "<div class='barrow'><div class='barlabel'>%s</div>"
        "<div class='bartrack'><div class='barfill %s' style='width:%.1f%%'></div></div>"
        "<div class='barval'>%s</div></div>"
        % (
            day,
            band((datetime.strptime(day, "%Y-%m-%d") - now).total_seconds()),
            max(v / max_day * 100, 2),
            fmt(v),
        )
        for day, v in days
    )

    repl = {
        "__CSS__": CSS,
        "__GEN__": data.get("generated_at") or C.now_str(),
        "__N__": str(len(accounts)),
        "__BATCH__": str(len(batch)),
        "__TOTAL__": fmt(total_alive),
        "__D1__": fmt(g["d1"]),
        "__D3__": fmt(g["d3"]),
        "__D7__": fmt(g["d7"]),
        "__D30__": fmt(g["d30"]),
        "__FAR__": fmt(g["far"]),
        "__ROWS__": "".join(rows) or "<tr><td colspan='10'>无数据</td></tr>",
        "__PRIORITY__": "".join(prio_rows) or "<tr><td colspan='6'>无数据</td></tr>",
        "__BARS__": bars or "<div class='dim'>无</div>",
        "__LOST__": fmt(total_lost),
        "__ACTEND__": act_end or "—",
        "__FAIL__": (
            "本次查询失败账号："
            + "、".join("%s(%s)" % (a.get("label") or a["key"], a["err"]) for a in failed)
        )
        if failed
        else "全部账号查询成功，无失败。",
    }
    html = TEMPLATE
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


def render_html_to_file(data, prev=None):
    os.makedirs(C.LOG_DIR, exist_ok=True)
    with open(REPORT_HTML, "w", encoding="utf-8") as fp:
        fp.write(render_html(data, prev))
    return REPORT_HTML
