#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill_1：在技能中心**使用**一个已安装的技能。

任务要求（2026-09-22 实证）：技能 tab →「我安装的」列表 → 点技能卡片的「使用/打开」
→ 打开该技能的会话 → 发一句话 → 等回复，服务端才计分。

⚠️ 不是点「+」安装新技能（skill-add-btn）：那是「直接安装技能」，点了只装不用，
进度不涨。也不是顶部工具栏的「添加技能」（那是创建自定义技能）。

实证（account_k）：点「使用」→ 会话打开 → 发「你好」→ 进度 0/1 → 1/1 completed。
"""
import json
import time

from engine import driver as U

SKILL_ENTER = r"""
(() => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const up = () => !!document.querySelector('.ec-list-tab');
  if (up()) return { ok: true, already: true };
  // 精确文本匹配：不能只找含「专家」的按钮（会命中侧边栏里标题带「专家」的会话）
  const entry = Array.from(document.querySelectorAll('button.conversation-list-tab-button'))
    .find(b => T(b) === '专家·技能·连接器');
  if (!entry) return { ok: false, err: '侧边栏找不到「专家·技能·连接器」入口' };
  return { ok: true, expr: "Array.from(document.querySelectorAll('button.conversation-list-tab-button')).find(b => ((b.innerText||'').replace(/\\s+/g,' ').trim() === '专家·技能·连接器'))" };
})()
"""

SKILL_WAIT_PANEL = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 25; i++) {
    if (document.querySelector('.ec-list-tab')) return { ok: true, waited: i };
    await sleep(800);
  }
  return { ok: false, err: '专家/技能中心面板没打开' };
})()
"""

SKILL_SWITCH_TAB = r"""
(() => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const tab = Array.from(document.querySelectorAll('.um-tab, [class*="um-tab"], .ec-list-tab, [role="tab"]'))
    .find(e => T(e) === '技能');
  if (!tab) {
    return { ok: false, err: '找不到「技能」tab',
             have: Array.from(document.querySelectorAll('.um-tab, .ec-list-tab'))
               .slice(0, 12).map(T) };
  }
  const active = (tab.className || '').toString().indexOf('active') !== -1
              || (tab.getAttribute('aria-selected') || '') === 'true';
  if (active) return { ok: true, already: true, tab: T(tab) };
  return { ok: true, expr: "Array.from(document.querySelectorAll('.um-tab, [class*=\\\"um-tab\\\"], .ec-list-tab, [role=\\\"tab\\\"]')).find(e => ((e.innerText||'').replace(/\\s+/g,' ').trim() === '技能'))" };
})()
"""

SKILL_INSTALLED_TAB = r"""
(() => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const btn = Array.from(document.querySelectorAll('button, [role="tab"]')).filter(vis)
    .find(b => { const t = T(b); return t.startsWith('我安装的'); });
  if (!btn) return { ok: false, err: '找不到「我安装的」切换按钮' };
  return { ok: true, expr: "Array.from(document.querySelectorAll('button, [role=\\\"tab\\\"]')).filter(e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;}).find(b => ((b.innerText||'').replace(/\\\\s+/g,' ').trim().startsWith('我安装的'))" };
})()
"""

