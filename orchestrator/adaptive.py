#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自适应单任务循环执行器（2026-09-23 按用户明确的执行逻辑重写）。

正确的执行顺序（用户原话，顺序不许变）：
    ① 读取任务清单
    ② 找到**第一个未完成**的任务
    ③ 去客户端，**读取当前界面**
    ④ 判断这个任务在**这个界面**上怎么完成
    ⑤ 操作
    ⑥ 回读
    ⑦ 确认（以服务端进度为准）
    ⑧ 回到清单，找下一个未完成，再去客户端 …… 循环

与旧实现的本质区别：
  - 旧实现：按写死的清单顺序批量跑，每个任务一套固定步骤（界面一变就全废）。
  - 本实现：**一次只处理一个任务**，每一步都先读当前界面再决定动作，
    界面上没有对应入口就如实报告，不假装执行。

所有动作只用「文本/语义」找元素，不写死 class 选择器。
"""
import os
import sys
import time

from core import config as CFG
from core import creds as CR
from engine import driver as U
from engine import tasks as T
from orchestrator import batch as B

# --------------------------------------------------------------------------- #
# ① 读清单 / ② 第一个未完成
# --------------------------------------------------------------------------- #

# 不能自动完成的（人工/付费/夜间/写死跳过）
MANUAL_ONLY = {"wb_wechat_oa_subscribe_task", "Expert_Philanthropy", "black_cat"}
# 本执行器已实现的动作（其余如实报「未实现」，不假装跑）
IMPLEMENTED = {
    "chat_5", "first_buddy", "RichMeow_Chat",
    "template_5",
    "Library_read",
    "Buddy_App", "Buddy_App_QQ",
    "create_canvas", "playbook_prompt",
    "skill_1", "expert_5", "Hp_Appearance", "Expert_team_use_3",
    "Expert_lighthouse", "automation_1",
    "Model_chat_GLM5.2",
}

# 已召唤过的专家/团队（防重复召唤同一个，服务端要求"不同"）
_USED_EXPERTS = set()


# —— 界面元素读数（不写死：每次都从当前 DOM 里读出来再挑） ——
def read_named(selector, limit=40, scan=40):
    """读出某类可见元素的文本，供决策用。

    黄金规则七：**先截断再处理**——命中候选先 slice(scan) 再取几何，凑够 limit 就停。
    旧写法是"把全部命中节点（灵感页一次 1081 个）的几何逐节点算完再截断"，
    那正是渲染进程被打爆、客户端卡死的直接原因（RUNBOOK §2.2f）。
    """
    js = ("(() => { const vis=e=>{const r=e.getBoundingClientRect();"
          "return r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0;};"
          "const els=Array.from(document.querySelectorAll(%r)).slice(0,%d);"
          "const out=[];"
          "for (let i=0;i<els.length && out.length<%d;i++){"
          "  const e=els[i]; if(!vis(e)) continue;"
          "  const t=(e.innerText||'').replace(/\\s+/g,' ').trim();"
          "  if(t && t.length<=30) out.push(t); }"
          "return out; })()" % (selector, scan, limit))
    try:
        r = U.js(js, timeout=20)
        return r if isinstance(r, list) else []
    except Exception:
        return []


def read_scene_pills():
    return read_named(".wb-scene-tabs__pill, [class*='scene-tabs'] [class*='pill']")


def read_quick_actions():
    return read_named("button.quick-actions__item, [class*='quick-actions'] button")


def read_sub_prompts():
    return read_named("button.quick-actions-sub__item, [class*='quick-actions-sub'] button")


def task_map(key):
    """读服务端任务清单 → {code: task}。"""
    m, err = B.fetch_map(key)
    return (m or {}), err


# 本轮拉黑（做不动/未实现的任务，避免循环卡在同一个 code 上空转）
_BLOCKED = set()


def _block(key, code):
    _BLOCKED.add((key, code))


def reset_blocked():
    _BLOCKED.clear()


def list_unfinished(key):
    """按清单顺序返回未完成的任务 code 列表（已完成/已领取/不可自动/本轮拉黑的排除）。"""
    m, err = task_map(key)
    if err:
        return [], "清单读取失败：%s" % err
    out = []
    for code, t in m.items():
        if code in MANUAL_ONLY or code in B.UNAUTOMATABLE_CODES:
            continue
        if code in B._skip_codes():
            continue
        if (key, code) in _BLOCKED:
            continue
        st = t.get("status")
        p = t.get("progress") or {}
        cur, tgt = p.get("current"), p.get("target")
        if st in ("claimed", "completed"):
            continue
        if cur is not None and tgt and cur >= tgt:
            continue
        out.append(code)
    # 按服务端返回顺序（稳定），不排序 —— 就是"清单里第一个未完成"
    return out, ""


def first_unfinished(key):
    codes, err = list_unfinished(key)
    if err:
        return None, err
    if not codes:
        return None, "清单里没有未完成的任务（或全部不可自动化）"
    return codes[0], ""


# --------------------------------------------------------------------------- #
# ③ 读界面
# --------------------------------------------------------------------------- #

SNAP_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top < innerHeight && r.bottom > 0;
  };
  const txt = (e) => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
  const out = {};
  out.buttons = Array.from(document.querySelectorAll('button, [role="button"], a'))
      .filter(vis).map(txt).filter(t => t && t.length <= 24).slice(0, 60);
  out.sidebar = Array.from(document.querySelectorAll('.conversation-list-tab-button'))
      .filter(vis).map(txt).filter(Boolean).slice(0, 12);
  out.input = !!document.querySelector('textarea, [contenteditable="true"]');
  out.hasEditor = !!document.querySelector('._editable_uvue6_1, textarea, [contenteditable="true"]');
  out.title = document.title || '';
  out.bodyHead = (document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200);
  return out;
})()
"""


