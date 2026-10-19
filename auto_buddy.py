#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""buddy-auto-v2 主 CLI：账号管理 / 签到 / 成长任务 / 计划任务 / 网页工作台。

用法：
  python auto_buddy.py --discover              扫描本机所有可用的登录态
  python auto_buddy.py --capture account_a     把当前客户端登录的账号存成 account_a 的凭证
  python auto_buddy.py --list                  列出配置账号与最近执行状态
  python auto_buddy.py --run                   执行到点的账号（计划任务调用）
  python auto_buddy.py --run --force           忽略时间窗，执行所有启用账号
  python auto_buddy.py --run --account b       只跑指定账号
  python auto_buddy.py --check                 只查询不领取（体检用）
  python auto_buddy.py --sync                  扫登录态，新账号自动入库
  python auto_buddy.py --ui [--port 8765]      启动网页工作台
  python auto_buddy.py --install-scheduler     注册 Windows 计划任务
  python auto_buddy.py --remove-scheduler      删除计划任务
  python auto_buddy.py --add <UID>             把本机某个登录态账号加入列表
  python auto_buddy.py --remove <KEY>          从列表移除（凭证文件保留）
  python auto_buddy.py --disable <KEY>         停用 / --enable 启用
  python auto_buddy.py --capture-all           一键续期所有账号凭证
  python auto_buddy.py --notify-test           测试提醒通道

退出码：0=全部成功或无需执行；1=存在失败；2=凭证失效/缺失
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as CFG  # noqa: E402
from core import creds as CR  # noqa: E402
from core import logs as L  # noqa: E402
from core import notify as N  # noqa: E402
from engine import missions_api as MA  # noqa: E402
from engine import tasks as T  # noqa: E402

OK = "success"
SKIP = "skip"
FAIL = "fail"


def is_due(account, cfg, force=False):
    if force:
        return True, "force"
    if cfg["schedule"].get("trigger_mode", "daily") == "interval":
        return True, "interval"
    window = int(cfg["schedule"].get("window_minutes", 25))
    now = datetime.now()
    for t in account.get("times", []):
        try:
            hh, mm = [int(x) for x in t.split(":")]
        except Exception:
            continue
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if abs((now - target).total_seconds()) <= window * 60:
            return True, t
    return False, ""


def scheduler_command(cfg):
    py = cfg.get("python_path") or sys.executable
    # ❗ 2026-09-26 修复：原实现执行 auto_buddy.py --run（纯 API 管线 cmd_run →
    # MA.run_account），从不走 run_all 四阶段（API 管线 + UI 驱动流水线）全流程，
    # 导致定时触发的任务缺 UI 阶段、成长任务只能做到接口级。
    # 改为执行 orchestrator.run_all 完整流程（其 main() 自带进度/日志/停止支持）。
    return 'cmd /c cd /d "%s" && "%s" -m orchestrator.run_all >> "%s" 2>&1' % (
        CFG.BASE_DIR,
        py,
        os.path.join(CFG.LOG_DIR, "scheduler.log"),
    )