SKILL_USE_BTN = r"""
(() => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  // 「我安装的」列表里卡片的按钮文案是「使用」或「打开」；点击后打开该技能的会话。
  const section = document.querySelector('.skills-installed-section, [class*="skills-installed"]') || document.body;
  const cards = Array.from(section.querySelectorAll('.skill-card, [class*="skill-card"]')).filter(vis);
  for (const card of cards) {
    const btn = Array.from(card.querySelectorAll('button')).filter(vis)
      .find(b => { const t = T(b); return t === '使用' || t === '打开'; });
    if (btn) {
      const title = card.querySelector('.skill-card-name, .skill-card__name, h3, [class*="title"], [class*="name"]');
      return {
        ok: true, skill: title ? T(title) : T(card).slice(0, 40), btnText: T(btn),
        expr: (() => {
          const allCards = Array.from(section.querySelectorAll('.skill-card, [class*="skill-card"]')).filter(vis);
          const ci = allCards.indexOf(card);
          const allBtns = Array.from(card.querySelectorAll('button')).filter(vis);
          const bi = allBtns.indexOf(btn);
          const sectionSel = '.skills-installed-section, [class*=\\\"skills-installed\\\"]';
          return "(Array.from(document.querySelectorAll(" + JSON.stringify(sectionSel) + ")).filter(e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;})[0] || document.body)"
            + ".querySelectorAll('.skill-card, [class*=\\\"skill-card\\\"]')"
            + " && Array.from((Array.from(document.querySelectorAll(" + JSON.stringify(sectionSel) + ")).filter(e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;})[0] || document.body).querySelectorAll('.skill-card, [class*=\\\"skill-card\\\"]')).filter(e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;})[" + ci + "]"
            + ".querySelectorAll('button')[" + bi + "]";
        })()
      };
    }
  }
  // 兜底：全局找「使用/打开」文字按钮
  const all = Array.from(document.querySelectorAll('button')).filter(vis);
  const btn = all.find(b => { const t = T(b); return t === '使用' || t === '打开'; });
  if (btn) {
    return { ok: true, skill: '全局匹配', btnText: T(btn),
             expr: "Array.from(document.querySelectorAll('button')).filter(e=>{const r=e.getBoundingClientRect();return r.width>8&&r.height>8;}).find(b => { const t = (b.innerText || '').replace(/\\\\s+/g,' ').trim(); return t === '使用' || t === '打开'; })" };
  }
  return { ok: false, err: '「我安装的」列表里找不到「使用/打开」按钮',
           page: document.body.innerText.replace(/\s+/g, ' ').slice(0, 300) };
})()
"""

