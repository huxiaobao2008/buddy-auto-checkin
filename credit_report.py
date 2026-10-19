#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""积分台账 CLI：手动全量刷新（签到流程会自动跑，这里用于随时重查）。

用法：
  python credit_report.py                 刷新所有启用账号
  python credit_report.py account_a       只刷新指定账号（可多个）
  python credit_report.py --no-html       只写 json，不重新渲染 HTML

产物：
  logs/credits_report.json    原始数据（每账号每个资源包的额度与过期时间）
  logs/credits_report.html    可视化报告（浏览器直接打开）
  logs/credits_history.jsonl  每次快照的历史（用于算「较上次」增减）
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from core import config as C  # noqa: E402
from core import credits as CRE  # noqa: E402


def main():
    args = sys.argv[1:]
    keys = [a for a in args if not a.startswith("-")]
    no_html = "--no-html" in args

    cfg = C.load_config()
    accounts = C.enabled_accounts(cfg)
    if keys:
        accounts = [
            a for a in accounts if a.get("key") in keys or a.get("label") in keys
        ]
    if not accounts:
        print("没有匹配的启用账号")
        return 1

    print("=== 积分台账刷新：%d 个账号 ===" % len(accounts), flush=True)
    results, summ = CRE.snapshot(
        accounts, log=lambda m: print(m, flush=True), refresh_html=not no_html
    )

    print("\n%-16s %10s %10s  %s" % ("账号", "可用积分", "7天内过期", "最近一批过期"))
    print("-" * 74)
    for r in sorted(results, key=lambda x: -x.get("alive_sum", 0)):
        if r.get("err"):
            print(
                "%-16s %10s %10s  %s"
                % ((r.get("label") or "")[:16], "失败", "-", r["err"][:40])
            )
            continue
        nxt = (r.get("buckets") or [None])[0]
        b = CRE._bands(r)
        print(
            "%-16s %10.2f %10.2f  %s（剩 %s）"
            % (
                (r.get("label") or r["key"])[:16],
                r.get("alive_sum", 0),
                b["d1"] + b["d3"] + b["d7"],
                nxt["expire"] if nxt else "无",
                CRE.human(nxt["secs"]) if nxt else "无",
            )
        )
    if summ.get("ok"):
        print("\n全部账号可用合计：%.2f" % summ["total"])
        print("报告：%s" % summ["report_html"])
    else:
        print("\n写入失败：%s" % summ.get("err"))
    return 0 if summ.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