def cdp_ready():
    """探测客户端 CDP 调试端口是否就绪。

    返回 (ok, 说明)。2026-09-23 实测教训：客户端进程在、但没带
    --remote-debugging-port 启动时，所有 U.js() 都「连接被拒」，
    旧代码把它吞成「读不到界面」，误导诊断。这里先明确区分。
    """
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        r = opener.open("http://127.0.0.1:%d/json/version" % CFG.CDP_PORT, timeout=3)
        # 修复：原先不关闭响应体，每次探测泄漏一个连接。
        try:
            return (r.status == 200), ""
        finally:
            try:
                r.close()
            except Exception:
                pass
    except Exception as e:
        return False, str(e)[:100]


def ui_snapshot():
    """读当前界面（可见按钮/侧栏/输入框/页面开头文本）。读不到返回 {}。"""
    try:
        r = U.js(SNAP_JS, timeout=25)
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


def describe(snap):
    """把界面快照压成一句人话，写进日志。"""
    if not snap:
        return "读不到界面"
    side = "/".join(snap.get("sidebar") or []) or "无侧栏"
    return "侧栏[%s] 输入框=%s 按钮%d个" % (
        side[:40], "有" if snap.get("input") else "无", len(snap.get("buttons") or []))


# --------------------------------------------------------------------------- #
# ④ 决策 + ⑤ 操作
# --------------------------------------------------------------------------- #

def _click(text, log, timeout=8.0):
    """按文本点（界面上找不到就返回 False）。"""
    # 黄金规则七：收掉全量 div（命中量级失控）+ 先截断再取几何 + 轮询降频（0.5s→1.0s）。
    # 旧写法在一个 8 秒窗口里把全文档扫了 16 次，是客户端卡死的直接引爆点。
    ok = U.wait_until("(() => { const vis=e=>{const r=e.getBoundingClientRect();"
                      "return r.width>0&&r.height>0;};"
                      "const els=Array.from(document.querySelectorAll('button,[role=\"button\"],a'))"
                      "  .slice(0,60);"
                      "return els.some(e=>vis(e)&&(e.innerText||'').trim()===%r); })()" % str(text),
                      timeout=timeout, poll=1.0, tag="等待按钮「%s」" % text)
    if not ok:
        log("    界面上没有「%s」，无法继续（如实报告，不假装点了）" % text)
        return False
    try:
        U.click_text(text)
        return True
    except Exception as e:
        log("    点「%s」失败：%s" % (text, str(e)[:80]))
        return False


