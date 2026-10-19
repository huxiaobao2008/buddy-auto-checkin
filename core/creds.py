#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭证与登录态：读取、扫描、抓取、有效期（零第三方依赖）。

数据来源只有两处（全部本机只读，不外传）：
  1. 客户端登录态活文件 %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\*.info
  2. 本项目 accounts/<key>.cred.json（各账号凭证快照）

行为与旧实现一致：
  - load_cred 校验凭证 uid 与配置 uid 一致（防抓错号）
  - discover_accounts 扫全部 auth_scan_dirs，按 uid 去重取「最优」登录态
  - capture 把当前/指定登录态存为某账号的凭证快照

⚠ 全模块统一口径：判断登录态「新不新」一律看 token 的 expiresAt，**不看文件 mtime**。
  切号备份的 mtime = 备份时刻，备份一份旧凭证同样会刷新 mtime，
  因此「文件最新」可能恰恰是「凭证最旧」。选取优先级统一为：
    ① 未过期优先 → ② 剩余有效期最远 → ③ mtime 最新（仅作同级兜底）
  涉及：discover_accounts / best_source_for_uid / _best_sources_map / _capture_with_fallback。
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


def auth_alive(auth, timeout=8, retries=2, retry_delay=1.5):
    """探活：用 accessToken 调只读状态接口，确认服务端是否仍认可该会话。

    返回 (alive, detail)：
      True  = 服务端认可；False = 确定被吊销（HTTP 401/403，已重试确认）；
      None  = 无法判定（网络异常/非鉴权错误/连接失败），调用方**不阻断**。

    为什么要重试：服务端 401 可能是**临时抖动**而非真的吊销——2026-10-18 凌晨
    account_d/e/f 全部 401，但 17 小时后同样的 token 又全部 200 复活，期间
    用户什么都没做。因此单次 401 不足以判定失效，必须重试确认后才判死；
    而网络异常（超时/连接重置）连一次判定都不做，直接放行，避免误伤正常切号/续期。
    """
    import urllib.error
    import urllib.request

    tok = (auth or {}).get("accessToken")
    dom = (auth or {}).get("domain") or "www.codebuddy.cn"
    if not tok or isinstance(tok, dict):
        return None, "无明文 token，跳过探活"

    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            "https://%s/v2/billing/meter/checkin-activity-status" % dom,
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return True, "HTTP %s" % r.status
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                last = "HTTP %s" % e.code
                if attempt < retries:
                    time.sleep(retry_delay)
                    continue
                return False, last
            return None, "HTTP %s（非鉴权错误，不阻断）" % e.code
        except Exception as e:
            return None, "%s（网络异常，不阻断）" % type(e).__name__
    return None, last or "未知"


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
    now_s = time.time()
    for path in paths:
        data = read_auth_file(path)
        if not data:
            continue
        uid = (data.get("account") or {}).get("uid") or "unknown"
        # ⚠ 不能只按文件 mtime 取最新：切号备份的 mtime 是「备份时刻」，
        # 备份一份旧凭证同样会刷新 mtime，于是「最新文件」可能是「最旧凭证」。
        # 与 best_source_for_uid 保持一致：① 未过期优先 ② 剩余有效期最远 ③ mtime 最新。
        exp = (data.get("auth") or {}).get("expiresAt")
        remain = (exp / 1000 - now_s) if exp else -1e15
        mtime = os.path.getmtime(path)
        rank = (remain > 0, remain, mtime)
        if uid not in best or rank > best[uid][0]:
            best[uid] = (rank, path, data)
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
    # 排序仍按文件 mtime 倒序（展示用），选取逻辑已在上面按「未过期 + 有效期最远」
    for uid, (rank, path, data) in sorted(best.items(), key=lambda kv: -kv[1][0][2]):
        mtime = rank[2]
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


def _all_sources_for_uid(uid):
    """扫全部登录态目录，返回该 uid 的所有可用登录态 (path, expiresAt) 列表。"""
    out = []
    for d in C.auth_scan_dirs():
        for path in glob.glob(os.path.join(d, "*.info")):
            try:
                data = read_auth_file(path)
            except Exception:
                continue
            if not data:
                continue
            if (data.get("account") or {}).get("uid") != uid:
                continue
            exp = (data.get("auth") or {}).get("expiresAt")
            out.append((path, exp))
    return out


