#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读验证：旧项目 atrest.key 能否解密当前凭证（不输出任何密钥/token 明文）。"""

import os, sys, json, glob

NEW = r"D:\workbuddy自动签到领积分完成成长任务"
OLD = r"D:\workbuddy\buddy-auto-v2"

sys.path.insert(0, NEW)
from core import config as C
from core import creds as CR


# 1. 两个项目的 key 是否存在
def key_state(proj):
    p = os.path.join(proj, "data", "atrest.key")
    if not os.path.exists(p):
        return None
    raw = open(p, "r", encoding="utf-8").read().strip()
    try:
        b = bytes.fromhex(raw)
        return (len(b), raw[:8] + "...")
    except Exception as e:
        return ("bad", str(e)[:40])


print("NEW data/atrest.key:", key_state(NEW))
print("OLD data/atrest.key:", key_state(OLD))

# 2. 用旧项目的 key 尝试解密新项目凭证的 accessToken（只报成功/失败）
old_key_path = os.path.join(OLD, "data", "atrest.key")
if os.path.exists(old_key_path):
    from core import atrest

    raw = open(old_key_path, "r", encoding="utf-8").read().strip()
    old_key = bytes.fromhex(raw)
    ok_cnt = fail_cnt = 0
    for a in C.load_config().get("accounts", []):
        cred = C.load_json(os.path.join(NEW, a["cred_file"]))
        if not cred:
            continue
        tok = (cred.get("auth") or {}).get("accessToken")
        if not (isinstance(tok, dict) and tok.get("$wbEncrypted")):
            ok_cnt += 1  # 本就是明文
            continue
        try:
            plain_tok = atrest.decrypt_envelope(tok, old_key)
            if plain_tok and len(plain_tok) > 20:
                ok_cnt += 1
            else:
                fail_cnt += 1
        except Exception as e:
            fail_cnt += 1
            print("  解密失败样本:", a["key"], type(e).__name__, str(e)[:80])
    print("用旧项目 key 解密新项目凭证: 成功=%d 失败=%d" % (ok_cnt, fail_cnt))

# 3. 用旧项目 key 模拟 normalize 后，API 请求头长什么样（只看 header 形态）
print("\n=== engine/api.py 请求构造抽样 ===")
api = os.path.join(NEW, "core", "api.py")
if os.path.exists(api):
    src = open(api, "r", encoding="utf-8-sig").read()
    import re

    for m in re.finditer(
        r"(def [a-z_]+\([^)]*\):|headers\s*=|Authorization[^\n]*)", src
    ):
        line = m.group(1)
        if "Authorization" in line or "def " in line:
            print("   ", line.strip()[:110])