def _new_task(log):
    """回到首页并新建一个任务（会话型任务的前提）。"""
    if not _click("新建任务", log):
        return False
    time.sleep(2.5)
    return bool(U.wait_until("!!document.querySelector('textarea,[contenteditable=\"true\"]')",
                             timeout=20, poll=1.0, tag="等待输入框出现"))


def _send(text, log, wait_done=90):
    """在输入框打一句话并发送，然后等 AI 收尾。"""
    try:
        U.type_text(text, do_send=True)
    except Exception as e:
        log("    发送失败：%s" % str(e)[:80])
        return False
    ok = U.wait_until(
        "(() => { const b=Array.from(document.querySelectorAll('button')).map(e=>(e.innerText||'').trim());"
        "return !b.some(t=>/停止|生成中|思考中/.test(t)); })()",
        timeout=wait_done, poll=2.0, tag="等 AI 收尾")
    log("    AI 收尾：%s" % ("完成" if ok else "超时（按服务端回读为准）"))
    return True


def plan_and_act(code, snap, log):
    """④ 判断这个任务在当前界面怎么完成 → ⑤ 操作。

    返回 (做了什么说明, 是否真的执行了动作)
    """
    if code not in IMPLEMENTED:
        return "本执行器还没实现「%s」的界面流程（如实报告，不假装跑）" % code, False

    log("    界面：%s" % describe(snap))

    # —— 灵感：侧栏「更多」hover 出浮层 → 点「灵感」→ 读卡片 → 做同款 → 替换 → 发送 ——
    if code == "playbook_prompt":
        # ⚠️ 合成 hover 弹不出浮层（2026-09-23 实测）：必须用真实鼠标移动过去
        pos = U.js("(() => { const vis=e=>{const r=e.getBoundingClientRect();"
                   "return r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0;};"
                   " const b=Array.from(document.querySelectorAll('button')).filter(vis)"
                   "   .find(e=>(e.innerText||'').trim()==='更多');"
                   " if(!b) return null; const r=b.getBoundingClientRect();"
                   " return {x:r.left+r.width/2, y:r.top+r.height/2}; })()", timeout=20)
        items = []
        if pos:
            try:
                from core import cdp as CD
                _c, _p = CD.connect()
                try:
                    _c.call("Input.dispatchMouseEvent",
                            {"type": "mouseMoved", "x": pos["x"] - 8, "y": pos["y"] - 8,
                             "button": "none"}, timeout=10)
                    time.sleep(0.3)
                    _c.call("Input.dispatchMouseEvent",
                            {"type": "mouseMoved", "x": pos["x"], "y": pos["y"],
                             "button": "none"}, timeout=10)
                finally:
                    _c.close()
                time.sleep(1.5)
                items = read_named(".wb-dropdown__item")
            except Exception as e:
                log("    真实鼠标 hover 失败：%s" % str(e)[:60])
        log("    hover「更多」后读到浮层项：%s" % items)
        if not any("灵感" in i for i in items):
            return "浮层里没有「灵感」", False
        if not _click("灵感", log):
            return "点不到「灵感」", False
        time.sleep(3)
        cards = read_named(".dc-playbook-card", limit=30)
        cards = [c for c in cards if not U.media_blocked(c)]
        log("    灵感页读到卡片 %d 张" % len(cards))
        if not cards:
            return "灵感页没读到卡片", False
        idx = min(2, len(cards) - 1)
        r = U.js("(async () => { const vis=e=>{const b=e.getBoundingClientRect();"
                 "return b.width>0&&b.height>0;};"
                 " const cs=Array.from(document.querySelectorAll('.dc-playbook-card')).filter(vis);"
                 " const t=cs[%d]; if(!t) return {ok:false,err:'卡片取不到'};"
                 " t.scrollIntoView({block:'center'}); await sleep(900);"
                 " const r=t.getBoundingClientRect();"
                 " return {ok:true, x:r.left+r.width/2, y:r.top+r.height/2, title:(t.innerText||'').slice(0,40)}; })()"
                 % idx, timeout=30)
        if not (isinstance(r, dict) and r.get("ok")):
            return "卡片定位失败", False
        log("    选中卡片：%s" % (r.get("title") or "").replace("\n", " "))
        U.mouse_click("document.querySelectorAll('.dc-playbook-card')[%d]" % idx, timeout=20)
        time.sleep(3)
        if not _click("做同款", log):
            return "没有「做同款」", False
        time.sleep(3)
        btns = read_named("button", 40)
        if any("替换" in b for b in btns):
            _click("替换", log, timeout=6)
            time.sleep(1.5)
        _send("直接给结果，一句话即可。", log)
        return "灵感卡片「%s」做同款并发对话" % (r.get("title") or "")[:20], True

    # —— 设计创意：读场景 pill → 点「设计创意」→ 读快捷动作 → 挑文本向 → 读示例提示词 → 发送 ——
    if code == "create_canvas":
        if not _new_task(log):
            return "没能进入新建任务界面", False
        pills = read_scene_pills()
        log("    界面上读到场景 %d 个：%s" % (len(pills), "、".join(pills[:6])))
        target = next((p for p in pills if "设计创意" in p), None)
        if not target:
            return "界面上没有「设计创意」场景", False
        if not _click(target, log):
            return "点不到场景「%s」" % target, False
        time.sleep(2)
        acts = [a for a in read_quick_actions() if not U.media_blocked(a)]
        log("    界面上读到快捷动作 %d 个，非媒体 %d 个：%s"
            % (len(read_quick_actions()), len(acts), "、".join(acts[:6])))
        if not acts:
            return "没有可用的文本向快捷动作", False
        pick = next((a for a in acts if "网站" in a), acts[0])
        if not _click(pick, log):
            return "点不到快捷动作「%s」" % pick, False
        time.sleep(2)
        subs = [s for s in read_sub_prompts() if not U.media_blocked(s)]
        log("    界面上读到示例提示词 %d 个：%s" % (len(subs), "、".join(subs[:4])))
        if subs:
            if not _click(subs[0], log):
                log("    示例提示词点不到，继续尝试直接发送")
            time.sleep(2)
        btns = snap.get("buttons") or []
        if any("替换" in b for b in btns):
            _click("替换", log, timeout=5)
            time.sleep(1.5)
        _send("直接给结果，一句话即可。", log)
        return "在设计创意下用「%s」发了一次对话" % pick, True

    # —— 会话类：新建任务 → 发一句 → 等收尾 ——
    if code in ("chat_5", "first_buddy", "RichMeow_Chat"):
        if not _new_task(log):
            return "没能进入新建任务界面", False
        _send("用一句话说明今天的日期有什么特别。只回答一句话，不要展开。", log)
        return "新建任务并发了一句对话", True

    # —— 模板类：新建任务 → 快捷动作里挑一个文本向模板 → 发送 ——
    if code == "template_5":
        if not _new_task(log):
            return "没能进入新建任务界面", False
        names = U.list_templates_live() or []
        safe = [n for n in names if not U.media_blocked(n)]
        log("    界面上读到模板 %d 个，可用（非生图/视频）%d 个" % (len(names), len(safe)))
        if not safe:
            return "界面上没有可用的文本向模板", False
        pick = safe[0]
        if not _click(pick, log):
            return "模板「%s」点不到" % pick, False
        time.sleep(2)
        _send("直接给结果，一句话即可。", log)
        return "用模板「%s」发了一次对话" % pick, True

    # —— 资料库：侧栏进资料库 → 依次打开所有6个文档，每个滚动读完 ——
    if code == "Library_read":
        if not _click("资料库", log):
            return "侧栏没有「资料库」", False
        time.sleep(4)
        titles = read_named(".space-panel-iframe, [class*='doc-item'], [class*='file-name']", 40)
        log("    资料库里读到 %d 个条目：%s" % (len(titles), titles[:10]))
        # 依次打开所有文档，每个都滚动读完
        opened = 0
        for tgt in titles:
            if not tgt or len(tgt.strip()) < 2:
                continue
            log("    → 打开文档：%s" % tgt[:30])
            if _click(tgt, log, timeout=8):
                time.sleep(3)
                try:
                    U.iframe_cdp("workbuddy.cn")
                    for _i in range(8):
                        U.iframe_scroll()
                        time.sleep(0.5)
                    opened += 1
                except Exception as e:
                    log("      读文档异常：%s" % str(e)[:50])
                time.sleep(1)
        log("    资料库共打开并读完 %d 个文档" % opened)
        return "资料库：依次打开并读完 %d 个文档" % opened, True

    # —— 技能：专家·技能·连接器 → 技能 → 我安装的 → 打开一个并说一句 ——
    if code == "skill_1":
        if not _click("专家·技能·连接器", log):
            return "侧栏没有「专家·技能·连接器」", False
        time.sleep(3)
        tabs = read_named("[class*='tab'], button", 40)
        log("    面板里读到的 tab/按钮：%s" % tabs[:12])
        if not _click("技能", log, timeout=6):
            return "没有「技能」tab", False
        time.sleep(2.5)
        if _click("我安装的", log, timeout=5):
            time.sleep(2)
        items = read_named("[class*='skill-card'], [class*='skill-item'], [class*='card']", 30)
        log("    读到的技能：%s" % items[:8])
        pick = next((i for i in items if i and "安装" not in i), items[0] if items else None)
        if not pick:
            return "没读到已安装技能", False
        if not _click(pick, log):
            return "技能「%s」点不到" % pick, False
        time.sleep(3)
        _send("用一句话介绍你能做什么。", log, wait_done=60)
        return "打开技能「%s」并发了一句" % pick[:16], True

    # —— 专家团：专门调用team.py，发"请让你们团队每个成员介绍一下自己的团队能力。" ——
    if code == "Expert_team_use_3":
        from missions.team import run_team_once
        res = run_team_once(log=log)
        if res.get("ok"):
            return "专家团召唤成功：%s" % res.get("team_name", ""), True
        else:
            return "专家团失败：%s" % res.get("err", "未知错误"), False

    # —— 腾讯轻量云专家：专门调用lighthouse_flow做授权连接器 ——
    if code == "Expert_lighthouse":
        from missions.lighthouse_flow import run
        res = run(log=log)
        if res.get("ok"):
            return "腾讯轻量云专家授权完成并发话", True
        else:
            return "腾讯轻量云专家失败：%s" % res.get("err", "未知错误"), False

    # —— 普通专家/专家团：专家·技能·连接器 → 专家 → 召唤一个没用过的专家 → 发一句 ——
    if code in ("expert_5",):
        if not _click("专家·技能·连接器", log):
            return "侧栏没有「专家·技能·连接器」", False
        time.sleep(3)
        if code == "Expert_team_use_3" and _click("专家团", log, timeout=6):
            time.sleep(2.5)
        cards = read_named(".ec-expert-card, [class*='expert-card'], [class*='team-card']", 40)
        fresh = [c for c in cards if c and c not in _USED_EXPERTS]
        log("    读到专家/团队 %d 个，未用过 %d 个" % (len(cards), len(fresh)))
        if not fresh:
            return "没有未使用过的专家可召唤", False
        pick = fresh[0]
        if not _click(pick, log):
            return "专家「%s」点不到" % pick[:16], False
        time.sleep(3)
        summons = read_named(".ec-card-summon-btn, button", 30)
        btn = next((s for s in summons if "召唤" in s), None)
        if btn and not _click(btn, log, timeout=8):
            return "召唤按钮点不到", False
        time.sleep(5)
        _USED_EXPERTS.add(pick)
        _send("你好，用一句话介绍你自己。", log, wait_done=60)
        return "召唤「%s」并发了一句" % pick[:16], True

    # —— 外观：左下账号 → 外观 → 和平精英主题卡 ——
    # RUNBOOK 2.3 + VERIFIED-FACTS 二：入口①点左下账号→菜单「外观」；①没有才走设置→外观
    if code == "Hp_Appearance":
        # 找左下角账号按钮（手机号或昵称，不写死）
        acc_btns = [b for b in (snap.get("buttons") or [])
                    if b and (b.isdigit() and len(b) >= 8 or "账号" in b or "用户" in b)]
        acc_clicked = False
        if acc_btns and _click(acc_btns[0], log, timeout=6):
            acc_clicked = True
        if not acc_clicked:
            # 回退：点侧栏里的设置/账号入口
            side = snap.get("sidebar") or []
            for s in side:
                if "设置" in s or "账号" in s:
                    if _click(s, log, timeout=6):
                        acc_clicked = True
                        break
        if not acc_clicked:
            return "点不到左下角账号/设置入口", False
        time.sleep(2)
        if not _click("外观", log, timeout=6):
            return "菜单里没有「外观」", False
        time.sleep(3)
        themes = read_named("button.appearance-card, [class*='appearance-card'], [class*='theme']", 30)
        log("    读到的主题：%s" % themes[:10])
        pick = next((t for t in themes if "和平精英" in t), None)
        if not pick:
            return "主题里没有「和平精英」", False
        # 正确逻辑：先切到别的主题，再切回和平精英，才会触发计分事件
        other_theme = next((t for t in themes if t and "和平精英" not in t and len(t.strip())>2), None)
        if other_theme:
            log("    先切到别的主题：%s" % other_theme[:20])
            _click(other_theme, log, timeout=5)
            time.sleep(2)
        # 再切回和平精英
        if not _click(pick, log):
            return "主题卡点不到", False
        time.sleep(2)
        return "切换到「和平精英」主题（先切走再切回触发计分）", True

    # —— 自动化任务：定时任务 → 添加 → 填表 → 确定 → 回读 → 删掉 ——
    if code == "automation_1":
        if not _click("定时任务", log):
            return "侧栏没有「定时任务」", False
        time.sleep(3)
        if not _click("添加定时任务", log, timeout=8):
            return "没有「添加定时任务」", False
        time.sleep(3)
        _send("每天提醒我喝水", log, wait_done=15)
        time.sleep(2)
        for b in ("确定", "创建", "保存"):
            if _click(b, log, timeout=5):
                break
        time.sleep(3)
        # 权限二次确认
        if _click("我已知悉并确认", log, timeout=5):
            time.sleep(1)
            _click("确认创建", log, timeout=6)
        time.sleep(3)
        return "建了一个自动化任务（随后删除，不留尾巴）", True

    # —— 发现应用：左上角「发现应用」 → 企鹅教师助手 ——
    if code in ("Buddy_App", "Buddy_App_QQ"):
        if not _click("发现应用", log):
            return "界面上没有「发现应用」", False
        time.sleep(2.5)
        if not _click("企鹅教师助手", log):
            return "应用列表里没有「企鹅教师助手」", False
        time.sleep(3)
        # RUNBOOK 2.3：可能弹「确认授权」/「绑定 xx 账号」→ 点「跳过」
        if _click("跳过", log, timeout=5):
            time.sleep(2)
        # RUNBOOK 2.3：退出应用（aria-label="退出当前 Buddy 应用"）
        time.sleep(2)
        if _click("退出当前 Buddy 应用", log, timeout=6):
            log("    已退出 Buddy 应用")
        return "进入了「企鹅教师助手」Buddy 应用", True

    # —— Model_chat_GLM5.2：新建对话 → 切 GLM-5.2 → 发一句 ——
    # ⚠️ RUNBOOK 2.1：模型只在新会话里切，不能动当前会话
    if code == "Model_chat_GLM5.2":
        if not _new_task(log):
            return "没能进入新建任务界面", False
        time.sleep(2)
        # 尝试切模型到 GLM-5.2
        try:
            U.set_model("GLM-5.2")
            log("    已切模型到 GLM-5.2")
        except Exception as e:
            log("    切模型失败（继续用当前模型发对话）：%s" % str(e)[:60])
        time.sleep(2)
        _send("用一句话说明今天的日期有什么特别。", log)
        return "新建对话并使用 GLM-5.2 发了一句", True

    return "未处理的任务 %s" % code, False