def best_source_for_uid(uid):
    """该 uid 当前能取到的最新鲜登录态文件：token 有效期最远优先，其次 mtime 最新。

    续期本质是「复制磁盘上的登录态快照」而非服务端刷新令牌（已验证服务端不会在
    响应里回刷 accessToken，切号也只搬运已有 token）。因此「最新鲜」= 选有效期最远的
    那一份，尽量让续期得到的 token 离过期最远。

    注意：在线号同样走这里，不要强制绑到活文件——活文件往往是当前有效期最短的那
    一份，而备份快照里常有更长的有效期；统一取最远有效期才能给续期最长寿命。
    """
    sources = _all_sources_for_uid(uid)
    if not sources:
        return None
    # 只保留未过期的来源，避免挑中已失效凭证
    now_s = time.time()
    sources = [(p, e) for (p, e) in sources if e and e / 1000 - now_s > 0]
    if not sources:
        return None
    now_ms = time.time() * 1000

    def score(item):
        path, exp = item
        exp_score = (exp - now_ms) if exp else -1e15
        return (exp_score, os.path.getmtime(path))

    return max(sources, key=score)[0]


def capture(account, allow_mismatch=False, source=None):
    """把客户端当前登录（或指定登录态文件）存为该账号的凭证快照。返回 (rc, msg)。

    rc:
      0  = 已写入凭证快照（相较原凭证有变化则代表拿到了更长的有效期；未变化也视为成功，
           因为有效期依然有效）。续期按钮据此统一报成功。
      2  = 无可用登录态 / uid 不符（需先在客户端登录该账号）
      3  = 来源 token 已过期（续期得到的是失效凭证，需重新登录）
    """
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
    # 过期护栏：来源 token 已过期则明确失败，避免把失效凭证当成功写入。
    exp = (data.get("auth") or {}).get("expiresAt")
    if exp and exp / 1000 - time.time() <= 0:
        return 3, (
            "该账号登录态已于 %s 过期，续期得到的仍是失效凭证；请先在客户端登录/切换到该账号后重试"
            % time.strftime("%Y-%m-%d", time.localtime(exp / 1000))
        )
    # 服务端吊销护栏：本地有效期没过 ≠ 服务端还认（2026-10-18 事故：d/e/f 有效期剩
    # 几十天但 token 已全部被吊销）。确定吊销则明确失败，不再报「续期成功」误导。
    alive, alive_detail = auth_alive(data.get("auth") or {})
    if alive is False:
        return 3, (
            "该账号登录态已被服务端吊销（%s），本地快照全部失效；"
            "请先在客户端重新登录该账号，再续期抓取新凭证" % alive_detail
        )
    cur_uid = (data.get("account") or {}).get("uid")
    nickname = (data.get("account") or {}).get("nickname")
    if account.get("uid") and cur_uid != account["uid"] and not allow_mismatch:
        return 2, (
            "当前登录的是「%s」(%s)，与配置里 %s 的 uid %s 不一致。"
            % (nickname, cur_uid, account["key"], account["uid"])
        )
    # 变化检测：仅用于措辞（是否真的换到了更新的 token），不再把「未变化」判为失败——
    # 续期 = 复制磁盘登录态，token 与现有凭证一致也应视为成功（有效期依然有效）。
    cur_cred = C.load_json(os.path.join(C.BASE_DIR, account["cred_file"]))
    cur_exp = (cur_cred.get("auth") or {}).get("expiresAt") if cur_cred else None
    changed = not (
        cur_exp is not None and exp is not None
        and abs((cur_exp - exp) / 1000.0) < 3600  # 有效期差异 < 1 小时，视为同一份
    )
    C.save_json(os.path.join(C.BASE_DIR, account["cred_file"]), data)
    # 抓完立刻纠 config 里的账号名（避免 UI 长期显示 uin/uid 这类数字串）
    try:
        sync_display_name(account, data)
    except Exception:
        pass
    exp_days = cred_expiry_days(data)
    shown = cred_display_name(data) or nickname
    if changed:
        return 0, (
            "已续期 %s（%s）：有效期约 %.0f 天"
            % (account["key"], shown, exp_days or -1)
        )
    return 0, (
        "已续期 %s（%s）：token 未变化，有效期约 %.0f 天"
        % (account["key"], shown, exp_days or -1)
    )


def _live_uid():
    """当前客户端在线账号的 uid（只读活文件，不切号）。"""
    try:
        return (C.load_json(C.CURRENT_AUTH) or {}).get("account", {}).get("uid")
    except Exception:
        return None


def _source_for(account, live_uid=None):
    """为该账号选定续期来源：统一取该 uid 有效期最远（离过期最久）的登录态快照，
    让续期得到的 token 有效期最长。在线与否走同一逻辑——不再把在线号强行绑到有效期
    更短的活文件上（那是之前续期被「砍」成 28 天、且报『已是最新/续不上』的根因）。"""
    uid = account.get("uid")
    if not uid:
        return None, "无"
    src = best_source_for_uid(uid)
    return src, ("快照" if src else "无")


