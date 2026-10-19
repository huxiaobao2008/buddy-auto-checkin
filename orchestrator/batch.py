#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单账号批量执行器：切号 → 账号硬闸 → 任务并行发射/串行面板 → 领取 → 清理闸 → 切回。

任务分两类：
  会话型（可并行）：template_5 / Model_chat_GLM5.2 / expert_5 / Expert_lighthouse /
                    create_canvas / playbook_prompt
    → 先逐个「建会话 + 发消息」（发射），再统一等全部生成收尾（等待），
      总时长 ≈ N×setup + max(wait)，而不是 N×(setup+wait)。
  面板型（必须串行）：Library_read / Hp_Appearance / Buddy_App·QQ / automation_1 /
                      skill_1
    → 改全局 UI 状态，只能一个个来。
    （注：盲盒/buddy_blindbox 是 API 任务，由 engine.missions_api 管线负责，
      不在客户端 UI 任务清单里，故此处不含 open_blindbox。）
  专家团（严格串行）：Expert_team_use_3 —— 设计约束「同一时刻只允许一个团队未收尾」。

账号硬闸：切号后必须确认当前登录昵称 == 目标账号，对不上拒绝执行（防烧错号额度）。
清理闸：该账号成长任务全部完成 + 无可领积分之后，才清理自动化遗留会话。
"""

import datetime
import json
import os
import time

from core import config as CFG
from engine import driver as U
from engine import switch as AS
from engine import tasks as T
from missions import chat as M_CHAT
from missions import experts as M_EXP
from missions import panels as M_PANEL
from missions import scenes as M_SCENE
from missions import skill as M_SKILL
from missions import team as M_TEAM

# 需要「在别的界面点」的面板型任务
PANEL_CODES = (
    "Library_read",
    "Hp_Appearance",
    "Buddy_App",
    "Buddy_App_QQ",
    "automation_1",
    "skill_1",
)
# 会话型任务（可并行发射）
CHAT_CODES = (
    "template_5",
    "Model_chat_GLM5.2",
    "expert_5",
    "Expert_lighthouse",
    "create_canvas",
    "playbook_prompt",
    "RichMeow_Chat",
    "chat_5",
)
UI_CODES = CHAT_CODES + ("Expert_team_use_3",) + PANEL_CODES

# 桌面端自动化做不到的任务（需外部动作）
# 2026-09-25: 改成从 config.json 的 skip_tasks.codes 动态读取，不写死
#2026-10-02 核验：实际生效的判断是下方 _skip_codes()（读 config），
#             本常量恒为空列表，故下方「UNAUTOMATABLE_CODES」分支为死代码。
#             保留常量与分支仅为兼容旧配置，显式标注以免误判为有效逻辑。
UNAUTOMATABLE_CODES = []


# template_5 用的模板池：动态枚举（list_templates_live）→ 产品级通用兜底（U.TEMPLATE_NAMES）
# 两种来源都按内容红线过滤媒体向分类（视频生成/绘图/海报等），避免点开媒体模板烧积分。
def _template_pool(key=None):
    """模板池（跨轮次去重）：返回该账号【未用过】的模板分类。

    - 池源：动态枚举（list_templates_live）→ 产品级通用兜底（U.TEMPLATE_NAMES）
    - 过滤媒体向分类（视频生成/绘图/海报等），避免点开媒体模板烧积分
    - key 为 None：返回全池（不读账号记录）
    - key 非空：剔除该账号已用过（服务端已计分）的分类，只留未用过的。
      修复 template_5 重复分类不计分：旧实现用 idx 取模，服务端 cur=None 时
      idx 恒为 0 → 永远用第一个分类；且无跨轮次记忆 → 同一分类反复使用，
      服务端按「5 个不同模板」计分，重复分类不递增。
    """
    try:
        live = U.list_templates_live()
        if live:
            pool = [n for n in live if not U.media_blocked(n)] or list(U.TEMPLATE_NAMES)
        else:
            pool = [n for n in U.TEMPLATE_NAMES if not U.media_blocked(n)]
    except Exception:
        pool = [n for n in U.TEMPLATE_NAMES if not U.media_blocked(n)]
    if key:
        used = _used_templates(key)
        pool = [n for n in pool if n not in used]
    return pool


# template_5 已用模板分类：账号级持久化（data/template_usage.json）
# 结构 {"account_key": ["分类名", ...]}。发起成功（服务端大概率计分）才标记，
# 失败不标记——下次还能再试该分类。跨轮次（跨天跑）靠它跳过重复分类。
TEMPLATE_USAGE_FILE = os.path.join(CFG.DATA_DIR, "template_usage.json")


def _load_template_usage():
    return CFG.load_json(TEMPLATE_USAGE_FILE, {}) or {}


def _used_templates(key):
    try:
        return set(_load_template_usage().get(key) or [])
    except Exception:
        return set()


def _save_template_usage(data):
    try:
        CFG.save_json(TEMPLATE_USAGE_FILE, data)
    except Exception:
        pass


def _mark_template_used(key, tpl):
    if not key or not tpl:
        return
    try:
        data = _load_template_usage()
        used = set(data.get(key) or [])
        used.add(tpl)
        data[key] = sorted(used)
        _save_template_usage(data)
    except Exception:
        pass


def _reset_template_usage(key):
    try:
        data = _load_template_usage()
        data.pop(key, None)
        _save_template_usage(data)
    except Exception:
        pass


# expert_5 已召唤专家名：账号级持久化（data/expert_usage.json）
# 结构 {"account_key": ["专家名", ...]}。发起成功（服务端大概率计分）才标记，
# 失败不标记——下次还能再召同一专家。跨轮次靠它跳过重复专家（服务端按「不同
# 专家」计分，重复召唤是白跑）。召唤前 JS 先滚动加载全量卡片、跳过已用名单，
# 从「未召唤过」的专家里选卡；名单全用过仍不达标说明服务端计分口径漂移，
# 清空该账号记录重来一轮，避免卡死。
EXPERT_USAGE_FILE = os.path.join(CFG.DATA_DIR, "expert_usage.json")


def _load_expert_usage():
    return CFG.load_json(EXPERT_USAGE_FILE, {}) or {}


def _used_experts(key):
    try:
        return set(_load_expert_usage().get(key) or [])
    except Exception:
        return set()


def _save_expert_usage(data):
    try:
        CFG.save_json(EXPERT_USAGE_FILE, data)
    except Exception:
        pass


def _mark_expert_used(key, name):
    if not key or not name:
        return
    try:
        data = _load_expert_usage()
        used = set(data.get(key) or [])
        used.add(str(name))
        data[key] = sorted(used)
        _save_expert_usage(data)
    except Exception:
        pass


def _reset_expert_usage(key):
    try:
        data = _load_expert_usage()
        data.pop(key, None)
        _save_expert_usage(data)
    except Exception:
        pass


# playbook_prompt 用首页第几张灵感卡片（0 起）。挑最轻的（精读笔记卡），省十几分钟。
PB_CASE = 2

BUDDY_PAIR = ("Buddy_App", "Buddy_App_QQ")


# --------------------------------------------------------------------------- #
# 进度查询
# --------------------------------------------------------------------------- #
def fetch_map(key):
    cfg = CFG.load_config()
    acct = CFG.find_account(cfg, key)
    if not acct:
        return {}, "配置里没有账号 %s" % key
    from core import creds as CR

    cred, err = CR.load_cred(acct)
    if err:
        return {}, "凭证不可用：%s" % err
    lst, err = T.fetch_tasks(cred, cfg)
    if err:
        return {}, err
    return {t["code"]: t for t in lst}, ""


def prog(key, code):
    m, err = fetch_map(key)
    if err:
        return None, None, "err:" + err
    t = m.get(code)
    if not t:
        return None, None, "not_found"
    p = t.get("progress") or {}
    return p.get("current"), p.get("target"), t.get("status")


def done(key, code):
    cur, tgt, st = prog(key, code)
    if st == "claimed" or st == "completed":
        return True
    return bool(cur is not None and tgt and cur >= tgt)


def _skip_codes():
    try:
        blk = (CFG.load_config() or {}).get("skip_tasks") or {}
        return {c for c in (blk.get("codes") or []) if c}
    except Exception:
        return set()


def _undone_ui_codes(key, acct, cfg, log=print):
    """从服务端实时任务清单动态取该账号仍未达标的 UI 可自动化 codes。

    返回 (codes, err)：
      codes 为非空列表 → 该账号真正未达标的 UI 任务，只执行这些；
      codes 为空列表 → 清单已拉取且未达标项为 0（全部完成/跳过/锁定），仅剩领取收尾；
      codes 为 None   → 清单拉取失败，无法动态判定（调用方决定是否回退硬编码）。
    """
    from core import creds as CR

    cred, err = CR.load_cred(acct)
    if not cred:
        return None, "凭证不可用：%s" % (err or "无凭证")
    tl, e2 = T.fetch_tasks(cred, cfg)
    if e2:
        return None, e2
    out = []
    for t in tl:
        c = t.get("code")
        if not c or c not in UI_CODES:
            continue  # 非 UI 驱动任务（签到/盲盒/API 管线）不进执行清单
        if t.get("paid") or t.get("expired") or t.get("locked") or t.get("skip"):
            continue
        if t.get("status") in ("claimed", "completed"):
            continue
        p = t.get("progress") or {}
        cur, tgt = p.get("current"), p.get("target")
        if tgt and cur is not None and cur >= tgt:
            continue  # 进度已达标，等领取即可
        out.append(c)
    return out, ""


def _resolve_ui_codes(key, acct, cfg, log=print):
    """动态清单决策：拉取该账号真实未达标 UI 任务；拉取失败回退硬编码全量。

    返回 (codes, source)：
      source == "dynamic"  → codes 来自服务端实时清单（可能为空列表 = 只剩领取收尾）；
      source == "fallback" → 清单拉取失败，回退硬编码全量（文档/网络异常时的保守兜底）。
    """
    dyn, dyn_err = _undone_ui_codes(key, acct, cfg, log)
    if dyn is None:
        # 登录态失效（401/403）= 凭证已被服务端吊销：绝不能回退全量清单继续跑——
        # 后续切号会把死 token 写进活登录文件，客户端 reload 后强制下线，
        # 把当前正在使用的在线账号也踢掉（2026-10-18 凌晨真实事故：d/e/f 失效
        # 导致在线的 account_o 被挤下线）。此时直接跳过本账号 UI 阶段。
        if dyn_err and ("登录态失效" in dyn_err or "401" in dyn_err or "403" in dyn_err):
            log(
                "  [动态清单] 拉取失败（%s）——凭证已被服务端吊销，"
                "本账号跳过 UI 阶段（不切号，保护当前在线账号）" % dyn_err
            )
            return [], "auth_dead"
        log("  [动态清单] 拉取失败（%s），回退硬编码全量清单" % dyn_err)
        return ["template_5", "Model_chat_GLM5.2"] + list(UI_CODES), "fallback"
    log(
        "  [动态清单] 本账号未达标 UI 任务 %d 项：%s"
        % (len(dyn), ", ".join(dyn) if dyn else "（无 → 只剩领取/收尾）")
    )
    return dyn, "dynamic"


# --------------------------------------------------------------------------- #
# 账号硬闸
# --------------------------------------------------------------------------- #
def verify_account_gate(key, log, wait_sec=None):
    """确认客户端当前登录的账号就是 key 对应的账号。

    2026-09-24 修复：用户自定义昵称后，左下角显示的是"小宝·觉醒之旅"这类自定义名，
    不是 config 里的手机号。除了界面昵称匹配，还要检查登录文件 uid——
    文件写对 = 客户端下次请求必用新账号，界面昵称只是显示层。
    """
    cfg = CFG.load_config()
    acct = CFG.find_account(cfg, key)
    if not acct:
        return False, "配置里没有账号 %s" % key

    # 文件 uid 校验：登录文件里的 account.uid 就是当前登录账号
    try:
        from engine import switch as SW

        live = SW.read_live()
        live_uid = (live.get("account") or {}).get("uid")
        want_uid = acct.get("uid")
        if live_uid and want_uid and live_uid == want_uid:
            now_nick = U.current_account_name()
            log("    [账号闸] 文件uid通过（当前界面=%s）" % (now_nick or "?"))
            return True, ""
    except Exception as e:
        log("    [账号闸] 文件uid校验异常: %s，回退昵称核对" % e)

    acct_nick = acct.get("nickname") or ""
    if not acct_nick:
        return True, "目标账号无 nickname，跳过核对"

    now_nick = U.current_account_name()
    if not (now_nick and str(acct_nick) in str(now_nick)):
        _wait = (
            wait_sec
            if wait_sec is not None
            else int(os.environ.get("BR_NICK_GATE_WAIT", "30"))
        )
        log(
            "    [账号闸] 当前=%s，期望=%s，等待刷新（最多 %ds）"
            % (now_nick, acct_nick, _wait)
        )
        _dl = time.time() + _wait
        while time.time() < _dl:
            if U.stop_requested():
                break
            time.sleep(3)
            now_nick = U.current_account_name()
            if now_nick and str(acct_nick) in str(now_nick):
                break

    if now_nick and str(acct_nick) in str(now_nick):
        log("    [账号闸] 通过（当前=%s）" % now_nick)
        return True, ""
    log("    [账号闸] 拒绝执行：当前=%s，期望=%s" % (now_nick, acct_nick))
    return False, "账号核对失败：当前=%s 期望=%s" % (now_nick, acct_nick)


# --------------------------------------------------------------------------- #
# 会话型任务：并行发射（发完即返回，不等结果）+ 统一等待 + 生效验证
# --------------------------------------------------------------------------- #
# 完成保障：除专家团与 skip 任务外，任务未完成后自动重试（有上限与间隔，防无限循环）
TASK_RETRY_MAX = int(
    os.environ.get("BR_TASK_RETRY", "3")
)  # 每任务最多尝试轮数（含首轮）
TASK_RETRY_GAP = float(os.environ.get("BR_TASK_RETRY_GAP", "20"))  # 重试间隔（秒）
# 环境类失败（非程序问题，重试无意义）：额度用尽 / 登录态失效 / 账号不符 / 用户占用
FATAL_ERR_PAT = (
    "额度已用尽",
    "quota",
    "登录态失效",
    "auth_expired",
    "账号核对失败",
    "已让路",
)
# 审计豁免清单（动态，不写死模型）：登录态/账号不符/用户占用一律豁免；
# 「额度用尽」是否豁免取决于 config.json 的 free_quota ——
# 模型免费无额度的环境（free_quota=true）里它一旦出现即属异常，照常判 bug 告警；
# 开源默认 false（使用者可能用付费/限量模型），按环境问题豁免，不刷告警。
AUDIT_EXEMPT_BASE = ("登录态失效", "auth_expired", "账号核对失败", "已让路")


def _sleep_gap(seconds, stop=None):
    """分段睡眠（每秒查一次停止信号）。返回 False=被停止打断。"""
    dl = time.time() + max(0.0, seconds)
    while time.time() < dl:
        if (stop and stop()) or U.stop_requested():
            return False
        time.sleep(1.0)
    return True


def _is_fatal_err(*texts):
    blob = " ".join(str(t or "") for t in texts)
    return any(pat in blob for pat in FATAL_ERR_PAT)


def _audit_exempt(*texts):
    """审计豁免判定：登录态/账号不符/用户占用一律豁免；
    额度类仅当 free_quota=false（付费/限量模型环境）才豁免。"""
    blob = " ".join(str(t or "") for t in texts)
    pats = list(AUDIT_EXEMPT_BASE)
    try:
        free_quota = bool((CFG.load_config() or {}).get("free_quota"))
    except Exception:
        free_quota = False
    if not free_quota:
        pats += ["额度已用尽", "quota"]
    return any(pat in blob for pat in pats)


def _cleanup_new_convs(snapshot, log):
    """删除快照之外的所有新增会话（原有会话一律保留）。

    deleteNewTask 只删任务区第一条且必须前缀匹配（防误删），
    新增会话都比原有的新、排在前面，按侧栏显示顺序逐条删即可。
    返回 (删除数, 失败数)。
    """
    try:
        after = U.task_titles() or []
    except Exception as e:
        log("  [清理] 读会话列表失败：%s" % str(e)[:80])
        return 0, 0
    old = set(snapshot or [])
    new = [t for t in after if t and t not in old]
    removed = failed = 0
    for t in new:
        try:
            r = U.delete_new_task(t)
            if isinstance(r, dict) and r.get("ok"):
                removed += 1
            else:
                failed += 1
                log(
                    "  [清理] 跳过「%s」：%s"
                    % (str(t)[:24], (r or {}).get("err") if isinstance(r, dict) else r)
                )
        except Exception as e:
            failed += 1
            log("  [清理] 删除「%s」异常：%s" % (str(t)[:24], str(e)[:80]))
    return removed, failed


# --------------------------------------------------------------------------- #
# 会话型任务：串行发射 → 统一等待 → 验证（生效才删）→ 下一个
# --------------------------------------------------------------------------- #
# 事件驱动：不再用固定时间窗（SCORE_SETTLE 已废弃），改为 CDP 轮询观察
# 真实交付完成 + 服务端回读确认积分到账。
LAUNCH_GAP = float(os.environ.get("BR_LAUNCH_GAP", "2"))  # 串行发射间隔（秒）


def _run_chat_unit(key, code, idx, log, poll=False):
    """执行一个会话单元。

    poll=False=发射即走（默认标准逻辑：验证会话发起成功立刻返回，不原地等收尾、不阻塞，快速串行下一个任务）
    poll=True=发射并原地等收尾（仅特殊调试场景使用）
    返回 {sent, ok, title, err, run, env_unavailable}。
    """

    def _pack(r, title_key="created_title"):
        return {
            "sent": bool(r.get("sent") or r.get("ok")),
            "ok": bool(r.get("ok") or r.get("sent")),
            "title": r.get(title_key),
            "err": r.get("err") or r.get("preErr"),
            "run": r.get("run"),
            "env_unavailable": bool(r.get("env_unavailable")),
        }

    _tag = "发射" if not poll else "原地等收尾"
    if code == "template_5":
        log("      日常任务分类下选未使用模板，做同款（%s）..." % _tag)
        r = M_SCENE.do_generic_template(
            key,
            preferred_cats=["日常任务", "日常办公", "网站设计", "设计创意"],
            poll=poll
        )
        return {
            "sent": bool(r.get("ok")),
            "ok": bool(r.get("ok")),
            "title": r.get("created"),
            "err": r.get("err"),
            "run": None,
        }

    if code == "Model_chat_GLM5.2":
        log("      GLM-5.2 对话（%s）..." % _tag)
        return _pack(
            M_CHAT.do_model_chat(do_delete=False, stop=U.stop_requested, poll=poll)
        )

    if code == "expert_5":
        used = _used_experts(key)
        log("      召唤专家（已用 %d 位，%s）..." % (len(used), _tag))
        r = M_EXP.run_expert_once(
            kind="expert",
            idx=idx,
            delete_after=False,
            verbose=False,
            poll=poll,
            used=sorted(used),
        )
        pre = r.get("pre") or {}
        name = pre.get("name")
        # 召唤并发起成功才标记该专家名已用（服务端大概率计分）；
        # 召唤步骤明确报告「名单全用过」时清空该账号记录重来一轮。
        if (r.get("sent") or r.get("ok")) and name:
            _mark_expert_used(key, name)
            log("      专家「%s」已发起，标记已用（%s）..." % (name, _tag))
        elif "均已被召唤过" in str(pre.get("err") or ""):
            _reset_expert_usage(key)
            log("      专家池已全部用过但未达标，清空记录重来（%s）..." % _tag)
        return _pack(r)

    if code == "Expert_lighthouse":
        log("      授权「Lighthouse 运维」连接器 → 召唤「腾讯轻量云」（%s）..." % _tag)
        # 2026-09-26 修复：旧实现直接 run_expert_once 未传 need_connector，
        # 连接器没授权就发消息 = 假跑不计分。必须走 do_expert_lighthouse
        # （need_connector=True 是服务端硬条件，连不上绝不发消息）。
        return _pack(
            M_EXP.do_expert_lighthouse(poll=poll, delete_after=False)
        )

    if code == "create_canvas":
        log("      设计创意分类下选模板，做同款（%s）..." % _tag)
        r = M_SCENE.do_generic_template(
            key,
            preferred_cats=["设计创意", "网站设计"],
            poll=poll
        )
        return {
            "sent": bool(r.get("ok")),
            "ok": bool(r.get("ok")),
            "title": r.get("created"),
            "err": r.get("err"),
            "run": None,
        }

    if code == "playbook_prompt":
        log("      灵感卡片（第 %d 张）→ 做同款（%s）..." % (PB_CASE + 1, _tag))
        return _pack(
            M_SCENE.do_playbook_prompt(
                case_idx=PB_CASE, verbose=False, delete_after=False, poll=poll
            ),
            title_key="created",
        )

    return {
        "sent": False,
        "ok": False,
        "title": None,
        "err": "该 code 不是会话型任务",
        "run": None,
    }


def judge_task(key, code):
    """THING 1 · 判断（只读、不执行）：回服务端真实进度，回答「这个任务真正发起/达标了没有」。

    只回答成败，不做任何动作、不据此自报成功。成功与否的唯一标准是服务端进度
    delta>0 或已 claimed/completed——本地 send 成功在这里一律不算数。
    """
    cur, tgt, st = prog(key, code)
    done = bool(
        st in ("claimed", "completed") or (cur is not None and tgt and cur >= tgt)
    )
    return {"cur": cur, "tgt": tgt, "st": st, "done": done}


def initiate_task(key, code, idx, log, poll=False):
    """THING 2 · 发起真正的任务（默认发射即走：验证会话真的创建、任务开始执行就返回，不原地等AI完整回复）
    只回报「动作跑没跑 / 环境是否不支持」，绝不据此判定任务成败——成败由 judge_task 统一轮询服务端说了算。

    返回 {ran, env_unavailable, err, title, run}。
    """
    try:
        r = _run_chat_unit(key, code, idx=int(idx or 0), log=log, poll=poll)
        return {
            "ran": bool(r.get("sent") or r.get("ok")),
            "env_unavailable": bool(r.get("env_unavailable")),
            "err": r.get("err"),
            "title": r.get("title"),
            "run": r.get("run"),
        }
    except Exception as e:
        return {"ran": False, "err": "%s: %s" % (type(e).__name__, str(e)[:160])}


def _wait_all_conversations(log, stop=None, timeout=600):
    """用 CDP 轮询检查是否还有任何对话在生成中。全部交付完成后返回。

    事件驱动：不等固定时间，观察到所有对话都停止生成才返回。
    """
    start = time.time()
    while time.time() - start < timeout:
        if stop and stop():
            log("    [停止] 等待阶段收到停止指令")
            return False
        try:
            r = U.js(
                """(() => {
                const b = document.querySelector('button.cr-send-button');
                const proc = !!document.querySelector('.cr-agent--processing');
                const stopBtn = b ? b.className.toString().indexOf('--stop') !== -1 : false;
                return { processing: proc, stopBtn: stopBtn, anyActive: proc || stopBtn };
            })()""",
                timeout=30,
            )
        except Exception as e:
            log("    [等待·异常] CDP 查询失败：%s" % str(e)[:60])
            time.sleep(3)
            continue
        if isinstance(r, dict) and not r.get("anyActive"):
            log("    [交付] 所有对话已交付完成（%.1fs）" % (time.time() - start))
            return True
        time.sleep(2)
    log("    [超时] 等待对话交付超时（%ds），可能有对话仍在生成中" % timeout)
    return False


def run_chat_batch(key, codes, do_delete, log, deadline=None, stop=None):
    """会话任务标准调度（判断 与 发起严格分离，严格串行发射，发射即走，不原地等）：
    ① 判断：回读服务端进度，筛选未达标任务
    ② 逐个串行发射：点发送后验证会话真的创建、任务开始执行，立刻发射下一个，不等AI回复、不等交付
    ③ 所有任务全部发射完成后，由上层统一轮询服务端进度，未计分任务后续统一补做
    「成功」唯一标准是服务端进度，本地send成功不算最终完成。
    """
    recs = []
    # ① 判断：哪些需要做
    pending = []
    for code in codes:
        if stop and stop():
            log("    [停止] 收到停止指令，中止调度")
            break
        # 三类人工跳过任务（需求六）：实际以 config.json 的 skip_tasks.codes 为准，
        # 此处保留为兜底并显式说明当前 UNAUTOMATABLE_CODES 恒空（死代码）。
        if code in _skip_codes() or code in UNAUTOMATABLE_CODES:
            reasons = {
                "wb_wechat_oa_subscribe_task": "需微信扫码，必须人工完成",
                "Expert_Philanthropy": "涉及付费捐款，无法自动完成",
                "black_cat": "任务本身无积分",
            }
            reason = reasons.get(code, "无法自动完成")
            log("    [跳过] %s（%s）" % (T.title_of(code), reason))
            continue
        j = judge_task(key, code)
        if j["st"] == "not_found":
            log("    [跳过] %s 不在任务清单" % T.title_of(code))
            recs.append({"code": code, "skipped": "not_found", "ok": True})
            continue
        if j["done"]:
            log("    [跳过] %s 已达标 %s/%s" % (T.title_of(code), j["cur"], j["tgt"]))
            recs.append({"code": code, "skipped": "已达标", "ok": True})
            continue
        pending.append((code, j))
        log(
            "    [判断] %s 未达标 %s/%s → 需发起"
            % (T.title_of(code), j["cur"], j["tgt"])
        )

    # ② 逐个串行发射：必须真实验证「会话已创建、任务已发起」才进入下一个；
    #    点击失效/未验证成功立刻重试，绝不盲发、绝不把失败打印成「验证发起成功」。
    for code, j in pending:
        if stop and stop():
            log("    [停止] 收到停止指令，中止发起")
            break
        rec = {"code": code, "before": {"current": j["cur"], "target": j["tgt"]}}
        ini = None
        attempt = 0
        aborted = False
        for attempt in range(1, TASK_RETRY_MAX + 1):
            if stop and stop():
                aborted = True
                break
            ini = initiate_task(
                key, code, int(j["cur"] or 0), log, poll=False
            )
            if ini.get("ran"):
                break
            # 未真实发起：如实打印真实 err，立刻重试，不把失败当成功、不盲发下一个
            log(
                "    [发起失败] %s 第 %d/%d 次未真实验证发起（err=%s），立刻重试"
                % (
                    T.title_of(code),
                    attempt,
                    TASK_RETRY_MAX,
                    str(ini.get("err"))[:80],
                )
            )
            if ini.get("env_unavailable"):
                # 环境不支持，不空转重试
                break
            if attempt < TASK_RETRY_MAX:
                if not _sleep_gap(TASK_RETRY_GAP, stop):
                    aborted = True
                    break
        if ini is None:
            ini = {"ran": False, "err": "未发起（停止/中断）"}
        rec.update(
            {
                "ran": ini.get("ran"),
                "err": ini.get("err"),
                "env_unavailable": ini.get("env_unavailable"),
                "title": ini.get("title"),
                "run": ini.get("run"),
                "attempts": attempt,
            }
        )
        if rec.get("ran"):
            log(
                "    [发起成功] %s 已真实验证会话创建、任务发起（第 %d 次尝试），"
                "立刻进入下一个，不等回复"
                % (T.title_of(code), attempt)
            )
        else:
            log(
                "    [发起未成功] %s 重试 %d 次后仍未真实验证发起（err=%s）——"
                "不盲发，如实记录，继续后续任务"
                % (T.title_of(code), attempt, str(rec.get("err"))[:80])
            )
        if rec.get("env_unavailable"):
            log(
                "    [环境] %s 环境不支持（%s）——不空转"
                % (T.title_of(code), str(rec.get("err"))[:60])
            )
        recs.append(rec)
        if aborted:
            log("    [停止] 收到停止指令，中止发起")
            break
    return recs


# --------------------------------------------------------------------------- #
# 完成审计：凡「启动过却未完成」的任务（专家团/skip 除外）= 程序 bug → 详细日志 + 告警
# --------------------------------------------------------------------------- #
def audit_account_completion(key, acct, cfg, records, log, stopped=False):
    """对单账号做完成审计。返回 bug 清单（[] = 无异常）。

    判定规则：本次启动过（发射/执行过、非"已达标跳过"）的任务，在重试耗尽后
    查服务端状态仍未完成 → 判定为程序 bug。排除：专家团（自带限次）、
    skip_tasks/不可自动化任务、手动停止本轮、环境类失败（额度/登录态/让路）。
    动作：tasks.jsonl 记 audit_bug（账号/任务/状态/进度/时间戳/错误）+ 桌面/日志/webhook 告警。
    """
    if stopped:
        return []
    from core import logs as L
    from core import notify as N

    bugs = []
    # 0) 阶段级崩溃（整个阶段异常中断）本身就是 bug
    for r in records:
        if r.get("code") in ("chat_phase", "panel_phase") and r.get("err"):
            bug = {
                "account": key,
                "code": r["code"],
                "title": "阶段级异常",
                "status": "phase_crashed",
                "progress": "-",
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "err": str(r.get("err"))[:160],
            }
            bugs.append(bug)
            log(
                "  [审计·BUG] %s 阶段异常中断：%s"
                % (T.title_of(r["code"]), bug["err"][:80])
            )

    # 1) 任务级：启动过但未完成
    attempted = {}
    for r in records:
        c = r.get("code")
        if not c or c in ("chat_phase", "panel_phase"):
            continue  # 阶段级任务不参与任务级审计
        if c in _skip_codes() or c in UNAUTOMATABLE_CODES:
            continue
        if r.get("skipped") or r.get("env_unavailable"):
            continue
        attempted.setdefault(c, r)
    if not attempted:
        return bugs
    m, err = fetch_map(key)
    if err:
        log("  [审计] 任务状态读取失败（%s），跳过审计" % err[:60])
        return bugs
    for code, rec in attempted.items():
        t = m.get(code) or {}
        p = t.get("progress") or {}
        is_done = t.get("status") in ("claimed", "completed") or bool(
            p.get("target") and (p.get("current") or 0) >= p["target"]
        )
        if is_done:
            continue
        # 豁免判据取 rec 里真实存在的字段：会话任务记在 run 里（以前写的是 wait，
        # 那个键根本不存在 → 豁免永远判不中，额度/让路类失败被误判成程序 BUG 刷告警）
        if _audit_exempt(rec.get("err"), (rec.get("run") or {}).get("reason")):
            log(
                "  [审计] %s 未完成但属豁免原因（登录态/账号不符/用户占用/付费额度），不判 bug"
                % T.title_of(code)
            )
            continue
        bug = {
            "account": key,
            "code": code,
            "title": t.get("title"),
            "status": t.get("status"),
            "progress": "%s/%s" % (p.get("current"), p.get("target")),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "err": str(rec.get("err") or "")[:120],
        }
        bugs.append(bug)
        log(
            "  [审计·BUG] %s 已启动但未完成（%s，状态 %s）——已记 tasks.jsonl 并告警"
            % (T.title_of(code), bug["progress"], t.get("status"))
        )
    # 2) 落日志 + 告警（任何一环失败都不影响主流程）
    for bug in bugs:
        try:
            L.log_step(
                key,
                acct.get("nickname") or "",
                bug["code"],
                bug.get("title") or bug["code"],
                "audit_bug",
                "fail",
                "程序BUG：任务已启动但未完成（重试 %d 轮后）" % TASK_RETRY_MAX,
                extra=bug,
            )
        except Exception:
            pass
        try:
            N.notify(
                "[buddy-auto] 程序BUG待排查",
                "账号 %s 任务 %s 启动后未完成（%s，状态 %s）"
                % (key, bug["code"], bug["progress"], bug.get("status")),
            )
        except Exception:
            pass
    return bugs


# --------------------------------------------------------------------------- #
# 面板型任务（串行）
# --------------------------------------------------------------------------- #
def _run_panel_code(key, code, cur, tgt, log):
    if code == "Library_read":
        log("    打开资料库并读完文档")
        r = M_PANEL.do_library_read()
        return {"ok": bool(r.get("ok")), "doc": r.get("doc"), "err": r.get("err")}
    if code == "Hp_Appearance":
        log("    切到「和平精英激战金秋」主题")
        r = M_PANEL.do_hp_appearance(verbose=False)
        return {
            "ok": bool(r.get("ok")),
            "theme": r.get("theme"),
            "err": r.get("err"),
            "env_unavailable": bool(r.get("env_unavailable")),
        }
    if code in BUDDY_PAIR:
        log(
            "    发现应用 → 企鹅教师助手 → 进入（一个动作同时完成 Buddy_App 与 Buddy_App_QQ）"
        )
        r = M_PANEL.do_buddy_app("企鹅教师助手")
        return {
            "ok": bool(r.get("ok")),
            "clicked": r.get("clicked"),
            "err": r.get("err"),
        }
    if code == "automation_1":
        # 需求三.5：临时内容必须等积分到账后才能删。
        # 因此这里只创建、不删除（keep 默认 True）；
        # 实际删除由 run_ui_code_once 末尾的「清理闸」在确认积分到账后统一执行。
        log("    创建 1 个自动化任务（暂不删除，待积分到账后由清理闸统一删除）")
        r = M_PANEL.do_automation_1_and_clean()
        return {"ok": bool(r.get("ok")), "name": r.get("name"), "err": r.get("err")}
    if code == "skill_1":
        log("    技能页使用一个已安装技能（打开会话 → 发一句话）")
        r = M_SKILL.do_skill_1(verbose=False)
        return {"ok": bool(r.get("ok")), "skill": r.get("skill"), "err": r.get("err")}
    return {"skipped": "该 code 没有面板执行策略"}


def run_panel_batch(key, codes, log, deadline=None, stop=None):
    """面板任务串行批（带完成保障重试）：单任务失败重试 TASK_RETRY_MAX 次，
    间隔 TASK_RETRY_GAP 秒；环境类失败（额度/登录态）不重试。"""
    recs = []
    for code in codes:
        if stop and stop():
            log("    [停止] 收到停止指令，中止面板任务")
            break
        if deadline and time.time() > deadline:
            log("    [预算] 账号时间预算已到，中止面板任务")
            break
        if code in _skip_codes() or code in UNAUTOMATABLE_CODES:
            continue
        cur, tgt, st = prog(key, code)
        if st == "not_found":
            # 本账号本期未开放该面板任务（未接取/未开放）：属「完成跳过」而非失败，
            # 日志明确按跳过处理，避免冒出误导性的「不在本账号任务清单里」。
            log(
                "    [完成/跳过] %s 本期未开放（不在本账号任务清单），按跳过处理"
                % T.title_of(code)
            )
            recs.append(
                {
                    "code": code,
                    "skipped": "未开放",
                    "ok": True,
                    "before": {"current": cur, "target": tgt, "status": st},
                }
            )
            continue
        rec = {"code": code, "before": {"current": cur, "target": tgt, "status": st}}
        if cur is not None and tgt and cur >= tgt:
            rec["skipped"] = "已达标"
            rec["ok"] = True
            recs.append(rec)
            continue
        attempt = 0
        while attempt < TASK_RETRY_MAX:
            if stop and stop():
                log("    [停止] 收到停止指令，中止 %s" % T.title_of(code))
                break
            if deadline and time.time() > deadline:
                log("    [预算] 时间预算已到，中止 %s" % T.title_of(code))
                break
            attempt += 1
            if attempt > 1:
                log(
                    "    [重试] %s 第 %d/%d 次（间隔 %ds）"
                    % (T.title_of(code), attempt, TASK_RETRY_MAX, int(TASK_RETRY_GAP))
                )
                if not _sleep_gap(TASK_RETRY_GAP, stop):
                    break
            if attempt == 1:
                log("    [任务·面板] %s" % T.title_of(code))
            try:
                rec.update(_run_panel_code(key, code, cur, tgt, log))
            except Exception as e:
                rec["ok"] = False
                rec["err"] = "%s: %s" % (type(e).__name__, str(e)[:160])
            if rec.get("env_unavailable"):
                log(
                    "    [环境] %s 目标不存在（%s）——不空转重试"
                    % (code, str(rec.get("err"))[:60])
                )
                break
            c2, t2, st2 = prog(key, code)
            rec["after"] = {"current": c2, "target": t2, "status": st2}
            rec["attempts"] = attempt
            if (c2 is not None and t2 and c2 >= t2) or st2 in ("claimed", "completed"):
                rec["ok"] = True
                break
            if _is_fatal_err(rec.get("err")):
                log("    [不重试] %s 属环境类失败，重试无意义" % T.title_of(code))
                break
            if attempt < TASK_RETRY_MAX:
                log("    [未完成] %s 进度 %s/%s，准备重试" % (T.title_of(code), c2, t2))
        recs.append(rec)
    return recs


# --------------------------------------------------------------------------- #
# 专家团（严格串行）
# --------------------------------------------------------------------------- #
def _wait_template_delivery(log, maxsec=900, stop=None):
    """等当前模板完整交付：processing 连续消失且无失败文案，才算交付。"""
    t0 = time.time(); stable = 0
    while time.time() - t0 < maxsec:
        if stop and stop():
            return False, "stopped"
        try:
            st = U.js("(function(){ return deliveryState(); })()", timeout=10)
        except Exception:
            time.sleep(6); continue
        if isinstance(st, dict):
            if st.get("err"):
                return False, st["err"]
            if not st.get("processing"):
                stable += 1
                if stable >= 2:
                    return True, int(time.time() - t0)
            else:
                stable = 0
        try:
            U.js("(()=>{const rj=Array.from(document.querySelectorAll('button'))"
 ".find(e=>(e.innerText||'').trim()==='拒绝'&&e.getBoundingClientRect().top>500);"
 "if(rj)rj.click();return 0;})()", timeout=8)
        except Exception:
            pass
        time.sleep(8)
    return False, "wait_timeout"


def _sleep_until_reset(wait_str, log, deadline=None, stop=None):
    """分段睡眠到免费模型重置时间，响应 stop/deadline。"""
    import datetime as _dt
    try:
        target = _dt.datetime.strptime(str(wait_str).strip(),
            "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return "bad"
    log("  [template_5] 模型冷却中，睡到 %s 后自动重试" % wait_str)
    while time.time() < target:
        if stop and stop():
            return "stopped"
        if deadline and time.time() > deadline:
            return "deadline"
        time.sleep(min(20, max(1, target - time.time())))
    return "ok"


def run_template_five(key, log, deadline=None, stop=None):
    """专门完成 template_5：一个账号内串行发射并交付多个不同全新模板直到 target。

    - 模型频率限制：睡眠到最早重置后自动重试（模板不标记、可复用）；
    - 发射/交付后以服务端 current 是否 +1 为唯一确认，确认才永久记录；
    - 交付但未 +1 的模板标记 bad 避免死循环。
    """
    recs = []
    safety = 0
    while safety < 16:
        if stop and stop():
            recs.append({"code": "template_5", "ok": False, "err": "stopped"}); break
        if deadline and time.time() > deadline:
            recs.append({"code": "template_5", "ok": False, "err": "deadline"}); break
        j = judge_task(key, "template_5")
        cur = j["cur"] if j["cur"] is not None else 0
        tgt = j["tgt"] or 5
        if j["done"]:
            log("  [template_5] 已达标 %s/%s" % (cur, tgt))
            recs.append({"code": "template_5", "ok": True, "cur": cur}); break
        log("  [template_5] 进度 %s/%s，准备下一个全新模板" % (cur, tgt))
        all_tpl, err = U.collect_all_available_templates()
        if err or not all_tpl:
            recs.append({"code": "template_5", "ok": False, "err": "枚举失败:%s" % err}); break
        used = U._tpl_get_used(key)
        cand = [t for t in all_tpl if t.get("id") not in used]
        if not cand:
            recs.append({"code": "template_5", "ok": False, "err": "无全新模板"}); break
        tpl = cand[0]
        _home = U.reset_to_clean_home(retries=3, log=log)
        if not _home.get("ok"):
            log("  [template_5] 无法回到干净首页：%s，稍后重试" % _home.get("err"))
            time.sleep(3); safety += 1; continue
        before = cur
        r = U.fire_template_once(key, tpl)
        if not r.get("ok"):
            if r.get("step") == "rate_limit":
                _st = _sleep_until_reset(r.get("wait_until"), log, deadline, stop)
                if _st in ("stopped", "deadline"):
                    recs.append({"code": "template_5", "ok": False, "err": _st}); break
                continue  # 重置后重试，模板未标记
            log("  [template_5] 模板「%s」发射失败：%s" % (tpl.get("name"), r.get("err")))
            U._tpl_mark_used(key, tpl.get("id"))
            safety += 1
            continue
        log("  [template_5] 「%s」已发射（%s），等待完整交付..." % (tpl.get("name"), r.get("model")))
        delivered, info = _wait_template_delivery(log, stop=stop)
        log("  [template_5] 交付结果：%s（%s）" % (delivered, info))
        j2 = judge_task(key, "template_5")
        after = j2["cur"] if j2["cur"] is not None else 0
        if after > before:
            log("  [template_5] 服务端确认 +1 → %s/%s" % (after, tgt))
        else:
            log("  [template_5] 已交付但服务端未计分，标记该模板 bad")
        U._tpl_mark_used(key, tpl.get("id"))
        safety += 1
    return recs


def run_expert_team(key, do_delete, log, deadline=None, stop=None):
    code = "Expert_team_use_3"
    cur, tgt, st = prog(key, code)
    rec = {"code": code, "before": {"current": cur, "target": tgt, "status": st}}
    if cur is not None and tgt and cur >= tgt:
        rec["skipped"] = "已达标"
        rec["ok"] = True
        return rec
    need = max(0, (tgt or 3) - (cur or 0))
    if need <= 0:
        rec["skipped"] = "已达标"
        rec["ok"] = True
        return rec

    # 页面就绪闸（切号 reload 后第一个 CDP 调用最容易崩）
    try:
        _rdy, _sec = U.wait_page_ready(timeout=90.0)
        log(
            "    [就绪] 页面 JS 上下文%s（等待 %.1fs）"
            % ("可用" if _rdy else "仍不可用", _sec)
        )
    except Exception as e:
        log("    [就绪] 等待页面就绪异常：%s" % str(e)[:80])

    cfg = CFG.load_config()
    acct = CFG.find_account(cfg, key)
    acct_nick = (acct or {}).get("nickname")

    cands, info = M_TEAM.pick_candidates(key, log=log)
    if not cands:
        rec["ok"] = False
        rec["err"] = (
            "没有可试的新团队：账本已用 %s + 历史已试 %s → 该账号在自动化下无法继续，"
            "需人工接手（确认要重试请删 data/team_state.json 中该账号的 attempts）"
            % (info.get("used"), info.get("tried"))
        )
        return rec

    tried, gained_total = [], 0
    # 团队真实生命周期实测 2026-09-24 = 32m43s（account_k 财税合规专家团，6 份产物完整交付）。
    # 旧默认 600s(10min) 预算 / 480s(8min) 单次等待 全部小于真实时长 → 把正常交付的团队误杀、
    # 且账本 fail 计数被污染。新默认：单次等待 2400s(40min)、总预算 7200s(2h，约 3 个团队)。
    # ❗ 2026-09-26 修复：原实现允许外部环境变量覆盖预算/次数/等待，
    # 外部若注入旧值（600s/480s）会再次把 32m43s 的真实团队生命周期误判成超时、
    # 误杀正常交付团队并污染账本。团队生命周期参数已实测校准，固定为内置常量，不再被环境变量改写。
    TEAM_BUDGET = 7200
    MAX_TRIES = 3
    log(
        "    还需 %d 个**不同**专家团；逐个候选试（每账号最多 %d 次，预算 %.0fs），每轮核对进度增量"
        % (need, MAX_TRIES, TEAM_BUDGET)
    )
    team_t0 = time.time()
    for team_name in cands:
        if len(tried) >= MAX_TRIES:
            log(
                "    [上限] 本账号已试 %d 个团队（上限 %d），不再尝试"
                % (len(tried), MAX_TRIES)
            )
            break
        if stop and stop():
            log("    [停止] 收到停止指令，中止专家团尝试")
            break
        if U.stop_requested():
            log("    [停止] 收到停止指令，中止专家团尝试")
            break
        if time.time() - team_t0 > TEAM_BUDGET:
            log("    [预算] 专家团已达 %.0fs 预算，中止剩余候选" % TEAM_BUDGET)
            break
        c0, _, _ = prog(key, code)
        if c0 is None:
            time.sleep(4)
            c0, _, _ = prog(key, code)
            if c0 is None:
                log(
                    "    [进度查询失败] 跳过候选 [%s]（API 抖动），继续下一个"
                    % team_name
                )
                continue
        if not tgt or c0 >= tgt:
            break
        r = M_TEAM.run_team_once(
            team_name=team_name,
            idx=0,
            delete_after=False,
            max_wait=2400,
            expect_nick=acct_nick,
        )
        c1, t1, _ = prog(key, code)
        delta = 0 if c1 is None else (c1 - (c0 or 0))
        gained_total += delta
        # 让路/前置失败（sent=false）不算尝试，免得把好团队永久拉黑
        if "已让路" not in (r.get("err") or "") and r.get("sent"):
            M_TEAM.record_team_attempt(
                key,
                {
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "team": team_name,
                    "sent": bool(r.get("sent")),
                    "reason": (r.get("run") or {}).get("reason"),
                    "delta": delta,
                    "scored": bool(delta and delta > 0),
                    "err": (r.get("err") or "")[:120],
                },
            )
        nags = [
            x.get("nAg")
            for x in ((r.get("run") or {}).get("log") or [])
            if isinstance(x, dict) and x.get("nAg") is not None
        ]
        tried.append(
            {
                "team": team_name,
                "delta": delta,
                "nAg": max(nags) if nags else None,
                "sent": bool(r.get("sent")),
                "reason": (r.get("run") or {}).get("reason"),
                "created": r.get("created_title"),
                "err": (r.get("err") or "")[:160],
            }
        )
        log(
            "    团队[%s] 进度 %s → %s（%+d）nAg=%s%s"
            % (
                team_name,
                c0,
                c1,
                delta,
                tried[-1]["nAg"],
                ("  err=" + tried[-1]["err"]) if tried[-1]["err"] else "",
            )
        )
        # 交付等待：服务端只在会话完整交付后计分；原地短等一轮，不动就换下一个候选（防卡死）
        if r.get("sent") and delta == 0:
            _wait_s = int(
                os.environ.get("BR_DELIVER_WAIT", "600")
            )  # 2026-09-25: 从300秒加到600秒，确保TeamCreate事件有足够时间触发
            log(
                "    [交付等待] 进度未动，原地等团队交付（最多 %ds，到点换下一个）..."
                % _wait_s
            )
            _dl = time.time() + _wait_s
            while time.time() < _dl and not U.stop_requested():
                time.sleep(15)
                _c1b, _, _ = prog(key, code)
                if _c1b is not None and _c1b > (c0 or 0):
                    delta = _c1b - (c0 or 0)
                    gained_total += delta
                    tried[-1]["delta"] = delta
                    log("    [交付等待] 计分到账 %+d" % delta)
                    break
            c1, t1, _ = prog(key, code)
        if c1 is not None and t1 and c1 >= t1:
            break
    c1, t1, _ = prog(key, code)
    ok = bool(c1 is not None and t1 and c1 >= t1)
    # 宽限轮询：团队会话驱动退出后仍可能延迟计分（短宽限，防卡死）
    if not ok and gained_total == 0 and not U.stop_requested():
        grace = int(
            os.environ.get("BR_TEAM_GRACE", "600")
        )  # 2026-09-25: 从300秒加到600秒，宽限等待延迟计分
        log("    进度未变，宽限等待 %ds 看延迟计分..." % grace)
        g_deadline = time.time() + grace
        while time.time() < g_deadline:
            if U.stop_requested():
                break
            time.sleep(15)
            c1, t1, _ = prog(key, code)
            if c1 is not None and t1 and c1 >= t1:
                ok = True
                log("    [宽限] 延迟计分到账，进度 %s/%s" % (c1, t1))
                break
    rec["ok"] = ok
    rec["progress"] = "%s/%s" % (c1, t1)
    rec["gained"] = gained_total
    rec["teams"] = tried
    rec["max_tries"] = MAX_TRIES
    if not ok:
        if tried and gained_total == 0:
            # 连试 N 次都没积分：记录问题，立刻交还流水线（切下一账号），不卡死
            rec["problem"] = (
                "专家团连试 %d 次均未得积分（候选 %s）——已记录，跳过本账号专家团"
                % (len(tried), "、".join(t["team"] for t in tried))
            )
            rec["err"] = rec["problem"]
            log("    [问题记录] %s" % rec["problem"])
        else:
            rec["err"] = "试过 %d 个团队都未推进，详见 teams" % len(tried)
    rec["after"] = {"current": c1, "target": t1}
    return rec


# --------------------------------------------------------------------------- #
# 清理闸
# --------------------------------------------------------------------------- #
def _remaining_actionable(acc, cfg):
    from core import creds as CR

    try:
        cred, err = CR.load_cred(acc)
        if not cred:
            return [], None, err or "无凭证"
        tl, e2 = T.fetch_tasks(cred, cfg)
        if e2:
            return [], None, e2
        s = T.summarize(tl)
        rest = []
        for t in tl:
            p = t.get("progress") or {}
            if t.get("status") in ("claimed", "completed"):
                continue
            if p.get("target") and (p.get("current") or 0) >= p["target"]:
                continue
            if t.get("paid") or t.get("expired") or t.get("locked"):
                continue
            if t.get("code") in UNAUTOMATABLE_CODES or t.get("code") in _skip_codes():
                continue
            rest.append(t)
        return rest, s, ""
    except Exception as e:
        return [], None, "%s: %s" % (type(e).__name__, str(e)[:120])


def account_ready_for_cleanup(acc, cfg):
    """是否满足「可以清理会话」的前提：任务全部完成 + 无可领积分。"""
    rest, s, err = _remaining_actionable(acc, cfg)
    if err:
        return False, "无法确认任务状态（%s），不清理" % err
    if s and int(s.get("pending_credit") or 0) > 0:
        return False, "还有可领积分 %s，不清理" % s.get("pending_credit")
    if rest:
        return False, "还有 %d 项未完成（%s），不清理" % (
            len(rest),
            "、".join(str(t.get("title"))[:10] for t in rest[:4]),
        )
    return True, "成长任务已完成且无可领积分"


def cleanup_team_convs(log=None):
    """删掉当前账号里由专家团任务产生的会话（标题命中自动化指纹「执行规则」）。"""
    say = log or (lambda *a, **k: None)
    removed, failed = [], []
    FINGERPRINTS = ["执行规则"]
    try:
        titles = U.task_titles() or []
    except Exception as e:
        say("    清理跳过：读会话列表失败 %s" % str(e)[:80])
        return removed
    for t in titles:
        if not any(f in (t or "") for f in FINGERPRINTS):
            continue
        try:
            U.delete_conversation(t, force=False)
            removed.append(t[:30])
        except Exception:
            failed.append(t[:30])
    if failed:
        say("    清理失败 %d 条（受保护或当前使用中）" % len(failed))
    return removed


# --------------------------------------------------------------------------- #
# 单账号主流程
# --------------------------------------------------------------------------- #
def run_account(
    key,
    codes=None,
    do_claim=True,
    do_delete=False,
    dry=False,
    log=print,
    stop=None,
    deadline=None,
):
    """对一个账号跑完整 UI 任务流水线：切号 → 账号闸 → 串行发射会话任务 → 串行面板任务
    → 专家团发射 → 统一轮询复核 → 领取 → 清理。"""
    out = {"account": key, "records": []}
    cfg = CFG.load_config()
    acct = CFG.find_account(cfg, key)
    if not acct:
        out["err"] = "配置里没有账号 %s" % key
        return out
    out["label"] = acct.get("label")

    if not codes:
        # 动态清单：只跑该账号真实未达标的 UI 任务（2026-09-24 修复：
        # 之前硬编码全量 UI_CODES，账号已完成 15/19 也会打印全部阶段标题，误导观察）
        codes, _src = _resolve_ui_codes(key, acct, cfg, log)
        if _src == "auth_dead":
            # 凭证已被服务端吊销：不切号、不跑 UI（切号会把死 token 写进活登录文件，
            # 客户端 reload 后强制下线，会连累当前在线账号）
            out["err"] = (
                "登录态失效（401/403）：已跳过切号与 UI 任务；"
                "请在客户端重新登录该账号后抓取/续期凭证"
            )
            log("  [跳过] %s" % out["err"])
            return out
    codes = list(dict.fromkeys(codes))
    chat_codes = [c for c in codes if c in CHAT_CODES]
    panel_codes = [c for c in codes if c in PANEL_CODES]
    # Buddy_App 与 Buddy_App_QQ 同一次点击完成：去重只跑一次
    if all(c in panel_codes for c in BUDDY_PAIR):
        todo = [c for c in BUDDY_PAIR if not done(key, c)]
        panel_codes = [c for c in panel_codes if c not in BUDDY_PAIR]
        if todo:
            panel_codes.append(todo[0])
    has_team = "Expert_team_use_3" in codes

    if not dry:
        # ⚠️ 这里**绝不清停止标志**（2026-09-23 红线）：用户在 A 账号执行途中按下停止，
        # 若下一个账号开头把 STOP 清掉，停止就失效了 —— 用户会以为停了，实际还在跑。
        # 只有「用户显式发起一次新运行」的入口（run_all / 网页点击）才可以清。
        sw = AS.switch_to(key, reload=True, verify=True)
        out["switch"] = sw
        label = (sw.get("after") or {}).get("menu") or acct.get("label", "未知")
        log("  [切号·%s·%s] -> %s ok=%s" % (key, label, key, sw.get("ok")))
        if not sw.get("ok"):
            out["err"] = "切换失败"
            return out
        gate_ok, gate_err = verify_account_gate(key, log)
        out["account_gate"] = {"ok": gate_ok, "err": gate_err}
        if not gate_ok:
            out["err"] = gate_err
            return out

    # 前置「接取 + 领取」：新账号的任务多半还是 not_accepted，不接取进度根本不计
    if not dry and do_claim:
        res0 = T.run_account_tasks(acct, cfg)
        out["pre_accept"] = {
            "reason": res0.get("reason"),
            "accepted": res0.get("summary", {}).get("accepted"),
            "claimed": res0.get("summary", {}).get("claimed"),
            "credit_got": res0.get("credit_got"),
        }
    else:
        res0 = {"reason": "dry/未领取", "summary": {}, "credit_got": 0}
    log(
        "  [%s·接取] %s | 接取 %s 个 / 领取 %s 个"
        % (
            key,
            (res0 or {}).get("reason"),
            (res0 or {}).get("summary", {}).get("accepted"),
            (res0 or {}).get("summary", {}).get("claimed"),
        )
    )

    # 0) 任务清单 + 会话快照（收尾清理的基准：保留原有、删除新增）
    try:
        _m, _e = fetch_map(key)
        if not _e and _m:
            log("  [清单] 本账号任务 %d 项：" % len(_m))
            for _c, _t in _m.items():
                _p = _t.get("progress") or {}
                log(
                    "      - %s %s/%s [%s]"
                    % (
                        T.title_of(_c),
                        _p.get("current"),
                        _p.get("target"),
                        _t.get("status"),
                    )
                )
    except Exception as e:
        log("  [清单] 读取失败（不阻塞）：%s" % str(e)[:80])
    snapshot = []
    if not dry:
        try:
            # 侧栏懒加载：必须等列表稳定后再拍，否则拍不全，原有会话会被误判为"新增"
            snapshot = U.stable_task_titles() or []
        except Exception:
            snapshot = []
        log("  [快照] 账号原有会话 %d 条（收尾保留原有、删除新增）" % len(snapshot))

    # 1-3) 任务执行（发射即走 → 等待服务端计分 → 补做未计分项）
    # 核心：发射成功的任务加入 pending_credit 集合，不再每轮重复发射；
    # 每轮重新拉取服务端状态，已计分的从集合移除；等待多轮仍未计分才重试。
    SWEEPS = 100
    MAX_LAUNCH_ATTEMPTS = 3
    # pending_credit: {code: {"launched_at": ts, "attempts": n}}
    pending_credit = {}

    def _unfinished():
        rest = []
        for c in chat_codes + panel_codes:
            if c in _skip_codes() or c in UNAUTOMATABLE_CODES:
                continue
            try:
                _c, _t, _s = prog(key, c)
            except Exception:
                continue
            if _s == "not_found" or _s in ("claimed", "completed"):
                continue
            if _c is not None and _t and _c >= _t:
                continue
            rest.append(c)
        return rest

    def _sync_credit():
        """重新拉取服务端状态，从 pending_credit 移除已计分任务。返回新到账列表。"""
        newly_done = []
        for code in list(pending_credit.keys()):
            try:
                j = judge_task(key, code)
                if j["done"]:
                    newly_done.append(code)
                    del pending_credit[code]
            except Exception:
                pass
        return newly_done

    for sweep in range(1, SWEEPS + 1):
        if stop and stop():
            break
        if deadline and time.time() > deadline:
            log("  [预算] 账号时间预算耗尽，停止推进")
            break

        # 每轮开始：先同步服务端计分状态
        done_now = _sync_credit()
        if done_now:
            log("  [计分到账] %s" % "、".join(T.title_of(c) for c in done_now))

        rest = _unfinished()
        if not rest and not pending_credit:
            log("  [过号闸] 本账号非豁免任务全部通过，可以切号")
            break

        # template_5 专门处理：连续发射并交付多个不同模板直到 5/5（不进入单次发射逻辑）
        if "template_5" in chat_codes:
            _t5j = judge_task(key, "template_5")
            if not _t5j["done"]:
                _t5recs = run_template_five(key, log, deadline=deadline, stop=stop)
                out["records"].extend(_t5recs)
                _rl = next((x for x in _t5recs if x.get("rate_limited")), None)
                if _rl:
                    log("  [template_5] 免费模型频率限制，需等到 %s 重置，本轮先停" % _rl.get("wait_until"))
                    break

        if sweep > 1:
            log(
                "  [续推] 第 %d/%d 轮 | 待完成 %d 项 | 等待计分 %d 项"
                % (sweep, SWEEPS, len(rest), len(pending_credit))
            )
            if pending_credit:
                log("    等待计分中：%s" % "、".join(T.title_of(c) for c in pending_credit.keys()))

        # 本轮需要发射的会话任务：未完成 + 不在pending_credit + 重试次数未超限
        chat_to_fire = []
        for c in chat_codes:
            if c == "template_5":
                continue  # 已由 run_template_five 连续处理
            if c not in rest:
                continue
            if c in pending_credit:
                att = pending_credit[c].get("attempts", 1)
                if att >= MAX_LAUNCH_ATTEMPTS:
                    log("    [放弃] %s 已发射 %d 次仍未计分，跳过" % (T.title_of(c), att))
                    continue
                continue  # 等待计分中，不重复发射
            chat_to_fire.append(c)

        if chat_to_fire:
            log(
                "  [阶段] 会话型任务串行发射（%d个）：%s"
                % (len(chat_to_fire), "、".join(T.title_of(c) for c in chat_to_fire))
            )
            try:
                recs = run_chat_batch(
                    key, chat_to_fire, do_delete, log, deadline=deadline, stop=stop
                )
                out["records"].extend(recs)
                for r in recs:
                    c = r.get("code")
                    if not c or r.get("skipped"):
                        continue
                    if r.get("ran") or r.get("sent") or r.get("ok"):
                        if c not in pending_credit:
                            pending_credit[c] = {"launched_at": time.time(), "attempts": 1}
                            log("    [已发射待计分] %s" % T.title_of(c))
                        else:
                            pending_credit[c]["attempts"] = pending_credit[c].get("attempts", 1) + 1
                            pending_credit[c]["launched_at"] = time.time()
            except Exception as e:
                log("  [阶段异常] 会话型任务被异常中断：%s: %s" % (type(e).__name__, str(e)[:120]))
                out["records"].append({"code": "chat_phase", "ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:160])})

        # 面板型任务：串行（面板操作后立即查服务端，直接判断是否完成）
        panel_to_run = [c for c in panel_codes if c in rest and c not in pending_credit]
        if panel_to_run:
            log("  [阶段] 面板型任务（串行）：%s" % "、".join(T.title_of(c) for c in panel_to_run))
            try:
                out["records"].extend(
                    run_panel_batch(key, panel_to_run, log, deadline=deadline, stop=stop)
                )
            except Exception as e:
                log("  [阶段异常] 面板型任务被异常中断：%s: %s" % (type(e).__name__, str(e)[:120]))
                out["records"].append({"code": "panel_phase", "ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:160])})

        # 专家团：严格串行
        if has_team:
            try:
                _tc, _tt, _ = prog(key, "Expert_team_use_3")
            except Exception:
                _tc, _tt = None, None
            if _tc is not None and _tt and _tc >= _tt:
                log("  [阶段] 专家团已完成（%s/%s），跳过" % (_tc, _tt))
                out["records"].append({"code": "Expert_team_use_3", "skipped": "已达标", "ok": True})
                pending_credit.pop("Expert_team_use_3", None)
            elif "Expert_team_use_3" not in pending_credit:
                log("  [阶段] 专家团（严格串行）")
                try:
                    r_team = run_expert_team(key, do_delete, log, deadline=deadline, stop=stop)
                    out["records"].append(r_team)
                    if r_team.get("ok") or r_team.get("sent"):
                        pending_credit["Expert_team_use_3"] = {"launched_at": time.time(), "attempts": 1}
                        log("    [已发射待计分] 专家团任务")
                except Exception as e:
                    log("  [阶段异常] 专家团被异常中断：%s: %s" % (type(e).__name__, str(e)[:120]))
                    out["records"].append({"code": "Expert_team_use_3", "ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:160])})

        # 过号闸
        rest = _unfinished()
        if not rest and not pending_credit:
            log("  [过号闸] 本账号非豁免任务全部通过，可以切号")
            break

        # 有等待计分的任务：等待服务端处理
        if pending_credit:
            wait_s = 20
            log("  [等待计分] %d 个任务已发射，等待 %ds 让服务端处理..." % (len(pending_credit), wait_s))
            waited = 0
            while waited < wait_s:
                if stop and stop():
                    break
                time.sleep(2)
                waited += 2
                done_now = _sync_credit()
                if done_now:
                    log("    [计分到账] %s" % "、".join(T.title_of(c) for c in done_now))
                    rest = _unfinished()
                    if not rest and not pending_credit:
                        log("  [过号闸] 本账号非豁免任务全部通过，可以切号")
                        break
        elif rest:
            log("  [过号闸] 还有 %d 项未通过：%s —— 继续下一轮" % (len(rest), ", ".join(rest)))

    # 记下最终未完成清单：供汇总报告与审计使用，绝不因「跑过几轮」就算过
    try:
        out["unfinished"] = _unfinished()
    except Exception:
        out["unfinished"] = []

    # 3.5) 统一复核：全部任务发起完成后，逐任务核对服务端真实状态（只读，不重复执行）
    try:
        out["reconcile"] = reconcile_fire_state(
            key, chat_codes, panel_codes, log, stop=stop
        )
    except Exception as e:
        log("  [统一复核] 复核本身异常（不阻塞）：%s" % str(e)[:80])

    # 4) 领取
    if do_claim and not dry:
        log("  [领取] ...")
        res = T.run_account_tasks(acct, cfg)
        out["claim"] = {
            "status": res.get("status"),
            "reason": res.get("reason"),
            "credit_got": res.get("credit_got"),
            "claimed": res.get("summary", {}).get("claimed"),
            "claimed_titles": [
                st.get("title")
                for st in (res.get("steps") or [])
                if st.get("step") == "claim" and "领取成功" in str(st.get("reason"))
            ],
        }
        log("    %s | 本次领取 %s 积分" % (res.get("reason"), res.get("credit_got")))

    # 4.5) 完成审计：除阶段级任务/skip 外，启动过却未完成的任务 = 程序 bug → 日志 + 告警
    if not dry:
        try:
            _stopped = bool((stop and stop()) or U.stop_requested())
            out["bugs"] = audit_account_completion(
                key, acct, cfg, out["records"], log, stopped=_stopped
            )
            if not out["bugs"]:
                log("  [审计] 通过：无「启动未完成」任务")
        except Exception as e:
            log("  [审计] 审计本身异常（不阻塞）：%s" % str(e)[:80])
            out["bugs"] = []

    # 6) 最终清理：确认所有可自动化任务完成、积分全部到账后，才删除新增会话/自动化任务
    if not dry:
        can_clean, clean_reason = account_ready_for_cleanup(acct, cfg)
        if can_clean and do_delete:
            log("  [清理闸] %s，开始清理自动化产物" % clean_reason)
            try:
                removed, failed = _cleanup_new_convs(snapshot, log)
                log(
                    "  [清理] 删除本次新增会话 %d 条%s（原有 %d 条已保留）"
                    % (
                        removed,
                        ("，失败/跳过 %d 条" % failed) if failed else "",
                        len(snapshot),
                    )
                )
                out["cleanup_convs"] = removed
                # 删除本次创建的自动化任务（积分已到账）
                auto_name = "每日喝水提醒"
                try:
                    auto_del = M_PANEL.do_automation_delete(auto_name)
                    if auto_del.get("ok"):
                        log("  [清理] 已删除临时自动化任务「%s」" % auto_name)
                        out["cleanup_auto"] = True
                except Exception as e:
                    log("  [清理] 删除自动化任务异常（不阻塞）：%s" % str(e)[:80])
            except Exception as e:
                log("  [清理] 清理异常（不阻塞）：%s" % str(e)[:80])
        else:
            log("  [清理闸] %s，暂不清理（积分/任务未确认完成）" % clean_reason)

    m, err = fetch_map(key)
    if not err:
        out["final"] = {
            "done": sum(1 for t in m.values() if t.get("done")),
            "total": len(m),
        }
    return out


def reconcile_fire_state(key, chat_codes, panel_codes, log, stop=None):
    """统一复核（调度规范第 3/5 步）：全部任务发起完成后，逐任务回读服务端真实状态，
    确认每个任务「真正发起成功」，并标注「积分/次数是否到账」。

    只读不写：仅回读服务端任务进度，不重复发起、不重复执行，避免双计 / 重复会话。
    这一步把「全部发起后再次校验发起状态」「核对到账未到账则重试」显式化为一道可见闸。
    """
    codes = [
        c
        for c in (chat_codes + panel_codes)
        if c not in _skip_codes() and c not in UNAUTOMATABLE_CODES
    ]
    if not codes:
        return {}
    m, err = fetch_map(key)
    if err:
        log("  [统一复核] 任务状态读取失败（%s），跳过复核" % err[:60])
        return {}
    log("  [统一复核] 全部任务已发起，逐任务核对服务端真实状态：")
    report = {}
    for c in codes:
        if stop and stop():
            break
        t = m.get(c)
        if not t:
            # 服务端根本没有这个任务（活动下架 / 本账号未开放）：属环境未开放，非程序失败
            report[c] = {"status": "not_found", "truly_initiated": False}
            log(
                "    - %s：服务端无此任务（not_found，环境未开放，非程序失败）"
                % T.title_of(c)
            )
            continue
        p = t.get("progress") or {}
        cur, tgt, st = p.get("current"), p.get("target"), t.get("status")
        truly = tgt is not None  # 有 target = 服务端已接取/已建任务条目 = 真正发起
        credited = bool(
            st in ("claimed", "completed") or (cur is not None and tgt and cur >= tgt)
        )
        report[c] = {
            "current": cur,
            "target": tgt,
            "status": st,
            "truly_initiated": truly,
            "credited": credited,
        }
        flag = "✔到账" if credited else ("○未到账" if truly else "✗未发起")
        log("    - %s：%s/%s [%s] %s" % (T.title_of(c), cur, tgt, st, flag))
    return report


def run_ui_code_once(key, code, do_delete=True, log=None, deadline=None, stop=None):
    """对单个账号执行一个 UI code（供 web 单任务接口调用）。切号 + 账号闸 + 跑一个 code。"""
    say = log or (lambda *a, **k: None)
    cfg = CFG.load_config()
    acct = CFG.find_account(cfg, key)
    if not acct:
        return {"code": code, "ok": False, "error": "配置里没有账号 %s" % key}

    # 必须 reload=True：账号闸 verify_account_gate 认的是左下角 UI 昵称，
    # 不整页重载昵称可能不重绘 → 闸直接拒绝执行（表现为「切过去了啥也没做」）。
    # 代价是整页重载会销毁 CDP 上下文并干掉会话内驱动 → 因此本函数只能由
    # **会话之外的常驻服务进程**调用（web/server.py 的后台线程）。
    sw = AS.switch_to(key, reload=True, verify=True)
    if not sw.get("ok"):
        return {"code": code, "ok": False, "error": "切号失败：%s" % sw, "switch": sw}
    say("切号 -> %s ok=%s" % (key, sw.get("ok")))
    # 切号会整页 reload，CDP 执行上下文必销毁。不等页面就绪就动手，
    # 第一步就会掉进 js() 的静默重连黑洞（表现为「切过去了但什么都没做」）。
    try:
        _rdy, _sec = U.wait_page_ready(timeout=90.0)
        say("页面就绪=%s（等待 %.1fs）" % (_rdy, _sec))
    except Exception as e:
        say("等待页面就绪异常：%s" % str(e)[:80])

    gate_ok, gate_err = verify_account_gate(key, say)
    if not gate_ok:
        return {"code": code, "ok": False, "error": gate_err, "switch": sw}

    cur, tgt, st = prog(key, code)
    if cur is not None and tgt and cur >= tgt:
        return {
            "code": code,
            "ok": True,
            "skipped": "已达标",
            "before": {"current": cur, "target": tgt, "status": st},
        }

    if code not in UI_CODES:
        return {
            "code": code,
            "ok": False,
            "error": "%s 不在可自动执行的任务列表里" % T.title_of(code),
        }
    if stop and stop():
        return {"code": code, "ok": False, "skipped": "收到停止指令"}

    rec = {"code": code, "before": {"current": cur, "target": tgt, "status": st}}
    try:
        if code in CHAT_CODES:
            recs = run_chat_batch(
                key, [code], do_delete, say, deadline=deadline, stop=stop
            )
            if recs:
                rec.update(recs[0])
        elif code in PANEL_CODES:
            rec.update(_run_panel_code(key, code, cur, tgt, say))
        elif code == "Expert_team_use_3":
            rec.update(
                run_expert_team(key, do_delete, say, deadline=deadline, stop=stop)
            )
    except Exception as e:
        rec["ok"] = False
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    c2, t2, st2 = prog(key, code)
    rec["after"] = {"current": c2, "target": t2, "status": st2}
    return rec
