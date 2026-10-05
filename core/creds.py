#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭证与登录态：读取、扫描、抓取、有效期（零第三方依赖）。

数据来源只有两处（全部本机只读，不外传）：
  1. 客户端登录态活文件 %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\*.info
  2. 本项目 accounts/<key>.cred.json（各账号凭证快照）

行为与旧实现一致：
  - load_cred 校验凭证 uid 与配置 uid 一致（防抓错号）
  - discover_accounts 扫全部 auth_scan_dirs，按 uid 去重取 mtime 最新
  - capture 把当前/指定登录态存为某账号的凭证快照
"""

import glob
import os
import re
import time
from datetime import datetime

from . import config as C


def is_encrypted(v):
    """字段是否为客户端 $wbEncrypted 信封对象（无法离线解密）。"""
    return isinstance(v, dict) and v.get("$wbEncrypted") is not None


def plain(v, fallback=""):
    """把可能是 $wbEncrypted 信封对象的字段转成可显示的字符串。

    客户端新版本会把活跃账号的 nickname/phoneNumber/accessToken 等写成
    {"$wbEncrypted":1,"envelope":"..."} 信封对象；旧代码直接当字符串用会
    得到 '[object Object]'。这里统一归一化：加密对象 / None 返回 fallback。
    """
    if v is None:
        return fallback
    if is_encrypted(v):
        return fallback
    if isinstance(v, dict):
        return str(v)
    return str(v)


def _atrest_key():
    """取本机 at-rest 静态密钥；没有/取不到则返回 None（不抛异常）。"""
    try:
        from . import atrest

        return atrest.load_key()
    except Exception:
        return None


def read_auth_file(path):
    """读一份 .info 登录态文件 → {auth, account} 或 None。

    客户端 5.6.2+ 会把 accessToken/nickname/phoneNumber 写成 $wbEncrypted 信封；
    有本机静态密钥时就地解密，这样抓取（capture）落盘的凭证就是明文。
    没有静态密钥且 accessToken 仍是信封时返回 None——上层据此拒绝抓取，
    避免把加密信封对象当成明文凭证覆写落盘（历史 P0 故障根因）。
    """
    j = C.load_json(path)
    if not j:
        return None
    key = _atrest_key()
    if key is not None:
        try:
            from . import atrest

            atrest.normalize_cred(j, key)
        except Exception:
            pass
    auth = j.get("auth") or {}
    if not auth.get("accessToken") or not auth.get("domain"):
        return None
    # 没有静态密钥时信封无法解密：宁可判为"不可用"，绝不把信封当明文外发
    if key is None and is_encrypted(auth.get("accessToken")):
        return None
    return {"auth": auth, "account": j.get("account") or {}}


def load_cred(account):
    """加载某配置账号的凭证快照。返回 (cred, err)。

    凭证里的敏感字段若被客户端写成 $wbEncrypted 信封，有本机静态密钥时
    就地解密成明文；没有密钥则原样返回（调用方按“凭证不完整/查询失败”处理）。
    为防止把加密信封当明文使用，accessToken 仍是信封时返回明确错误。
    """
    path = os.path.join(C.BASE_DIR, account["cred_file"])
    cred = C.load_json(path)
    if not cred:
        return None, "凭证文件缺失：%s（先登录客户端后抓取）" % account["cred_file"]
    key = _atrest_key()
    if key is not None:
        try:
            from . import atrest

            atrest.normalize_cred(cred, key)
        except Exception:
            pass
    auth = cred.get("auth") or {}
    if not auth.get("accessToken"):
        return None, "凭证内容不完整"
    if is_encrypted(auth.get("accessToken")):
        return None, (
            "凭证仍是 $wbEncrypted 加密信封且本机缺少 data/atrest.key，"
            "无法解密 accessToken；请先恢复静态密钥，再重新抓取凭证"
        )
    if (
        account.get("uid")
        and cred.get("account", {}).get("uid")
        and cred["account"]["uid"] != account["uid"]
    ):
        return None, "凭证 UID 与配置不符（凭证=%s 配置=%s）" % (
            cred["account"]["uid"],
            account["uid"],
        )
    return cred, None


def cred_expiry_days(cred):
    exp = (cred.get("auth") or {}).get("expiresAt")
    if not exp:
        return None
    return (exp / 1000 - time.time()) / 86400.0


_ID_RE = re.compile(r"^[\d\-\s_]+$")


def looks_like_id(name):
    """名字是否"不像人能读的名字"：空、纯数字/连字符（uin、手机号也算数字串）、UUID 形态。

    只有返回 True（即配置里那个值读起来像一串编号）时，才用凭证里的真实账号名顶替它。
    **用户自己写的备注（含中英文）必须保留**，不能被手机号覆盖。
    """
    s = plain(name)
    if not s:
        return True
    if _ID_RE.match(s):
        return True
    if re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", s):  # UUID 前缀
        return True
    return False


def cred_display_name(data):
    """从一个登录态 dict（{\"auth\":…, \"account\":…}，已解密）里取账号展示名。

    优先级：**手机号 > 昵称**。客户端 5.6.2+ 会把这两个字段写成 $wbEncrypted 信封，
    只要本机有 at-rest 静态密钥，read_auth_file/load_cred 返回的就是明文。
    """
    acct = (data or {}).get("account") or {}
    return plain(acct.get("phoneNumber")) or plain(acct.get("nickname")) or ""


def sync_display_name(account, data):
    """抓完凭证后把真实账号名回填进 config.json（只纠"非用户自定义"的展示字段）。

    背景：账号入库时若客户端还没写入可读昵称，config 里会残留 uin/uid 这类数字串，
    此后 UI 一直显示它、永不校正。这里在每次抓凭证后纠正，且不覆盖用户的自定义备注：
      - nickname（账号名）：无值或与实际不一致 → 写入真实值
      - label（备注）：为空 / 等于 uid / 等于 uin / 等于旧 nickname → 同步；否则保留不动
    """
    real = cred_display_name(data)
    if not real:
        return False
    cfg = C.load_config()
    acc = None
    for a in cfg.get("accounts") or []:
        if a.get("key") == account.get("key") or (
            a.get("uid") and a.get("uid") == ((data.get("account") or {}).get("uid"))
        ):
            acc = a
            break
    if acc is None:
        return False
    old_nick = plain(acc.get("nickname")) or real
    changed = False
    if old_nick != real:
        acc["nickname"] = real
        changed = True
    lab = plain(acc.get("label"))
    if not lab or lab in (
        str(acc.get("uid") or ""),
        str(acc.get("uin") or ""),
        old_nick,
    ):
        if lab != real:
            acc["label"] = real
            changed = True
    if changed:
        C.save_config(cfg)
    return changed


def discover_accounts():
    """扫描本机保存过的所有登录态，按 uid 去重（取最新），按最近使用排序。

    客户端新版本会把活跃账号的 nickname/phoneNumber 写成 $wbEncrypted 信封对象，
    这里统一归一化为可显示字符串：优先用本机 auth 文件里的明文，缺失时回退到
    config.json 里同一 uid 的明文 label/nickname，再不行用 uin / uid 兜底，
    避免 UI 出现 '[object Object]'。
    """
    best = {}
    paths = []
    for d in C.auth_scan_dirs():
        paths.extend(glob.glob(os.path.join(d, "*.info")))
    for path in paths:
        data = read_auth_file(path)
        if not data:
            continue
        uid = (data.get("account") or {}).get("uid") or "unknown"
        mtime = os.path.getmtime(path)
        if uid not in best or mtime > best[uid][0]:
            best[uid] = (mtime, path, data)
    # config 里的明文昵称，用于回填被客户端加密的账号
    cfg_nick = {}
    try:
        for a in C.load_config().get("accounts") or []:
            if a.get("uid"):
                cfg_nick[a["uid"]] = plain(
                    a.get("label") or a.get("nickname")
                ) or plain(a.get("nickname"))
    except Exception:
        pass
    out = []
    for uid, (mtime, path, data) in sorted(best.items(), key=lambda kv: -kv[1][0]):
        acct = data.get("account") or {}
        nick = plain(acct.get("nickname")) or plain(acct.get("phoneNumber"))
        if not nick:
            nick = cfg_nick.get(uid) or plain(acct.get("uin")) or (uid[:8] + "…")
        phone = plain(acct.get("phoneNumber"))
        days = cred_expiry_days(data)
        out.append(
            {
                "uid": uid,
                "nickname": nick,
                "phone": phone,
                "file": os.path.basename(path),
                "path": path,
                "saved_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "expiry_days": round(days, 1) if days is not None else None,
                "expired": bool(days is not None and days <= 0),
            }
        )
    return out


def capture(account, allow_mismatch=False, source=None):
    """把客户端当前登录（或指定登录态文件）存为该账号的凭证快照。返回 (rc, msg)。"""
    src = source or C.CURRENT_AUTH
    data = read_auth_file(src)
    if not data:
        # 区分「文件无有效登录态」与「登录态存在但缺静态密钥无法解密」，
        # 后者必须直说原因，避免用户反复续期却仍得到信封凭证。
        j = C.load_json(src)
        if j and is_encrypted((j.get("auth") or {}).get("accessToken")):
            return 2, (
                "登录态存在但 accessToken 是加密信封，且本机缺少 data/atrest.key，"
                "无法解密。请先恢复该文件（或重启客户端后重试），否则续期得到的是无效凭证。"
            )
        return 2, (
            "没读到登录态：%s%s（可用扫描功能查看本机已保存的登录态）"
            % (src, "" if source else "，请先打开 WorkBuddy 客户端并登录。")
        )
    cur_uid = (data.get("account") or {}).get("uid")
    nickname = (data.get("account") or {}).get("nickname")
    if account.get("uid") and cur_uid != account["uid"] and not allow_mismatch:
        return 2, (
            "当前登录的是「%s」(%s)，与配置里 %s 的 uid %s 不一致。"
            % (nickname, cur_uid, account["key"], account["uid"])
        )
    C.save_json(os.path.join(C.BASE_DIR, account["cred_file"]), data)
    # 抓完立刻纠 config 里的账号名（避免 UI 长期显示 uin/uid 这类数字串）
    try:
        sync_display_name(account, data)
    except Exception:
        pass
    exp = cred_expiry_days(data)
    shown = cred_display_name(data) or nickname
    return 0, (
        "已保存 %s（%s）凭证：%s / %s / 有效期约 %.0f 天"
        % (account["key"], account.get("label", ""), shown, cur_uid, exp or -1)
    )


def capture_all_possible(cfg=None):
    """一键续期：所有已配置账号，本机还有登录态就直接抓。返回 (ok_keys, skipped)。"""
    cfg = cfg or C.load_config()
    found = {d["uid"]: d for d in discover_accounts()}
    ok, skipped = [], []
    for a in cfg["accounts"]:
        src = found.get(a.get("uid"))
        if not src:
            skipped.append("%s（本机无登录态，需扫码登录）" % a["key"])
            continue
        rc, msg = capture(a, source=src["path"])
        if rc == 0:
            ok.append(a["key"])
        else:
            skipped.append("%s（%s）" % (a["key"], msg[:40]))
    return ok, skipped


def next_key(cfg):
    used = {a["key"] for a in cfg["accounts"]}
    i = 0
    while True:
        i += 1
        key = "account_" + chr(ord("a") + i - 1) if i <= 26 else "account_%d" % i
        if key not in used:
            return key


def add_account_by_uid(uid, label=None, cfg=None):
    """把本机已有登录态的账号加入配置并抓凭证。返回 (account, message)。"""
    cfg = cfg or C.load_config()
    if any(a.get("uid") == uid for a in cfg["accounts"]):
        return None, "该账号已在列表中（uid=%s）" % uid
    src = next((d for d in discover_accounts() if d["uid"] == uid), None)
    if not src:
        return None, "本机没有 uid=%s 的登录态，请先在客户端登录该账号" % uid
    key = next_key(cfg)
    acc = {
        "key": key,
        "label": label or src["nickname"] or src["phone"] or key,
        "uid": uid,
        "nickname": src["nickname"],
        "cred_file": "accounts/%s.cred.json" % key,
        "enabled": True,
        "times": list(
            (cfg["accounts"][0].get("times") if cfg["accounts"] else None) or ["08:00"]
        ),
        "tasks": dict(C.DEFAULT_TASKS),
    }
    cfg["accounts"].append(acc)
    C.save_config(cfg)
    rc, cap_msg = capture(acc, source=src["path"])
    if rc == 0:
        return acc, "已添加 %s（%s）并抓取凭证" % (key, acc["label"])
    return acc, "已加入列表，但凭证抓取失败：%s；请登录该账号后重新抓取" % cap_msg


def auto_sync_accounts(include_expired=False, cfg=None):
    """新账号自动入库：登录态里有、配置里没有的账号自动加入并抓凭证。"""
    cfg = cfg or C.load_config()
    if not cfg.get("auto_add_new_accounts", True):
        return [], ["配置里关闭了自动入库（auto_add_new_accounts=false）"]
    known = {a.get("uid") for a in cfg["accounts"]}
    added, skipped = [], []
    for d in discover_accounts():
        if d["uid"] in known:
            continue
        if d.get("expired") and not include_expired:
            skipped.append("%s（登录态已过期，跳过）" % (d["nickname"] or d["uid"]))
            continue
        acc, msg = add_account_by_uid(d["uid"], cfg=cfg)
        if acc:
            added.append({"key": acc["key"], "label": acc["label"], "uid": d["uid"]})
            cfg = C.load_config()  # add_account_by_uid 已写盘，重读保持同步
        else:
            skipped.append("%s（%s）" % (d["nickname"] or d["uid"], msg))
    return added, skipped


def remove_account(key, cfg=None):
    cfg = cfg or C.load_config()
    left = [a for a in cfg["accounts"] if a["key"] != key]
    if len(left) == len(cfg["accounts"]):
        return False, "列表里没有 %s" % key
    cfg["accounts"] = left
    C.save_config(cfg)
    return True, "已从列表移除 %s（凭证文件保留，未删除）" % key


def set_enabled(key, enabled, cfg=None):
    cfg = cfg or C.load_config()
    acc = C.find_account(cfg, key)
    if not acc:
        return False, "列表里没有 %s" % key
    acc["enabled"] = bool(enabled)
    C.save_config(cfg)
    return True, "%s 已%s" % (key, "启用" if enabled else "停用")