def install_scheduler(cfg):
    sch = cfg.get("schedule", {})
    mode = sch.get("trigger_mode", "daily")
    prefix = sch.get("task_prefix", "WorkBuddyAuto")
    cmd = scheduler_command(cfg)
    os.makedirs(CFG.LOG_DIR, exist_ok=True)
    specs = []
    if mode == "interval":
        every = max(30, int(sch.get("every_minutes", 60)))
        specs.append((prefix + "_Interval", "/SC MINUTE /MO %d" % every))
    else:
        times = []
        for a in cfg["accounts"]:
            if not a.get("enabled", True):
                continue
            for t in a.get("times", []):
                if t not in times:
                    times.append(t)
        times.sort()
        for t in times:
            specs.append(
                ("%s_%s" % (prefix, t.replace(":", "")), "/SC DAILY /ST %s" % t)
            )
    ok = True
    for name, trigger in specs:
        r = subprocess.run(
            'schtasks /Create /TN "%s" /TR "%s" %s /F' % (name, cmd, trigger),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode == 0:
            print("[已注册] %s  %s" % (name, trigger))
        else:
            ok = False
            print("[失败] %s: %s" % (name, r.stderr.strip() or r.stdout.strip()))
    if ok:
        print("共 %d 个定时任务，执行内容：%s" % (len(specs), cmd))
    return 0 if ok else 1


def remove_scheduler(cfg):
    prefix = cfg.get("schedule", {}).get("task_prefix", "WorkBuddyAuto")
    r = subprocess.run(
        "schtasks /Query /FO CSV",
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    names = [
        line.split(",")[0].strip('"')
        for line in r.stdout.splitlines()
        if line.startswith('"') and line.split(",")[0].strip('"').startswith(prefix)
    ]
    for n in names:
        subprocess.run(
            'schtasks /Delete /TN "%s" /F' % n,
            shell=True,
            capture_output=True,
            text=True,
        )
        print("[已删除] %s" % n)
    print("共删除 %d 个" % len(names))
    return 0


def cmd_discover(cfg):
    print("本机保存过的登录态（按最近使用排序）：")
    print("-" * 96)
    print("%-28s%-38s%-13s%-21s%8s" % ("昵称", "UID", "手机", "保存时间", "剩余天数"))
    print("-" * 96)
    for a in CR.discover_accounts():
        print(
            "%-28s%-38s%-13s%-21s%8s"
            % (
                a["nickname"][:26],
                a["uid"],
                a["phone"],
                a["saved_at"],
                a["expiry_days"],
            )
        )
    print("-" * 96)
    print("用法：客户端登录目标账号 -> python auto_buddy.py --capture <key>")
    return 0


def cmd_list(cfg):
    runs = []
    if os.path.exists(CFG.RUNS_LOG):
        with open(CFG.RUNS_LOG, encoding="utf-8") as f:
            runs = [__import__("json").loads(l) for l in f if l.strip()]
    print("%-12s%-14s%-8s%-10s最近执行" % ("账号", "备注", "状态", "凭证剩余"))
    print("-" * 92)
    for a in cfg["accounts"]:
        last = next((r for r in reversed(runs) if r.get("account") == a["key"]), None)
        cred, err = CR.load_cred(a)
        exp = "%.0f天" % CR.cred_expiry_days(cred) if cred else "缺失"
        state = (
            "停用" if not a.get("enabled", True) else ("正常" if cred else "凭证失效")
        )
        stamp = (
            ("%s | %s" % (last["ts"], last.get("reason", "")[:44]))
            if last
            else "从未执行"
        )
        print(
            "%-12s%-14s%-8s%-10s%s" % (a["key"], a.get("label", ""), state, exp, stamp)
        )
    return 0


def cmd_run(cfg, args):
    due = []
    for a in cfg["accounts"]:
        if not a.get("enabled", True):
            continue
        if args.account and args.account not in (a["key"], a.get("label")):
            continue
        ok, why = is_due(a, cfg, force=args.force)
        if ok:
            due.append((a, why))
        elif args.account:
            print(
                "[跳过] %s 未到点（配置为 %s，当前 %s）"
                % (a["key"], ",".join(a.get("times", [])), CFG.now_str())
            )

    if not due:
        print(
            "[%s] 没有到点的账号，本次不执行（加 --force 可强制执行）" % CFG.now_str()
        )
        return 0

    print(
        "===== 执行开始 %s | 账号数 %d | %s ====="
        % (CFG.now_str(), len(due), "仅查询" if args.check else "正式执行")
    )
    all_results, has_fail, has_auth_fail = [], False, False
    for a, why in due:
        try:
            res = MA.run_account(a, cfg, check_only=args.check)
        except Exception as e:
            res = {
                "account": a["key"],
                "label": a.get("label", ""),
                "status": FAIL,
                "tasks": {},
                "reason": "账号级异常：%s: %s" % (type(e).__name__, e),
            }
        res["ts"] = CFG.now_str()
        res["trigger"] = why
        res["date"] = CFG.today_str()
        all_results.append(res)
        L.append_run(res)
        flag = {"success": "成功", "skip": "跳过", "fail": "失败"}[res["status"]]
        print(
            "\n[%s] %s %s  (触发:%s)"
            % (flag, res["label"], res.get("nickname", ""), why)
        )
        for k, v in res.get("tasks", {}).items():
            mark = {"success": "成功", "skip": "跳过", "fail": "失败"}[v["status"]]
            name = MA.TASKS.get(k, (k,))[0] if k in MA.TASKS else k
            print("    - %s: [%s] %s" % (name, mark, v["reason"]))
            if v.get("steps"):
                for s in v["steps"]:
                    if s.get("step") in ("skip",):
                        continue
                    print(
                        "        · %s [%s/%s] %s"
                        % (
                            s.get("title") or s.get("task"),
                            s["step"],
                            s["status"],
                            s["reason"],
                        )
                    )

        gt = (res.get("tasks") or {}).get("growth_tasks") or {}
        b0, b1, bd = (
            gt.get("balance_before"),
            gt.get("balance_after"),
            gt.get("balance_delta"),
        )
        if gt.get("balance_error") or b1 is None:
            print(
                "    ! 积分余额获取失败：%s"
                % (gt.get("balance_error") or "接口未返回余额")
            )
        else:
            sign = "+" if (bd or 0) >= 0 else ""
            print(
                "    · 积分余额：%s → %s（增量 %s%s）" % (b0, b1, sign, bd)
                + (
                    "；本次任务领取 +%s 积分" % gt.get("credit_got")
                    if gt.get("credit_got")
                    else ""
                )
            )
        res["balance"] = {
            "before": b0,
            "after": b1,
            "delta": bd,
            "error": gt.get("balance_error", ""),
            "credit_got": gt.get("credit_got", 0),
        }
        if res["status"] == FAIL:
            has_fail = True
            if any(v.get("code") == "auth_expired" for v in res["tasks"].values()):
                has_auth_fail = True
        if res.get("cred_expiry_days") is not None and res["cred_expiry_days"] < 7:
            N.notify(
                "凭证即将过期",
                "%s 的登录凭证 %.0f 天后过期，请登录客户端后执行 --capture %s"
                % (res["label"], res["cred_expiry_days"], res["account"]),
            )
            print("    ! 凭证 %.0f 天后过期，请尽快重新抓取" % res["cred_expiry_days"])

    CFG.save_json(CFG.LATEST_JSON, {"ts": CFG.now_str(), "results": all_results})

    lines = [
        "===== %s 积分汇总（账号标识 / 执行前 / 执行后 / 增量） =====" % CFG.now_str()
    ]
    print("\n" + lines[0])
    for r in all_results:
        b = r.get("balance") or {}
        b0, b1, bd = b.get("before"), b.get("after"), b.get("delta")
        ident = "%s(%s)" % (r["account"], r.get("label") or "")
        if b.get("error") or b1 is None:
            lines.append(
                "[余额获取失败] %s：%s" % (ident, b.get("error") or "接口未返回余额")
            )
            print(
                "  %-26s 余额获取失败：%s" % (ident, b.get("error") or "接口未返回余额")
            )
        else:
            sign = "+" if (bd or 0) >= 0 else ""
            line = "%s  %s → %s  (%s%s)" % (ident, b0, b1, sign, bd)
            lines.append(line)
            print("  %-26s %6s → %6s  (%s%s)" % (ident, str(b0), str(b1), sign, bd))

    note = (
        "说明：积分余额 = Buddy加油站积分（官方唯一暴露的余额口径）；"
        "成长任务领取的积分计入成长账户、无公开余额接口，"
        "所以「增量」只反映签到变化，任务获得的积分在每个账号下单独列出。"
    )
    lines.append(note)
    print("  " + note)

    lines.append("")
    lines.append("===== %s 任务明细（完成 / 跳过 / 失败） =====" % CFG.now_str())
    print("\n" + lines[-1])
    for r in all_results:
        gt = (r.get("tasks") or {}).get("growth_tasks") or {}
        s = gt.get("summary") or {}
        print(
            "\n  账号 %s（%s）  完成 %s/%s  领取 %s  接取 %s  跳过付费 %s  跳过其它 %s  失败 %s"
            % (
                r["account"],
                r.get("label") or "",
                s.get("done", "-"),
                s.get("total", "-"),
                s.get("claimed", 0),
                s.get("accepted", 0),
                s.get("skipped_paid", 0),
                s.get("skipped_other", 0),
                s.get("failed", 0),
            )
        )
        lines.append(
            "账号 %s：完成 %s/%s，领取 %s，跳过付费 %s，失败 %s"
            % (
                r["account"],
                s.get("done", "-"),
                s.get("total", "-"),
                s.get("claimed", 0),
                s.get("skipped_paid", 0),
                s.get("failed", 0),
            )
        )
        for p in gt.get("skipped_paid", []):
            print(
                "      [跳过·需付费] %s（%s）：%s"
                % (p["title"], p["task"], p["reason"])
            )
            lines.append("    跳过付费：%s —— %s" % (p["title"], p["reason"]))
        for f in gt.get("failed_tasks", []):
            print("      [失败] %s（%s）：%s" % (f["title"], f["task"], f["reason"]))
            lines.append("    失败：%s —— %s" % (f["title"], f["reason"]))
        if r["status"] == FAIL:
            print("      [账号失败] %s" % r.get("reason", ""))
            lines.append("    账号失败：%s" % r.get("reason", ""))

    os.makedirs(CFG.LOG_DIR, exist_ok=True)
    with open(CFG.SUMMARY_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    alert_cfg = cfg.get("alert", {})
    if has_fail:
        failed_names = "、".join(r["label"] for r in all_results if r["status"] == FAIL)
        detail = " | ".join(
            r.get("reason", "")[:80] for r in all_results if r["status"] == FAIL
        )
        print("\n[告警] 以下账号执行失败：%s" % failed_names)
        N.notify("Buddy 自动任务失败", "%s：%s" % (failed_names, detail))
    elif alert_cfg.get("notify_on_success"):
        N.notify(
            "Buddy 自动任务完成",
            "\n".join(
                "%s: %s" % (r["label"], r.get("reason", "")[:60]) for r in all_results
            ),
        )
    if has_auth_fail:
        print(
            "[处理建议] 有账号登录态失效：打开 WorkBuddy 客户端登录对应账号，"
            "然后执行 python auto_buddy.py --capture <key>"
        )

    # 签到后自动刷新积分台账（可用积分 + 过期倒计时）。
    # 旁路逻辑：只调官方接口、不切号不碰客户端；失败仅打印，不改变本次退出码。
    try:
        from core import credits as CRE

        print("\n===== 积分台账刷新（签到后自动执行） =====")
        CRE.snapshot([a for a, _ in due], log=lambda m: print(m, flush=True))
        print("报告：logs/credits_report.html")
    except Exception as e:
        print("[积分台账] 刷新失败（不影响签到）：%s: %s" % (type(e).__name__, e))

    return 2 if has_auth_fail else (1 if has_fail else 0)


def main():
    parser = argparse.ArgumentParser(
        description="buddy-auto-v2 多账号自动签到 / 成长计划任务"
    )
    parser.add_argument(
        "--discover", action="store_true", help="扫描本机所有可用登录态"
    )
    parser.add_argument(
        "--capture", metavar="KEY", help="把客户端当前登录账号存为 KEY 的凭证"
    )
    parser.add_argument(
        "--from-file", metavar="PATH", help="配合 --capture：从指定登录态文件抓取"
    )
    parser.add_argument(
        "--allow-mismatch", action="store_true", help="capture 时允许 UID 不一致"
    )
    parser.add_argument("--list", action="store_true", help="列出配置账号与最近状态")
    parser.add_argument(
        "--sync", action="store_true", help="扫描本机登录态，新账号自动入库"
    )
    parser.add_argument("--run", action="store_true", help="执行到点账号")
    parser.add_argument("--check", action="store_true", help="配合 --run：只查询不领取")
    parser.add_argument("--account", metavar="KEY", help="只处理指定账号")
    parser.add_argument("--force", action="store_true", help="忽略时间窗强制执行")
    parser.add_argument(
        "--install-scheduler", action="store_true", help="注册 Windows 计划任务"
    )
    parser.add_argument("--remove-scheduler", action="store_true", help="删除计划任务")
    parser.add_argument("--add", metavar="UID", help="把本机某个登录态账号加入配置列表")
    parser.add_argument("--remove", metavar="KEY", help="从配置列表移除账号")
    parser.add_argument("--enable", metavar="KEY", help="启用账号")
    parser.add_argument("--disable", metavar="KEY", help="停用账号")
    parser.add_argument(
        "--capture-all", action="store_true", help="一键续期所有账号凭证"
    )
    parser.add_argument("--ui", action="store_true", help="启动网页工作台")
    parser.add_argument(
        "--port", type=int, default=8765, help="网页工作台端口，默认 8765"
    )
    parser.add_argument("--notify-test", action="store_true", help="测试提醒通道")
    args = parser.parse_args()

    cfg = CFG.load_config()

    if args.discover:
        return cmd_discover(cfg)
    if args.capture:
        acc = CFG.find_account(cfg, args.capture)
        if not acc:
            print(
                "[失败] 配置里没有 key=%s 的账号，先在 config.json 里加一个。"
                % args.capture
            )
            return 1
        rc, msg = CR.capture(
            acc, allow_mismatch=args.allow_mismatch, source=args.from_file
        )
        print(("[成功] " if rc == 0 else "[失败] ") + msg)
        return rc
    if args.list:
        return cmd_list(cfg)
    if args.install_scheduler:
        return install_scheduler(cfg)
    if args.remove_scheduler:
        return remove_scheduler(cfg)
    if args.capture_all:
        ok, skipped = CR.capture_all_possible()
        print("[完成] 已续期 %d 个：%s" % (len(ok), ", ".join(ok) or "无"))
        if skipped:
            print("[需手动登录] " + "；".join(skipped))
            print("           打开客户端扫码登录后，再执行一次本命令即可")
        return 0 if not skipped else 1
    if args.add:
        acc, msg = CR.add_account_by_uid(args.add)
        print(("[成功] " if acc else "[失败] ") + msg)
        return 0 if acc else 1
    if args.remove:
        ok, msg = CR.remove_account(args.remove)
        print(("[成功] " if ok else "[失败] ") + msg)
        return 0 if ok else 1
    if args.enable:
        ok, msg = CR.set_enabled(args.enable, True)
        print(("[成功] " if ok else "[失败] ") + msg)
        return 0 if ok else 1
    if args.disable:
        ok, msg = CR.set_enabled(args.disable, False)
        print(("[成功] " if ok else "[失败] ") + msg)
        return 0 if ok else 1
    if args.ui:
        from web import server

        return server.serve(args.port)
    if args.notify_test:
        N.notify("Buddy 提醒测试", "如果你看到这条气泡，说明告警通道正常。")
        print("已发送测试提醒。")
        return 0
    if args.sync:
        added, skipped = CR.auto_sync_accounts()
        print(
            "[自动入库] 新增 %d 个账号%s"
            % (
                len(added),
                ("：%s" % "、".join(a["label"] for a in added)) if added else "",
            )
        )
        for s in skipped:
            print("           跳过 %s" % s)
    if args.run:
        return cmd_run(cfg, args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
