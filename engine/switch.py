#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端切号：原子替换登录文件 + CDP 校验（照搬旧实现已验证的路径）。

做法：
  1) 备份当前登录文件（preswitch）
  2) 把目标账号的 {auth, account} 拼成官方 workbuddy-desktop.info 结构
  3) 原子替换（写 .tmp → os.replace → chmod 600）
  4) CDP Page.reload 让渲染进程重读
  5) 从左下角 .user-menu-trigger 读回昵称校验（轮询，界面重绘有延迟）

校验口径：界面昵称优先；界面一时没刷出来时回退到登录文件 uid（文件写对 = 客户端下次
请求必用新账号，界面几秒内必跟上）。
"""
import json
import os
import shutil
import time

from core import cdp as C
from core import config as CFG
from core import creds as CR

BACKUP_DIR = CFG.AUTH_BACKUP_DIR
LIVE = CFG.CURRENT_AUTH


def read_live():
    try:
        with open(LIVE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"__error__": str(e)}


def page_account(timeout=25):
    """读界面当前账号。URL 里的 accountSnapshot 是页面加载时生成的（会过期），
    校验一律以左下角 .user-menu-trigger 的昵称为准。"""
    expr = """(function(){
      var out = { stale: true };
      try {
        var m = location.href.match(/accountSnapshot=([^&]+)/);
        if (m) { var o = JSON.parse(decodeURIComponent(decodeURIComponent(m[1]))); out.urlUid = o.uid || null; }
      } catch (e) { out.snapErr = String(e); }
      var el = document.querySelector('.user-menu-trigger');
      out.menuName = el ? (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80) : null;
      out.ready = !!document.querySelector('.conversation-sidebar');
      return out;
    })()"""
    cdp, _ = C.connect()
    try:
        return cdp.evaluate(expr, timeout=timeout)
    finally:
        cdp.close()


def menu_matches(menu_name, want_nick):
    """菜单昵称是否就是目标账号（昵称可能被截断，双向包含判断）。"""
    if not menu_name or not want_nick:
        return False
    m = menu_name.strip()
    w = want_nick.strip()
    return m == w or m in w or w in m


def current_key():
    """客户端当前登录账号在 config 里的 key。

    匹配口径（2026-10-17 修复）：优先用「登录文件里的 uid」与 config 账号比对。
    旧实现只按界面昵称匹配——而客户端 5.6.2+ 加密昵称后 config 里存的是脱敏
    手机号（如 155****6188），与界面真实昵称（如「小宝·觉醒之旅」）永远对不上，
    导致工作台「当前账号」高亮时有时无（昵称恰好是手机号的账号才能匹配上）。
    界面昵称（CDP）只用于展示与最后兜底；CDP 不可达也不再让本函数整体失败。
    """
    menu = None
    try:
        cur = page_account() or {}
        menu = cur.get("menuName")
    except Exception:
        menu = None
    cfg = CFG.load_config()
    # 1) uid 匹配：登录文件是客户端真实身份的来源，不受昵称加密/界面重绘延迟影响
    uid = None
    try:
        live = CR.read_auth_file(LIVE) or read_live()
        uid = ((live or {}).get("account") or {}).get("uid")
    except Exception:
        uid = None
    if uid:
        for a in cfg.get("accounts") or []:
            if a.get("uid") == uid:
                return a.get("key"), menu
    # 2) 兜底：界面昵称匹配（旧逻辑）
    for a in cfg.get("accounts") or []:
        if menu_matches(menu, a.get("nickname") or ""):
            return a.get("key"), menu
    return None, menu


def wait_menu(want_nick, max_sec=12):
    """轮询等界面昵称变成目标账号（连续快速切换时昵称重绘有延迟）。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < max_sec:
        try:
            a = page_account(timeout=10)
            last = a
            if menu_matches((a or {}).get("menuName"), want_nick or ""):
                return a
        except Exception:
            pass
        time.sleep(1.0)
    return last