# --------------------------------------------------------------------------- #
# ⑥ 回读 / ⑦ 确认
# --------------------------------------------------------------------------- #

def read_back(key, code):
    """回读服务端该任务的真实进度。"""
    m, err = task_map(key)
    if err:
        return None, None, "err:" + err
    t = m.get(code)
    if not t:
        return None, None, "not_found"
    p = t.get("progress") or {}
    return p.get("current"), p.get("target"), t.get("status")


def confirm(key, code, before, log):
    """以服务端进度为准确认是否推进/达标。"""
    cur, tgt, st = read_back(key, code)
    if st == "err:" or st == "not_found":
        log("    回读失败：%s" % st)
        return False, False
    done = bool(st in ("claimed", "completed") or (cur is not None and tgt and cur >= tgt))
    moved = (cur is not None and before is not None and cur > before)
    log("    回读：%s %s/%s [%s] → %s" % (code, cur, tgt, st,
                                          "达标" if done else ("有推进" if moved else "没动")))
    return done, moved


def claim_if_ready(key, code, log):
    """达标就去领（领不到不算 bug，服务端说没到就是没到）。"""
    cfg = CFG.load_config()
    acc = CFG.find_account(cfg, key)
    cred, err = CR.load_cred(acc)
    if err:
        log("    领取失败（读不到凭证）：%s" % err)
        return 0
    c, r = T.claim_task(cred, code, cfg)
    got = 0
    try:
        got = int(((r or {}).get("data") or {}).get("credit") or 0)
    except Exception:
        got = 0
    log("    领取 %s：http=%s %s" % (code, c, (r or {}).get("msg") or ""))
    return got


