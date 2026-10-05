#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读诊断：续期所有凭证后账号查询失败根因探测。
只输出字段类型/前后缀/uid 一致性，绝不输出完整 token。
"""

import json, os, sys, glob

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from core import creds as CR
from core import config as C


def brief(v, n=8):
    if v is None:
        return "<None>"
    if isinstance(v, dict):
        if v.get("$wbEncrypted") is not None:
            return "<$wbEncrypted dict, envelope.len=%d>" % len(str(v.get("envelope")))
        return "<dict keys=%s>" % list(v.keys())[:6]
    s = str(v)
    return "%s...%s(len=%d)" % (s[:n], s[-n:], len(s))


print("=== 1. data 目录（atrest.key 是否存在）===")
for p in sorted(glob.glob(os.path.join(C.DATA_DIR, "*"))):
    print(
        "  ", os.path.basename(p), os.path.getsize(p) if os.path.isfile(p) else "<dir>"
    )

print("\n=== at-rest key ===")
try:
    from core import atrest

    k = atrest.load_key()
    print("load_key() ->", "OK(len=%d)" % len(k) if k else "None(拿不到密钥)")
except Exception as e:
    print("load_key() 异常:", type(e).__name__, str(e)[:200])

print("\n=== 2. config accounts ===")
cfg = C.load_config()
for a in cfg.get("accounts", []):
    print(
        "  %-12s uid=%-22s file=%s enabled=%s label=%s"
        % (
            a.get("key"),
            str(a.get("uid")),
            a.get("cred_file"),
            a.get("enabled"),
            str(a.get("label"))[:14],
        )
    )

print("\n=== 3. 各账号凭证 load_cred 结果 ===")
for a in cfg.get("accounts", []):
    cred, err = CR.load_cred(a)
    if cred is None:
        print("  %-12s FAIL: %s" % (a.get("key"), err))
        continue
    auth = cred.get("auth") or {}
    acct = cred.get("account") or {}
    tok = auth.get("accessToken")
    uid = acct.get("uid")
    dom = auth.get("domain")
    print(
        "  %-12s OK   token=%s dom=%s uid=%s (cfg_uid=%s, uid_match=%s)"
        % (
            a.get("key"),
            brief(tok),
            brief(dom),
            brief(uid),
            str(a.get("uid")),
            str(uid == a.get("uid")),
        )
    )

print("\n=== 4. discover_accounts（客户端登录态可发现哪些账号）===")
found = {}
try:
    for d in CR.discover_accounts():
        found[d["uid"]] = d["path"]
        print(
            "  uid=%s expiry=%s file=%s"
            % (d["uid"], d.get("expiry_days"), os.path.basename(d["path"]))
        )
except Exception as e:
    print("  discover_accounts 异常:", type(e).__name__, str(e)[:200])

print("\n=== 5. config uid 在 discover_accounts 中是否命中 ===")
for a in cfg.get("accounts", []):
    u = a.get("uid")
    hit = found.get(str(u))
    print(
        "  %-12s uid=%s -> %s"
        % (a.get("key"), u, "命中(%s)" % os.path.basename(hit) if hit else "未命中!")
    )

print("\n=== 6. 客户端登录态文件 auth.accessToken 形态（最新3个）===")
paths = sorted(glob.glob(os.path.join(C.AUTH_DIR, "*.info")))
for p in paths[-3:]:
    j = C.load_json(p)
    if not j:
        print("  ", os.path.basename(p), "解析失败")
        continue
    tok = (j.get("auth") or {}).get("accessToken")
    print(
        "  ",
        os.path.basename(p)[:60],
        "raw token=%s  account.uid=%s"
        % (brief(tok), brief((j.get("account") or {}).get("uid"))),
    )
