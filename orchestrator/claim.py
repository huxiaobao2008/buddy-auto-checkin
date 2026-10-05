#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收尾领取：一键领取所有账号的可领积分（纯 API，不需要切换客户端登录账号）。

UI 驱动把任务"做完"之后，积分是待领取状态，必须再调领取接口才算到账。
不驱动客户端 UI，可以随时跑、不会打断正在进行的界面操作。
"""

import json
import sys

from core import config as CFG
from engine import tasks as T


def claim_account(account, cfg, quiet=False):
    r = T.run_account_tasks(account, cfg)
    s = r.get("summary") or {}

    # 2026-09-25: 收尾领取时再检查一次猫猫旅行——阶段二跑了很久，猫猫可能中途回来了
    # 修复(2026-09-28): 必须先加载该账号凭证，否则 travel_dispatch 拿不到
    #   auth，内部异常被吞掉后"旅行检查"静默失效（历史 P0/P1 类缺陷）。
    from core import creds as CR
    from engine import missions_api as MA

    try:
        cred, cred_err = CR.load_cred(account)
        if cred is None:
            if not quiet:
                print("   猫猫旅行检查跳过：%s" % cred_err, flush=True)
        else:
            travel_r = MA.task_buddy_travel_dispatch(
                cred, account=account, check_only=False
            )
            if travel_r.get("status") == "success" and "领取" in travel_r.get(
                "reason", ""
            ):
                if not quiet:
                    print("   猫猫旅行：%s" % travel_r.get("reason"), flush=True)
            elif not quiet:
                print(
                    "   猫猫旅行检查：%s" % travel_r.get("reason", travel_r), flush=True
                )
    except Exception as e:
        if not quiet:
            print("   猫猫旅行检查失败：%s" % e, flush=True)

    out = {
        "account": account.get("key"),
        "label": account.get("label") or account.get("nickname"),
        "status": r.get("status"),
        "reason": r.get("reason"),
        "claimed": s.get("claimed"),
        "accepted": s.get("accepted"),
        "credit_got": r.get("credit_got"),
        "claimed_titles": [
            st.get("title")
            for st in (r.get("steps") or [])
            if st.get("step") == "claim" and "领取成功" in str(st.get("reason"))
        ],
        "balance_before": r.get("balance_before"),
        "balance_after": r.get("balance_after"),
        "balance_delta": r.get("balance_delta"),
    }
    if not quiet:
        print(
            "== %s（%s）  %s" % (out["account"], out["label"], out["status"] or "-"),
            flush=True,
        )
        print(
            "   领取 %s 项 | 本次到账 %s 积分 | 余额 %s -> %s（%s）"
            % (
                out["claimed"],
                out["credit_got"],
                out["balance_before"],
                out["balance_after"],
                out["balance_delta"],
            ),
            flush=True,
        )
        if out["reason"]:
            print("   说明：%s" % out["reason"], flush=True)
    return out


def main():
    argv = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    cfg = CFG.load_config()
    accounts = cfg.get("accounts") or []
    if argv:
        want = set(argv)
        accounts = [a for a in accounts if a.get("key") in want]
        missing = want - {a.get("key") for a in accounts}
        for m in sorted(missing):
            print("!! 配置里没有账号 %s" % m, flush=True)

    results = []
    total = 0
    for a in accounts:
        try:
            r = claim_account(a, cfg, quiet=as_json)
        except Exception as e:
            r = {
                "account": a.get("key"),
                "status": "fail",
                "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
            }
            if not as_json:
                print("!! %s 异常：%s" % (a.get("key"), r["reason"]), flush=True)
        results.append(r)
        total += int(r.get("credit_got") or 0)

    if as_json:
        print(
            json.dumps(
                {"total_credit_got": total, "accounts": results},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print()
        print("合计本次到账：%d 积分" % total, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