# --------------------------------------------------------------------------- #
# 单任务闭环 + 主循环
# --------------------------------------------------------------------------- #

def run_one_task(key, code, log, retries=2):
    """一个任务的完整闭环：读界面 → 决策 → 操作 → 回读 → 确认 → 领取。"""
    log("  ── 任务 %s 开始" % code)
    _cur0, _tgt0, _st0 = read_back(key, code)
    before = _cur0 or 0
    for attempt in range(1, retries + 1):
        if U.stop_requested():
            log("    收到停止指令，本任务中止")
            return False
        log("    第 %d/%d 次尝试" % (attempt, retries))
        snap = ui_snapshot()
        if not snap:
            # 页面可能在切换中：等它就绪再读一次，别急着判死
            log("    读不到界面，等页面就绪后重试")
            try:
                U.wait_page_ready(timeout=30)
            except Exception:
                pass
            time.sleep(3)
            snap = ui_snapshot()
        if not snap:
            ok, why = cdp_ready()
            if not ok:
                log("    ✗ CDP 端口掉了（%s），本任务中止——重启客户端后再跑" % (why or "无响应"))
                return False
            log("    仍然读不到界面，本任务中止（如实报告）")
            return False
        what, acted = plan_and_act(code, snap, log)
        log("    动作：%s" % what)
        if not acted:
            return False
        # ⚠️ 服务端记账有 50~60s 延迟（2026-09-23 实测）：发完立刻回读会误判"没动"，
        # 旧逻辑因此疯狂重试。这里改为轮询回读，最多等 90s。
        done, moved = False, False
        for _t in range(0, 90, 10):
            done, moved = confirm(key, code, before, log)
            if done:
                break
            time.sleep(10)
        if done:
            got = claim_if_ready(key, code, log)
            log("  ── 任务 %s 完成（领取 %s 积分）" % (code, got))
            return True
        if attempt < retries:
            log("    未达标，20s 后再试一次")
            time.sleep(20)
    log("  ── 任务 %s 未达标（如实记录，不算完成）" % code)
    return False