def _best_sources_map():
    """一次全盘扫描，返回 {uid: [候选登录态路径，按优先级降序]}（有效期最远优先，其次 mtime 最新）。

    批量续期专用（2026-10-17）：原实现每个账号都调用 best_source_for_uid →
    每次都把全部登录态目录（约 190 个 .info，含解密）扫一遍，20 个账号就是
    20 × 全量扫描，实测批量续期要 118 秒，Web 端等到超时、看着像「没真正续期」。
    现改为只扫一次，20 个账号共享结果，耗时可降到秒级。

    2026-10-18 改为返回**完整候选列表**而非单个最优：「有效期最远」可能只是
    旧会话的有效期长（55 天期老 token vs 新登录的 28 天期 token），老会话可能
    已被服务端清掉。调用方应按序探活逐个尝试（见 _capture_with_fallback），
    保证「昨天刚登录的新凭证」优先顶上来，而不是抱着旧长命 token 不放。
    """
    now_ms = time.time() * 1000
    cand = {}
    for d in C.auth_scan_dirs():
        for path in glob.glob(os.path.join(d, "*.info")):
            try:
                data = read_auth_file(path)
            except Exception:
                continue
            if not data:
                continue
            uid = (data.get("account") or {}).get("uid")
            exp = (data.get("auth") or {}).get("expiresAt")
            if not uid or not exp or exp / 1000 - time.time() <= 0:
                continue  # 跳过无 uid / 无有效期 / 已过期的来源
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cand.setdefault(uid, []).append(((exp - now_ms, mtime), path))
    return {
        uid: [p for _, p in sorted(items, key=lambda x: x[0], reverse=True)]
        for uid, items in cand.items()
    }


def _capture_with_fallback(account, candidates):
    """按候选顺序逐个尝试抓取；某候选 token 已过期/被服务端吊销（rc=3）就试下一个。

    配合 capture() 的吊销护栏：每个候选写盘前都会先探活，保证落盘的凭证是
    「当前真能用的」，而不是「理论上有效期最长的」。其他错误（rc=2 无登录态/
    uid 不符）说明账号本身没登录好，继续试下一个候选没意义，直接停。
    """
    if not candidates:
        return capture(account)
    rc, msg = 2, "无可用候选"
    for src in candidates:
        rc, msg = capture(account, source=src)
        if rc == 0:
            return rc, msg
        if rc != 3:
            break
    return rc, msg


def capture_best(account):
    """单账号续期入口：在该 uid 的全部本地快照里按优先级逐个探活抓取，取第一个真能用的。

    取代旧的 best_source_for_uid + capture 组合：旧逻辑只取「有效期最远」的一份，
    若那份恰好是已被服务端清掉的旧会话（55 天期老 token），续期就把死凭证落盘，
    而磁盘上昨天刚登录的新凭证反而被跳过（2026-10-18 d/e/f 事故的放大器）。
    """
    uid = account.get("uid")
    if not uid:
        return capture(account)
    now_s = time.time()
    now_ms = now_s * 1000
    scored = []
    for p, exp in _all_sources_for_uid(uid):
        if not exp or exp / 1000 - now_s <= 0:
            continue
        try:
            scored.append(((exp - now_ms, os.path.getmtime(p)), p))
        except OSError:
            continue
    cands = [p for _, p in sorted(scored, key=lambda x: x[0], reverse=True)]
    return _capture_with_fallback(account, cands)


def capture_all_possible(cfg=None):
    """一键续期：所有已配置账号，本机还有登录态就直接抓。返回 (ok_keys, skipped)。

    与单账号「续期」保持一致：统一用 best_source_for_uid 取该 uid 有效期最远的快照，
    让续期得到的 token 有效期最长（在线号同样走这条逻辑，不再强行绑活文件）。
    rc=0 计入 ok 侧；rc=2/3（无登录态/已过期）计入 skipped。
    """
    cfg = cfg or C.load_config()
    src_map = _best_sources_map()
    ok, skipped = [], []
    for a in cfg["accounts"]:
        rc, msg = _capture_with_fallback(a, src_map.get(a.get("uid")))
        if rc == 0:
            ok.append(a["key"])
        else:
            skipped.append("%s（%s）" % (a["key"], msg[:60]))
    return ok, skipped


def renew_all_with_status(cfg=None):
    """逐账号续期明细，供 Web 展示（区分「已续期 / 失败 / 已过期」）。

    2026-10-17：改用 _best_sources_map 一次扫描共享结果（原逐账号全目录扫描，
    20 账号实测 118 秒，Web 请求超时、用户以为没续上）。"""
    cfg = cfg or C.load_config()
    src_map = _best_sources_map()
    rows = []
    for a in cfg["accounts"]:
        cands = src_map.get(a.get("uid"))
        kind = "快照" if cands else "无"
        rc, msg = _capture_with_fallback(a, cands)
        rows.append({
            "key": a["key"],
            "rc": rc,
            "message": msg,
            "source": kind,
            "status": {0: "已续期", 2: "失败", 3: "已过期"}.get(rc, "失败"),
        })
    return rows


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