def wait_ready(max_sec=60):
    """等页面重新可用（reload 之后）。"""
    t0 = time.time()
    while time.time() - t0 < max_sec:
        try:
            a = page_account(timeout=10)
            if a and a.get("ready"):
                return a
        except Exception:
            pass
        time.sleep(1.5)
    return None


def reload_page():
    cdp, _ = C.connect()
    try:
        cdp.call("Page.reload", {"ignoreCache": False}, timeout=20)
        return {"ok": True}
    except Exception as e:
        return {"ok": True, "note": "reload 调用返回 %s（通常无碍）" % e}
    finally:
        try:
            cdp.close()
        except Exception:
            pass


def backup_live(tag="auto", force=False):
    if not os.path.exists(LIVE):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    cur = read_live()
    uid = ((cur.get("account") or {}).get("uid") or "unknown")[:8]
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, "auth_%s_%s_%s.info" % (ts, uid, tag))
    if force or not os.path.exists(dest):
        shutil.copy2(LIVE, dest)
    if tag == "preswitch":
        shutil.copy2(LIVE, os.path.join(BACKUP_DIR, "auth_preswitch.info"))
    return dest


def build_content(target_acct, target_auth, live):
    """拼成官方 {account, auth, accounts, allAccounts} 结构；accounts/allAccounts 按 uid 去重。"""
    others = []
    seen_uids = set()
    for src in list(live.get("accounts") or []) + list(live.get("allAccounts") or []):
        if isinstance(src, dict) and src.get("uid") and src["uid"] != target_acct.get("uid"):
            if src["uid"] not in seen_uids:
                seen_uids.add(src["uid"])
                others.append(src)
    acct = dict(target_acct)
    acct["lastLogin"] = True
    all_accs = others + [acct]
    return {"account": acct, "auth": dict(target_auth),
            "accounts": all_accs, "allAccounts": all_accs}


def atomic_write(path, obj):
    """原子写：先写 tmp，再 rename。Windows 上目标文件可能被客户端锁定，重试几次。"""
    import time as _time
    tmp = path + ".wbswitch.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    # Windows 上客户端正在运行时，os.replace 可能失败（WinError 5 拒绝访问）
    # 重试 5 次，每次间隔 1 秒
    last_err = None
    for i in range(5):
        try:
            os.replace(tmp, path)
            last_err = None
            break
        except PermissionError as e:
            last_err = e
            _time.sleep(1)
    if last_err:
        raise last_err
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def probe_token_alive(cred, timeout=8):
    """切号前探活：确认目标凭证在服务端仍有效（实现见 core.creds.auth_alive）。

    True=可安全写入活登录文件；False=确定被吊销（401/403），必须中止——
    把死 token 写进活登录文件 + reload，客户端会发现会话无效并强制退出登录，
    把当前正在使用的在线账号也踢下线；None=无法判定（网络异常），不阻断。
    """
    return CR.auth_alive(cred.get("auth") or {}, timeout=timeout)