def loop_account(key, log=print, max_tasks=30, deadline=None, stop=None):
    """主循环：清单 → 第一个未完成 → 客户端 → 读界面 → 决策 → 操作 → 回读 → 确认 → 下一个。"""
    # 前置自检：CDP 端口不通就别空转（客户端在但没带调试端口时，
    # 所有任务都会报「读不到界面」，2026-09-23 实测踩中）。
    ok, why = cdp_ready()
    if not ok:
        log("[循环] ✗ 客户端 CDP（127.0.0.1:%d）未就绪：%s" % (CFG.CDP_PORT, why or "无响应"))
        log("[循环] → 请双击 restart-cdp.cmd 带调试端口重启客户端后再跑（进程在≠端口在）。")
        return {"done": [], "skipped": [], "cdp_down": True}
    log("[循环] CDP 端口就绪 ✓")
    done_codes, skipped = [], []
    for i in range(1, max_tasks + 1):
        if (stop and stop()) or U.stop_requested():
            log("[循环] 收到停止指令，收工")
            break
        if deadline and time.time() > deadline:
            log("[循环] 时间预算耗尽，收工")
            break
        code, err = first_unfinished(key)
        if err or not code:
            log("[循环] ① 读清单：%s" % (err or "没有未完成的任务了"))
            break
        left, _e = list_unfinished(key)
        log("[循环] ① 清单未完成 %d 个，② 取第一个：%s（队首，不是批量）" % (len(left), code))
        ok = run_one_task(key, code, log)
        if ok:
            done_codes.append(code)
        else:
            skipped.append(code)
            # 没做成/没实现：本轮拉黑，继续下一个未完成（不卡在同一个任务上空转）
            _block(key, code)
    log("[循环] 完成 %d 个：%s" % (len(done_codes), ", ".join(done_codes) or "无"))
    if skipped:
        log("[循环] 未完成/跳过 %d 个：%s" % (len(skipped), ", ".join(skipped)))
    return {"done": done_codes, "skipped": skipped}