SKILL_DISMISS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 3; i++) {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await sleep(700);
  }
  return { dismissed: true };
})()
"""


def do_skill_1(verbose=False):
    """skill_1：使用一个已安装技能（打开技能会话 → 发一句话 → 等回复）。

    所有关键点击都走 Python 侧 CDP 真实鼠标（规避 Electron 对合成事件的过滤）。
    """
    out = {"kind": "skill_1"}

    # 1) 关浮层，避免侧边栏被遮挡
    U.js("""(() => { for (let i = 0; i < 3; i++) document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); })()""", timeout=30)
    time.sleep(0.6)

    # 2) 打开专家/技能中心面板
    r1 = U.js(SKILL_ENTER, timeout=120)
    out["enter"] = r1
    if not r1.get("ok"):
        out["ok"] = False
        out["err"] = "打开技能中心失败：%s" % (r1.get("err") or r1)
        if verbose:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return out
    if not r1.get("already"):
        out["m_enter"] = U.mouse_click(r1["expr"])
        time.sleep(2.2)
        r1b = U.js(SKILL_WAIT_PANEL, timeout=60)
        out["wait_panel"] = r1b
        if not r1b.get("ok"):
            out["ok"] = False
            out["err"] = "技能中心面板没打开：%s" % (r1b.get("err") or r1b)
            if verbose:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            return out

    # 3) 切换到「技能」tab
    r2 = U.js(SKILL_SWITCH_TAB, timeout=90)
    out["switch"] = r2
    if not r2.get("ok"):
        out["ok"] = False
        out["err"] = "定位技能 tab 失败：%s" % (r2.get("err") or r2)
        if verbose:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return out
    if not r2.get("already"):
        out["m_switch"] = U.mouse_click(r2["expr"])
        time.sleep(2.6)

    # 4) 切换到「我安装的」列表（已安装技能才有「使用/打开」入口）
    time.sleep(1.0)
    r_tab = U.js(SKILL_INSTALLED_TAB, timeout=60)
    out["installed_tab"] = r_tab
    if not r_tab.get("ok"):
        out["ok"] = False
        out["err"] = "定位「我安装的」tab 失败：%s" % (r_tab.get("err") or r_tab)
        if verbose:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return out
    out["m_installed_tab"] = U.mouse_click(r_tab["expr"])
    time.sleep(2.0)

    # 5) 在已安装卡片里定位「使用/打开」按钮，用 CDP 真实鼠标点击
    r4 = U.js(SKILL_USE_BTN, timeout=90)
    out["find"] = r4
    if not r4.get("ok"):
        out["ok"] = False
        out["err"] = "定位已安装技能的「使用/打开」按钮失败：%s" % (r4.get("err") or r4)
        if verbose:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    expr = r4.get("expr")
    if not expr:
        out["ok"] = False
        out["err"] = "定位按钮成功但没有可点击表达式"
        if verbose:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    out["skill"] = r4.get("skill")
    out["btnText"] = r4.get("btnText")
    out["mouse"] = U.mouse_click(expr)
    if not out["mouse"].get("ok"):
        out["ok"] = False
        out["err"] = "点击技能按钮失败：%s" % out["mouse"]
        if verbose:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    # 6) 等技能会话页加载（从技能中心导航到对话页）
    wait_convo = U.js(r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 30; i++) {
    const viewport = document.querySelector('.cr-message-list-viewport');
    const ed = document.querySelector('[contenteditable=true]');
    if (viewport && ed) return { ok: true, waited: i };
    await sleep(500);
  }
  return { ok: false, err: '点击技能后未进入会话页' };
})()
""", timeout=30)
    out["wait_convo"] = wait_convo
    if not wait_convo.get("ok"):
        out["ok"] = False
        out["err"] = "进入技能会话失败：%s" % wait_convo.get("err")
        if verbose:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    time.sleep(1.5)
    # 授权浮层（如「选择工作空间 / 允许完全访问」）会挡住输入框，先 Escape 关掉
    U.js("""(() => { for (let i = 0; i < 3; i++) document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); })()""", timeout=30)
    time.sleep(0.8)

    # 7) 发一句话前先切到免费模型（2026-09-26 修复：与其他会话任务对齐，
    #    避免沿用自定义模型发送导致 UNKNOWN_STOP_REASON 而服务端不计分）
    try:
        _m_cands = list(U.cheap_models())
    except Exception:
        _m_cands = []
    out["model_cands"] = _m_cands
    if _m_cands:
        for _m_name in _m_cands:
            _m_res = U.js(
                "(async () => { const m = await pickModel(%s); return m; })()"
                % json.dumps(_m_name),
                timeout=60,
            )
            if isinstance(_m_res, dict) and _m_res.get("ok"):
                out["model"] = _m_name
                break
        if not out.get("model"):
            out["ok"] = False
            out["err"] = "免费模型切换失败（候选 %s），已中止发送" % _m_cands
            if verbose:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            return out

    # 8) 发一句话（召唤本身不计分，必须真的产生一次技能对话）
    out["typed"] = U.type_text("你好", do_send=True)
    if not out["typed"].get("ok"):
        out["ok"] = False
        out["err"] = "技能会话发送消息失败：%s" % out["typed"]
        if verbose:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    # 9) 等 AI 回复完成（用 PRELUDE 里的 waitDone；并识别错误态）
    out["run"] = U.js("(async () => { return await waitDone(90); })()", timeout=200)
    if isinstance(out["run"], dict) and out["run"].get("started") is False:
        _vc = U.js("(async () => { return await verifyConversationStarted(); })()", timeout=10)
        if isinstance(_vc, dict) and _vc.get("error"):
            out["ok"] = False
            out["err"] = "技能会话报错（%s）" % _vc["error"]
            out["verify_error"] = _vc["error"]
            if verbose:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            return out

    # 关掉可能弹出的授权/安装二次确认
    out["dismiss"] = U.js(SKILL_DISMISS, timeout=60)
    out["ok"] = True
    if verbose:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return out