def switch_to(key, reload=True, verify=True, verify_max_sec=None, probe=True):
    # 纵深防御（串行铁则）：切号是破坏性操作，若调用方未持有客户端占用令牌，
    # 说明存在绕过互斥的路径——此时明确报错，而不是默默切错账号。
    # 编排层（run_all / batch）在令牌持有期调用，Web 手动切号同样持令牌，
    # 故这里只校验「有没有令牌」，不校验令牌种类。
    try:
        from core import client_lock as _CL

        if not _CL.holder():
            raise RuntimeError(
                "切号前必须持有客户端占用令牌（core/client_lock.try_acquire）"
            )
    except ImportError:
        pass  # 模块缺失时不阻断（向后兼容）
    except RuntimeError:
        raise

    cfg = CFG.load_config()
    acct = CFG.find_account(cfg, key)
    if not acct:
        return {"ok": False, "err": "配置里没有账号 %s" % key}

    before = page_account()
    out = {"target": key, "want_uid": acct.get("uid"),
           "before": {"page_uid": (before or {}).get("urlUid"),
                      "menu": (before or {}).get("menuName")}}

    # 「已在该账号」以左下角界面昵称为准（urlUid 切换前是过期的）
    if menu_matches((before or {}).get("menuName"), acct.get("nickname") or ""):
        out["ok"] = True
        out["switched"] = False
        out["msg"] = "客户端已经是该账号，无需切换"
        return out

    cred, err = CR.load_cred(acct)
    if err:
        out["ok"] = False
        out["err"] = "目标账号凭证不可用：%s" % err
        return out

    cred_uid = (cred.get("account") or {}).get("uid")
    if cred_uid != acct.get("uid"):
        out["ok"] = False
        out["err"] = "凭证 uid 与配置不符（凭证=%s 配置=%s），已中止" % (cred_uid, acct.get("uid"))
        return out
    if not (cred.get("auth") or {}).get("accessToken"):
        out["ok"] = False
        out["err"] = "目标账号缺少 accessToken"
        return out

    # 服务端探活：确定被吊销（401/403）则中止，绝不把死 token 写进活登录文件
    # （写死 token + reload = 客户端强制退出登录，会连累当前在线账号）。
    # 重新登录该账号的正规路径：客户端内登录 → 工作台「续期」抓新凭证。
    if probe:
        alive, probe_detail = probe_token_alive(cred)
        out["probe"] = probe_detail
        if alive is False:
            out["ok"] = False
            out["err"] = (
                "目标账号凭证已被服务端吊销（%s），切号已中止以保护当前在线账号；"
                "请先在客户端重新登录该账号，再点「续期」抓取新凭证" % probe_detail
            )
            return out

    live = read_live()
    if live.get("__error__"):
        out["ok"] = False
        out["err"] = "读取登录文件失败：%s" % live["__error__"]
        return out

    out["backup"] = backup_live("preswitch", force=True)
    content = build_content(cred.get("account") or {}, cred.get("auth") or {}, live)
    atomic_write(LIVE, content)
    out["written"] = LIVE
    out["wrote_uid"] = content["account"].get("uid")

    if reload:
        out["reload"] = reload_page()
        time.sleep(2.0)
    if verify:
        # 界面昵称重绘有延迟：先轮询等界面；界面没刷出来时回退到登录文件 uid
        # （文件写对 = 客户端下次请求必用新账号），绝不让界面重绘慢误报失败。
        max_sec = verify_max_sec or (20 if reload else 15)
        if reload:
            wait_ready(30)
        after = wait_menu(acct.get("nickname") or "", max_sec=max_sec)
        menu_ok = menu_matches((after or {}).get("menuName"), acct.get("nickname") or "")
        file_ok = (read_live().get("account") or {}).get("uid") == acct.get("uid")
        out["after"] = {"page_uid": (after or {}).get("urlUid"),
                        "menu": (after or {}).get("menuName"),
                        "menu_ok": menu_ok, "file_ok": file_ok}
        if menu_ok:
            out["ok"] = True
        elif file_ok:
            out["ok"] = True
            out["menu_pending"] = True
            out["msg"] = ("登录文件已切换到 %s，界面昵称稍后（几秒内）刷新。"
                          % acct.get("nickname"))
        else:
            out["ok"] = False
            out["err"] = ("切换后文件与界面都未指向目标账号（界面=%r 目标=%r）。"
                          % ((after or {}).get("menuName"), acct.get("nickname")))
        return out
    out["ok"] = True
    return out


def restore():
    """把最近一次切换前的备份写回登录文件并 reload。"""
    src = os.path.join(BACKUP_DIR, "auth_preswitch.info")
    if not os.path.exists(src):
        return {"ok": False, "err": "没有 preswitch 备份，无法回滚"}
    with open(src, encoding="utf-8") as f:
        obj = json.load(f)
    uid = (obj.get("account") or {}).get("uid")
    atomic_write(LIVE, obj)
    out = {"ok": True, "restored_uid": uid, "from": src}
    out["reload"] = reload_page()
    time.sleep(2.0)
    after = wait_ready(60)
    file_ok = (read_live().get("account") or {}).get("uid") == uid
    out["after"] = {"page_uid": (after or {}).get("urlUid"),
                    "menu": (after or {}).get("menuName"),
                    "file_ok": file_ok}
    out["ok"] = file_ok
    return out