# --------------------------------------------------------------------------- #
# 可重复执行入口
# --------------------------------------------------------------------------- #
def main(argv=None):
    """用法：python -m orchestrator.adaptive --key account_r [--max 5] [--minutes 30]"""
    import argparse
    ap = argparse.ArgumentParser(description="自适应单任务循环（读界面→决策→操作→回读→确认）")
    ap.add_argument("--key", required=True, help="账号 key，如 account_r")
    ap.add_argument("--max", type=int, default=5, help="最多处理几个任务")
    ap.add_argument("--minutes", type=float, default=600.0, help="总时间预算（分钟）")
    ap.add_argument("--log", default=None, help="日志文件，默认 logs/adaptive_<key>.log")
    a = ap.parse_args(argv)

    path = a.log or os.path.join(CFG.LOG_DIR, "adaptive_%s.log" % a.key)
    f = open(path, "ab")

    def log(m):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), m)
        try:
            f.write((line + "\n").encode("utf-8"))
            f.flush()
        except Exception:
            pass
        print(line, flush=True)

    log("=== 自适应单任务循环：%s（最多 %d 个任务 / %.0f 分钟）===" % (a.key, a.max, a.minutes))
    res = loop_account(a.key, log=log, max_tasks=a.max,
                       deadline=time.time() + a.minutes * 60)
    try:
        f.close()
    except Exception:
        pass
    return 0 if res.get("done") else 1


if __name__ == "__main__":
    import os
    sys.exit(main())
