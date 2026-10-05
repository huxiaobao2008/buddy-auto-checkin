#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板类任务：Library_read / Hp_Appearance / Buddy_App·QQ / automation_1。

这些都改全局 UI 状态（开面板/切主题/进应用），必须串行执行，不能与会话型任务并行。

注意：盲盒（buddy_blindbox）是 API 任务，由 engine.missions_api 管线负责，
      此处不再包含任何盲盒相关的客户端 UI 逻辑。
"""

import json
import time

from core import cdp as C
from engine import driver as U


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
HELPERS = r"""
function wbVis(el) { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; }
function wbT(el) { return (el.innerText || '').replace(/\s+/g, ' ').trim(); }
function wbNorm(s) { return (s || '').replace(/\s+/g, ''); }
function clickSidebar(t) {
  const b = Array.from(document.querySelectorAll('.conversation-list-tab-button'))
    .find(e => wbNorm(wbT(e)) === wbNorm(t));
  if (!b) return null;
  realClick(b);
  return wbT(b);
}
function closeOverlays(n) {
  for (let i = 0; i < (n || 3); i++)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
}
function findBtn(scope, text) {
  return Array.from((scope || document).querySelectorAll('button')).filter(wbVis)
    .find(b => wbT(b) === text || wbT(b).indexOf(text) !== -1) || null;
}
function setReactInput(el, val) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(el, val);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}
"""


def _js_block(block, *args, timeout=120.0):
    """把 HELPERS + 一个 (async (…) => {…}) 函数体拼成一次 JS 调用。"""
    code = (
        "(async () => {\n" + HELPERS + "\nconst f = " + block.strip() + ";\n"
        "return await f(" + ", ".join(json.dumps(a) for a in args) + ");\n})()"
    )
    return U.js(code, timeout=timeout)


def type_into(js_focus, text, click_first=True):
    """聚焦指定元素后用 CDP Input.insertText 真实输入（Slate/受控组件都认）。
    Slate 必须先用真鼠标点击获得焦点（Slate 内部要建立 selection），只用 e.focus() 不够。"""
    if click_first:
        U.mouse_click(js_focus)
    cdp, _ = C.connect()
    try:
        ok = cdp.evaluate(
            "(function(){var e=%s; if(!e) return false; e.focus(); return true;})()"
            % js_focus,
            timeout=20,
        )
        if not ok:
            return {"ok": False, "err": "找不到目标元素"}
        U.driver_typing(True)
        # 2026-09-27: Slate 用逐字符真实键盘事件（Input.insertText 只改 DOM 不进
        # Slate 模型）；普通 input/textarea 仍可用 insertText。
        target_is_slate = bool(
            cdp.evaluate(
                "(function(){var e=%s; return !!(e && e.getAttribute && e.getAttribute('contenteditable')==='true');})()"
                % js_focus,
                timeout=10,
            )
        )
        if target_is_slate:
            U._keyboard_type(cdp, text)
        else:
            cdp.call("Input.insertText", {"text": text}, timeout=25)
        time.sleep(0.8)
        got = cdp.evaluate(
            "(function(){var e=%s; if(!e) return null;"
            " return (e.value !== undefined ? e.value : (e.innerText||'')).replace(/\\s+/g,' ').slice(0,80);})()"
            % js_focus,
            timeout=20,
        )
        return {"ok": True, "after": got}
    finally:
        U.driver_typing(False)
        cdp.close()


# --------------------------------------------------------------------------- #
# Library_read：回首页 → 点侧栏「资料库」→ 真鼠标关引导 → 打开文档 → 滚动读完
# （5.6.2 资料库是主窗口普通视图，iframe.space-panel-iframe 已不存在，OOPIF 逻辑全部删除）
# --------------------------------------------------------------------------- #
SIDEBAR_LIB = (
    "Array.from(document.querySelectorAll('.conversation-list-tab-button'))"
    ".find(e => /资料库/.test(e.innerText || ''))"
)

# 收窄选择器：只按 class 片段 + 上限 25 + 单次 innerText 读取（禁止 querySelectorAll('*')）
LIB_DOC_CARD = r"""(()=>{
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const cands = Array.from(document.querySelectorAll(
    '[class*="doc"],[class*="Doc"],[class*="card"],[class*="item"],[class*="list"]'))
    .filter(vis).slice(0, 25);
  const hits = cands.filter(e => /资料库介绍/i.test(e.innerText || '') && (e.innerText || '').length < 60);
  return hits.slice(0, 5).map(e => ({ tag: e.tagName, cls: String(e.className).slice(0, 90),
    t: (e.innerText || '').replace(/\s+/g, ' ').slice(0, 60) }));
})()"""

# 枚举资料库面板里可见的文档卡标题（用于「找不到目标文档时如实说明有哪些」）
LIB_LIST_NAMES = r"""(()=>{
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const seen = [];
  Array.from(document.querySelectorAll(
    '[class*="doc"],[class*="Doc"],[class*="card"],[class*="item"],[class*="title"],li'))
    .filter(vis).slice(0, 40).forEach(e => {
      const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
      if (t && t.length < 80 && seen.indexOf(t) === -1) seen.push(t);
    });
  return seen.slice(0, 20);
})()"""

# 找滚动容器（scrollHeight > clientHeight 最大者）并滚到底。主窗口直接执行。
LIB_SCROLL_JS = r"""(async () => {
  const cands = Array.from(document.querySelectorAll(
    '[class*="scroll"],[class*="Scroll"],[class*="content"],[class*="body"],[class*="doc"],main,article'))
    .filter(e => e.scrollHeight > e.clientHeight + 40)
    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  const el = cands[0];
  if (!el) return { ok: false, err: '无可滚动容器' };
  const steps = [];
  const max = el.scrollHeight - el.clientHeight;
  for (let y = 0; y <= max; y += 600) {
    el.scrollTop = y;
    steps.push(y);
    await sleep(250);
  }
  el.scrollTop = max;
  return { ok: true, steps: steps.length, max: max,
           cls: String(el.className).slice(0, 60) };
})()"""

# 引导弹层「了解资料库 / 我知道了」：必须真实鼠标点（合成事件会把视图切回任务列表）
LIB_ONBOARD_NEXT = (
    "Array.from(document.querySelectorAll('button'))"
    ".filter(b=>{const r=b.getBoundingClientRect();return r.width>8&&r.height>8;})"
    ".find(b=>{const t=(b.innerText||'').replace(/\\s+/g,' ').trim();"
    "return t==='我知道了'||t==='了解资料库'||t==='知道了'||t==='开始使用';})"
)

OVERLAY_CANCEL = r"""
(async () => {
  const closed = [];
  const sc = document.querySelector('.settings-modal__close');
  if (sc) { realClick(sc); closed.push('settings'); await sleep(900); }
  for (let round = 0; round < 4; round++) {
    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
    const overlays = Array.from(document.querySelectorAll(
      '.wb-modal__overlay, [class*=dialogOverlay], [class*=confirm-overlay], ' +
      '.wb-modal, [class*=dialogContainer]')).filter(vis);
    if (!overlays.length) break;
    const btns = Array.from(document.querySelectorAll('button')).filter(vis)
      .filter(b => /^(取消|关闭|我知道了|稍后|返回)$/.test((b.innerText || '').trim()));
    if (!btns.length) break;
    realClick(btns[btns.length - 1]);
    await sleep(1000);
    closed.push('modal');
  }
  return { closed: closed };
})()
"""


def dismiss_overlays(verbose=False):
    out = U.js(OVERLAY_CANCEL, timeout=90)
    if verbose:
        print(json.dumps(out, ensure_ascii=False))
    return out


def _lib_tab_clicked(debug=False):
    """点侧栏「资料库」tab（真实 CDP 鼠标），返回点击结果。"""
    return U.mouse_click(SIDEBAR_LIB, timeout=30)


def ensure_library_open(verbose=False):
    """确保「资料库」面板处于打开状态（5.6.2 主窗口普通视图）。

    RUNBOOK §2.3 实测：停在会话详情页直接点「资料库」不切换，
    必须先回「新建任务」首页 → 再点「资料库」→ 视图才切过去；
    引导弹层「了解资料库 / 我知道了」必须真实鼠标点。
    """
    out = {"steps": []}
    for i in range(6):
        # 先回首页（openNewTask：找「新建任务」按钮点一下）
        U.js("(async () => { await openNewTask(); return true; })()", timeout=60)
        time.sleep(1.5)
        r = _lib_tab_clicked()
        out["steps"].append({"i": i, "click": r})
        if not r.get("ok") and "遮挡" in str(r.get("err")):
            out["dismiss"] = dismiss_overlays()
            r = _lib_tab_clicked()
            out["steps"][-1]["click2"] = r
        time.sleep(3.0)
        # 引导弹层：真实鼠标点「我知道了」
        ob = U.mouse_click(LIB_ONBOARD_NEXT, timeout=20)
        if ob.get("ok"):
            out["onboard_dismissed"] = True
            time.sleep(1.5)
        # 判据：面板切到资料库视图 —— 找到含「资料库介绍」的卡，或至少出现文档卡集合
        cards = U.js(LIB_DOC_CARD, timeout=30) or []
        names = U.js(LIB_LIST_NAMES, timeout=30) or []
        out["cards"] = cards
        out["names"] = names
        if isinstance(names, list) and len(names) >= 1:
            out["ok"] = True
            out["already"] = i == 0
            return out
    out["ok"] = False
    out["err"] = "资料库视图未打开（无任何文档卡可见）"
    if verbose:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return out



def do_library_read(doc="资料库介绍", fallback_docs=None, verbose=False):
    """Library_read：打开资料库 → 展开团队空间 → 依次读完6个文档 → 点外链。"""
    import time as _t
    out = {"attempts": []}

    # 1. 打开资料库面板
    out["open"] = ensure_library_open()
    if not out["open"].get("ok"):
        out["err"] = "打不开资料库面板"
        return out

    # 2. 展开团队空间
    expand_js = r"""
(async () => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const headers = Array.from(document.querySelectorAll('.workspace-tree-section-header-pc'));
  const team = headers.find(h => (h.innerText||'').includes('团队空间'));
  if (!team) return {ok: false, err: 'no team space header'};
  const chev = team.querySelector('.workspace-tree-section-chevron-pc');
  if (chev && !chev.className.includes('expanded')) {
    chev.scrollIntoView({block: 'center'});
    chev.click();
    await new Promise(r => setTimeout(r, 1500));
  }
  // 枚举团队空间下的文档节点
  const nodes = Array.from(team.parentElement.querySelectorAll('.workspace-tree-node-pc'))
    .map(n => ({
      text: T(n.querySelector('.workspace-tree-node-label-pc, [class*="label"]') || n),
      el: n
    }))
    .filter(x => x.text && x.text.length < 80);
  return {ok: true, docs: nodes.map(x => x.text)};
})()
"""
    exp = U.js(expand_js, timeout=30)
    out["team_expand"] = exp
    if not exp.get("ok"):
        out["err"] = "展开团队空间失败: %s" % exp.get("err")
        return out

    docs = exp.get("docs") or []
    out["docs_found"] = docs
    if not docs:
        out["err"] = "团队空间下没有文档"
        return out

    read_ok = 0
    for idx, doc_name in enumerate(docs, 1):
        attempt = {"idx": idx, "doc": doc_name}
        # 点这个文档节点
        #修复：改为 raw string，避免 \s 等正则被 Python 先行解析（SyntaxWarning）
        click_js = r"""
(async () => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const target = %s;
  const nodes = Array.from(document.querySelectorAll('.workspace-tree-node-pc'));
  const node = nodes.find(n => T(n).includes(target));
  if (!node) return {ok: false, err: 'not found: ' + target};
  node.scrollIntoView({block: 'center'});
  await new Promise(r => setTimeout(r, 500));
  const label = node.querySelector('[class*="label"]') || node;
  const r = label.getBoundingClientRect();
  return {ok: true, x: r.left + r.width/2, y: r.top + r.height/2};
})()
""" % json.dumps(doc_name)
        pos = U.js(click_js, timeout=15)
        if not pos.get("ok"):
            attempt["err"] = pos.get("err")
            out["attempts"].append(attempt)
            continue

        # 真实鼠标点击
        from core import cdp as C
        cdp, _ = C.connect()
        cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": int(pos["x"]), "y": int(pos["y"]), "button": "left", "clickCount": 1}, timeout=5)
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": int(pos["x"]), "y": int(pos["y"]), "button": "left", "clickCount": 1}, timeout=5)
        cdp.close()
        _t.sleep(4)

        # 滚动读完
        scroll_r = U.js(LIB_SCROLL_JS, timeout=90)
        attempt["scroll"] = scroll_r
        _t.sleep(2)
        if scroll_r.get("ok"):
            read_ok += 1
        out["attempts"].append(attempt)

    # 3. 最后点"进一步了解WorkBuddy资料库"外链
    LINK_EXPR = r"""
    Array.from(document.querySelectorAll('a, button, span'))
      .filter(e => {
        const r = e.getBoundingClientRect();
        return r.width > 2 && r.height > 2;
      })
      .find(e => /进一步了解.*WorkBuddy.*资料库|进一步了解.*资料库|深入了解.*WorkBuddy/.test((e.innerText || '').replace(/\s+/g, ' ')))
    """
    out["last_link"] = U.mouse_click(LINK_EXPR, timeout=30)

    out["total_docs"] = len(docs)
    out["read_ok"] = read_ok
    link_ok = bool((out.get("last_link") or {}).get("ok"))
    out["ok"] = bool(read_ok >= len(docs) and len(docs) > 0 and link_ok)
    if verbose:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


# --------------------------------------------------------------------------- #
# Hp_Appearance：设置 → 外观 → 点「和平精英激战金秋」主题卡
# --------------------------------------------------------------------------- #
OPEN_SETTINGS = r"""
(async () => {
  const out = { steps: [] };
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  if (document.querySelector('.settings-modal')) { out.already = true; return out; }
  const trig = document.querySelector('.user-menu-trigger');
  if (!trig) return { err: '找不到 .user-menu-trigger' };
  realClick(trig);
  await sleep(1500);
  // 实测（2026-09-22）：菜单项文本是「设置 Ctrl+,」，带快捷键后缀。
  // 原来用全等 === '设置' 永远匹配不上，直接报「用户菜单里没有「设置」」并中止，
  // 表现为「切换过去了但一步都没执行」。改成按「设置」开头匹配。
  const st = Array.from(document.querySelectorAll('.user-menu-item'))
    .find(el => T(el) === '设置' || T(el).indexOf('设置') === 0);
  if (!st) return { err: '用户菜单里没有「设置」（现有：'
                   + Array.from(document.querySelectorAll('.user-menu-item'))
                       .map(el => T(el).slice(0, 12)).join('|') + '）' };
  realClick(st);
  await sleep(2500);
  out.ok = !!document.querySelector('.settings-modal');
  return out;
})()
"""

NAV_SETTINGS = r"""
(async (name) => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const m = document.querySelector('.settings-modal');
  if (!m) return { err: '设置面板没打开' };
  const el = Array.from(m.querySelectorAll('button, [class*=nav], [class*=item], li, div'))
    .filter(vis).filter(e => T(e) === name || T(e).indexOf(name) === 0)
    .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length)[0];
  if (!el) return { err: '设置里没有导航项 ' + name };
  realClick(el);
  await sleep(2000);
  return { ok: true, page: (m.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300) };
})
"""

CLOSE_SETTINGS = r"""
(async () => {
  const b = document.querySelector('.settings-modal__close');
  if (b) { realClick(b); await sleep(1000); }
  if (document.querySelector('.settings-modal')) {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await sleep(900);
  }
  return { closed: !document.querySelector('.settings-modal') };
})()
"""

# 关「直接开在主窗口」的外观面板（没有 .settings-modal 可点，只能 Escape 收掉）
CLOSE_APPEARANCE = r"""
(async () => {
  for (let i = 0; i < 3; i++) {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await sleep(700);
    if (!document.querySelector('.appearance-card-cell')) break;
  }
  return { closed: !document.querySelector('.appearance-card-cell') };
})()
"""

READ_THEMES = r"""
(() => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const out = { cards: [], current: null };
  document.querySelectorAll('.appearance-card-cell').forEach(cell => {
    const btn = cell.querySelector('button.appearance-card');
    const nm = cell.querySelector('.appearance-card__name');
    const name = nm ? T(nm) : T(cell);
    if (!name) return;
    const sel = !!btn && /--selected/.test(btn.className.toString());
    if (sel) out.current = name;
    out.cards.push({ name: name, selected: sel,
                     cls: btn ? btn.className.toString().slice(0, 90) : '' });
  });
  return out;
})()
"""

PICK_THEME = r"""
(async (name) => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 主题卡片异步渲染：轮询最多 15s（切号/进页后列表不会立刻出来）
  let names = [], cell = null;
  for (let i = 0; i < 30; i++) {
    names = Array.from(document.querySelectorAll('.appearance-card__name')).map(T);
    cell = Array.from(document.querySelectorAll('.appearance-card-cell'))
      .find(c => { const nm = c.querySelector('.appearance-card__name'); return nm && T(nm) === name; });
    if (cell || names.length) break;
    await sleep(500);
  }
  if (!cell) return { err: '外观页找不到主题 ' + name, have: names };
  const btn = cell.querySelector('button.appearance-card');
  if (!btn) return { err: '主题卡片没有可点按钮' };
  // 走过场修复：无论是否已选中都真实点击一次，再回读确认确实处于 selected，
  // 不靠「already」早返回误报成功（当前已是目标主题时旧逻辑会直接报成功）。
  realClick(btn);
  await sleep(2500);
  const cell2 = Array.from(document.querySelectorAll('.appearance-card-cell'))
    .find(c => { const nm = c.querySelector('.appearance-card__name'); return nm && T(nm) === name; });
  const btn2 = cell2 ? cell2.querySelector('button.appearance-card') : null;
  const nowSel = btn2 ? /--selected/.test(btn2.className.toString()) : false;
  return { ok: nowSel, wasSelected: /--selected/.test(btn.className.toString()),
           after: btn2 ? btn2.className.toString().slice(0, 90) : '' };
})
"""


# --------------------------------------------------------------------------- #
# 外观的两个入口（用户 2026-09-23 口述，以此为准）
#   入口①：点左下角账号（.user-menu-trigger）→ 菜单里**直接就有「外观」**，点它即到。
#   入口②：设置里也有「外观」。
# 以前只实现了「先点设置 → 再到外观」这一条，绕远且设置是另开窗口，
# 多一次窗口切换就多一处失败点。现在**先走入口①，没找到再退回入口②**。
# --------------------------------------------------------------------------- #
APPEARANCE_DIRECT = r"""
(async () => {
  const out = { steps: [] };
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  if (document.querySelector('.appearance-card-cell')) { out.already = true; return out; }
  const trig = document.querySelector('.user-menu-trigger');
  if (!trig) return { err: '找不到 .user-menu-trigger' };
  realClick(trig);
  await sleep(1500);
  const items = Array.from(document.querySelectorAll('.user-menu-item'));
  const ap = items.find(el => T(el) === '外观' || T(el).indexOf('外观') === 0);
  if (!ap) {
    // 菜单里没有「外观」：把菜单收掉，交给调用方走「设置 → 外观」
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await sleep(600);
    return { ok: false, err: '用户菜单里没有「外观」',
             have: items.map(el => T(el).slice(0, 12)) };
  }
  realClick(ap);
  await sleep(2500);
  // 主题卡可能直接开在主窗口，也可能仍开在设置窗口里 —— 两种都如实回报
  out.ok = true;
  out.in_main = !!document.querySelector('.appearance-card-cell');
  out.clicked = T(ap).slice(0, 20);
  return out;
})()
"""

# 「设置」点开后是**另开一个窗口**（CDP 里是第二个 page 目标），
# 主窗口里永远没有 .settings-modal —— 以前只在主窗口里找，必然报「打开设置失败」，
# 表现为「切过去了但一步都没执行」。2026-09-22 实测确认。
_SETTINGS_CDP = {"c": None}
# 外观面板究竟开在哪：True=主窗口（入口①直达），False=设置窗口（入口②）
_APPEAR_IN_MAIN = {"v": False}


def _find_settings_cdp(max_wait=12.0):
    """在所有 CDP 目标里挑出真正有设置面板的那个窗口。"""
    from core import cdp as C

    t0 = time.time()
    while time.time() - t0 < max_wait:
        for p in C.list_pages():
            ws = p.get("webSocketDebuggerUrl")
            if not ws:
                continue
            try:
                c = C.CDP(ws)
            except Exception:
                continue
            try:
                has = c.evaluate(
                    "(function(){return !!document.querySelector('.settings-modal');})()",
                    timeout=15,
                )
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass
                continue
            if has:
                return c
            try:
                c.close()
            except Exception:
                pass
        time.sleep(1.5)
    return None


def _sjs(code, timeout=90):
    """在设置窗口里跑 JS（与主窗口 U.js 同样带 PRELUDE 工具函数）。"""
    c = _SETTINGS_CDP.get("c")
    if c is None:
        return {"err": "设置窗口未打开"}
    wrapped = (
        "(async () => {\n" + U.PRELUDE + "\nreturn await (\n" + code + "\n);\n})()"
    )
    return c.evaluate(wrapped, timeout=timeout)


def _ajs(code, timeout=90):
    """在「外观面板所在的那个窗口」里跑 JS：入口①在主窗口，入口②在设置窗口。"""
    if _APPEAR_IN_MAIN.get("v"):
        return U.js(code, timeout=timeout)
    return _sjs(code, timeout=timeout)


def open_appearance():
    """打开外观：先走入口①（账号菜单直达），菜单里没有再退回入口②（设置→外观）。"""
    r = U.js(APPEARANCE_DIRECT, timeout=90)
    if isinstance(r, dict) and r.get("already"):
        _APPEAR_IN_MAIN["v"] = True
        return {"ok": True, "window": "main", "already": True}
    if isinstance(r, dict) and r.get("ok"):
        _APPEAR_IN_MAIN["v"] = bool(r.get("in_main"))
        if r.get("in_main"):
            return {"ok": True, "window": "main", "clicked": r.get("clicked")}
        old = _SETTINGS_CDP.get("c")
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        c = _find_settings_cdp()
        _SETTINGS_CDP["c"] = c
        if c is None:
            return {"ok": False, "err": "点了「外观」但设置窗口没出现（最多等 12s）"}
        return {"ok": True, "window": "settings", "clicked": r.get("clicked")}
    # 主窗口菜单里没有「外观」→ 走入口②
    _APPEAR_IN_MAIN["v"] = False
    return open_settings()


def close_appearance():
    """关掉外观面板（主窗口用 Escape 收，设置窗口走 .settings-modal__close）。"""
    if _APPEAR_IN_MAIN.get("v"):
        r = U.js(CLOSE_APPEARANCE, timeout=60)
        _APPEAR_IN_MAIN["v"] = False
        return r
    return close_settings()


def open_settings():
    """点「设置」（在主窗口），然后切到它新开的那个窗口。"""
    r = U.js(OPEN_SETTINGS, timeout=90)
    if isinstance(r, dict) and r.get("already"):
        return r
    if isinstance(r, dict) and r.get("err"):
        return r
    old = _SETTINGS_CDP.get("c")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    c = _find_settings_cdp()
    _SETTINGS_CDP["c"] = c
    if c is None:
        return {"ok": False, "err": "点开「设置」后找不到设置窗口（最多等 12s）"}
    return {"ok": True, "window": "settings"}


def settings_nav(name):
    return _sjs(
        "(async () => { const f = %s; return await f(%s); })()"
        % (NAV_SETTINGS, json.dumps(name)),
        timeout=90,
    )


def close_settings():
    r = _sjs(CLOSE_SETTINGS, timeout=60)
    c = _SETTINGS_CDP.get("c")
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    _SETTINGS_CDP["c"] = None
    return r


def read_themes():
    return _ajs(READ_THEMES, timeout=60)


def pick_theme(name):
    return _ajs(
        "(async () => { const f = %s; return await f(%s); })()"
        % (PICK_THEME, json.dumps(name)),
        timeout=90,
    )


def current_theme():
    return (read_themes() or {}).get("current")


def do_hp_appearance(theme="和平精英激战金秋", restore=None, verbose=False):
    """Hp_Appearance：使用「和平精英」主题（可选任务生效后切回原主题）。

    走过场修复：若当前已是目标主题，先真实切到一个不同的可用主题、再切回目标，
    确保发生过真实的「切换」动作，而不是停在已选状态误报成功。
    """
    out = {"theme": theme}
    # 入口①：点账号 → 菜单里直接点「外观」；没找到才退回入口②设置→外观
    r = open_appearance()
    out["open"] = r
    if not r.get("ok") and not r.get("already"):
        out["ok"] = False
        out["err"] = "打开外观失败：%s" % r
        return out
    if r.get("window") == "settings":
        nav = settings_nav("外观")
        out["nav"] = {"ok": nav.get("ok"), "err": nav.get("err")}
        if not nav.get("ok"):
            close_appearance()
            out["ok"] = False
            out["err"] = "进入外观页失败"
            return out
    out["before"] = current_theme()
    # 走过场修复：若当前已是目标主题，先切到一个不同的可用主题，再切回目标
    if out["before"] == theme:
        themes = (read_themes() or {}).get("cards") or []
        other = next(
            (t.get("name") for t in themes if t.get("name") and t.get("name") != theme),
            None,
        )
        if other:
            p0 = pick_theme(other)
            out["pre_switch"] = p0
            time.sleep(3.0)
            out["mid"] = current_theme()
    p = pick_theme(theme)
    out["applied"] = p
    time.sleep(3.0)
    out["after"] = current_theme()
    out["ok"] = bool(out["after"]) and theme in str(out["after"])
    if not out["ok"] and not out.get("err"):
        out["err"] = (
            (p or {}).get("err") if isinstance(p, dict) else None
        ) or "主题应用后未生效（当前主题=%s，目标可能已下架）" % out["after"]
    if not out["ok"]:
        perr = (p or {}).get("err") if isinstance(p, dict) else None
        if perr and "找不到主题" in perr:
            # 目标不存在（活动下架等）= 环境不可用：不空转重试、审计豁免
            out["env_unavailable"] = True
            out["err"] = "主题「%s」不在可用列表（活动可能已结束）：%s" % (theme, perr)
        else:
            out["err"] = "主题应用未生效：pick=%s after=%s" % (p, out["after"])
    if restore and restore != theme:
        pick_theme(restore)
        time.sleep(2.0)
        out["restored"] = current_theme()
    out["closed"] = close_appearance()
    if verbose:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


# --------------------------------------------------------------------------- #
# Buddy_App / Buddy_App_QQ：发现应用 → 进入 → 退出（完整闭环）
#
# 实测流程（2026-09-22，账号 <手机号已脱敏>）：
#   发现应用 → 点应用 → 可能弹「确认授权」→ 可能弹「绑定 xx 账号」（前往绑定/跳过/×）
#   → 点「跳过」进入应用界面（侧边栏收窄成应用场景 tab，右栏出现「授权已确认」）
#   → 应用界面有 aria-label="退出当前 Buddy 应用" 按钮
#   → 点它退出 → 侧边栏恢复完整（新建任务/助理/项目/…）
# 进得去也要退得出来：以「退出按钮消失」为退出成功的判据。
# --------------------------------------------------------------------------- #
BUDDY_APP = r"""
(async (appName) => {
  closeOverlays(3);
  await sleep(600);
  clickSidebar('新建任务');
  await sleep(1500);
  const disc = findBtn(document, '发现应用');
  if (!disc) return { err: '找不到「发现应用」' };
  realClick(disc);
  await sleep(2200);
  const pop = Array.from(document.querySelectorAll('.wb-popover')).filter(wbVis)[0];
  if (!pop) return { err: '发现应用弹层没出现' };
  const apps = Array.from(pop.querySelectorAll('*')).filter(wbVis)
    .filter(e => e.children.length <= 3 && wbT(e) && wbT(e).indexOf(appName) === 0)
    .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  if (!apps.length) {
    return { err: '弹层里没有 ' + appName,
             have: Array.from(pop.querySelectorAll('*')).filter(wbVis)
               .map(e => wbT(e)).filter(t => t && t.length < 30).slice(0, 20) };
  }
  realClick(apps[0]);
  await sleep(3000);
  // 可能弹授权框
  const auth = findBtn(document, '确认授权');
  if (auth) { realClick(auth); await sleep(3500); }
  // 可能弹「绑定 xx 账号」：只进不出会卡在绑定框，点「跳过」不绑定
  const skip = findBtn(document, '跳过');
  if (skip) { realClick(skip); await sleep(2500); }
  // 等「退出当前 Buddy 应用」按钮出现 = 真正进入了应用界面
  const exitBtn = () => Array.from(document.querySelectorAll('button'))
    .find(b => b.getAttribute('aria-label') === '退出当前 Buddy 应用');
  for (let i = 0; i < 24; i++) {
    if (exitBtn()) break;
    await sleep(500);
  }
  const entered = !!exitBtn();
  if (!entered) {
    return { ok: false, err: '点进入后没看到应用界面（无退出按钮）',
             authorized: !!auth, skippedBind: !!skip,
             body: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 200) };
  }
  // 退出应用
  realClick(exitBtn());
  await sleep(2500);
  const exited = !exitBtn();
  return { ok: exited, clicked: wbT(apps[0]).slice(0, 40), authorized: !!auth,
           skippedBind: !!skip, entered: entered, exited: exited,
           err: exited ? null : '点了退出但按钮仍在' };
})
"""


def do_buddy_app(app_name="企鹅教师助手"):
    return _js_block(BUDDY_APP, app_name, timeout=120)


# --------------------------------------------------------------------------- #
# automation_1：创建 1 个自动化任务（跑完删掉）
# --------------------------------------------------------------------------- #
ATM_OPEN2 = r"""
(async () => {
  closeOverlays(3);
  await sleep(600);
  if (!document.querySelector('.automation-main-page')) {
    clickSidebar('定时任务');
    await sleep(2500);
  }
  let add = null;
  for (let i = 0; i < 12 && !add; i++) {
    add = findBtn(document, '添加定时任务');
    if (!add) await sleep(700);
  }
  if (!add) return { err: '没有「添加定时任务」按钮',
                     onPage: !!document.querySelector('.automation-main-page') };
  realClick(add);
  await sleep(2800);
  const modal = document.querySelector('.wb-modal');
  if (!modal) return { err: '添加对话框没打开' };
  const input = modal.querySelector('input.atm-modal-input');
  if (input) { realClick(input); }
  const okBtn = findBtn(modal, '确定');
  return { opened: true, focusedName: !!input,
           okDisabled: okBtn ? !!okBtn.disabled : null };
})
"""

ATM_ERRORS = r"""
(() => {
  const m = document.querySelector('.automation-editor-modal');
  if (!m) return { modal: false };
  const ed = m.querySelector('[contenteditable=true]');
  return { modal: true,
           errs: Array.from(document.querySelectorAll('.automation-workspace__error'))
                   .filter(x => wbT(x)).map(x => wbT(x).slice(0, 100)),
           freq: Array.from(m.querySelectorAll('.atm-schedule-trigger')).map(e => wbT(e)),
           perm: (Array.from(m.querySelectorAll('button'))
                    .filter(b => /完全访问|默认权限/.test(b.innerText || ''))[0] || {}).innerText || null,
           slateText: ed ? (ed.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60) : null };
})
"""

ATM_LIST = r"""
(async () => {
  await sleep(300);
  if (!document.querySelector('.automation-main-page')) {
    clickSidebar('定时任务');
    await sleep(3000);
  }
  const page = document.querySelector('.automation-main-page');
  return { txt: page ? page.innerText.replace(/\s+/g, ' ').slice(0, 1000) : null };
})
"""

ATM_PICK_FUTURE = r"""
(async () => {
  const vis = c => { const r = c.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  let cells = Array.from(document.querySelectorAll('.wb-datepicker-grid__cell'))
    .filter(vis).filter(c => !/other-month|disabled/.test(c.className.toString()));
  if (!cells.length) {
    const nextBtn = document.querySelector(
      '.wb-datepicker-header__btn--next, [class*=datepicker-header] [class*=next], ' +
      '.wb-datepicker-header__title-btn--next');
    if (nextBtn) { realClick(nextBtn); await sleep(1200); }
    cells = Array.from(document.querySelectorAll('.wb-datepicker-grid__cell'))
      .filter(vis).filter(c => !/other-month|disabled/.test(c.className.toString()));
  }
  if (!cells.length) return { err: '日历里找不到任何可选日期' };
  const target = cells[cells.length - 1];
  const rect = target.getBoundingClientRect();
  return { day: (target.innerText || '').trim(),
           cls: target.className.toString().slice(0, 80),
           x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
})
"""

ATM_PERM_STATE = r"""
(() => {
  const d = document.querySelector('.automation-permission-confirm__dialog');
  if (!d) return { confirm: false };
  const fallback = d.querySelector('.automation-permission-confirm__fallback-link');
  const cb = d.querySelector('.automation-permission-confirm__checkbox input');
  const ok = Array.from(d.querySelectorAll('button'))
    .find(b => /确认创建/.test(b.innerText || ''));
  return { confirm: true, hasFallback: !!fallback,
           cbChecked: cb ? !!cb.checked : null,
           okDisabled: ok ? !!ok.disabled : null };
})
"""

ATM_BATCH_STATE = r"""
(() => {
  const p = document.querySelector('.automation-main-page');
  if (!p) return { onPage: false };
  const txt = (p.innerText || '').replace(/\s+/g, ' ');
  return { onPage: true, inBatch: /已选择/.test(txt),
           selected: (txt.match(/已选择(\d+)项/) || [])[1] || null,
           names: Array.from(document.querySelectorAll('div.atm-row .atm-row-name'))
                    .map(e => wbT(e)) };
})
"""

ATM_BATCH_ON = r"""
(async () => {
  if (!document.querySelector('.automation-main-page')) {
    clickSidebar('定时任务'); await sleep(3000);
  }
  const p = document.querySelector('.automation-main-page');
  if (!p) return { err: '不在定时任务页' };
  if (/已选择/.test(p.innerText || '')) return { ok: true, already: true };
  const b = findBtn(p, '批量管理');
  if (!b) return { err: '没有「批量管理」按钮' };
  realClick(b);
  await sleep(1500);
  return { ok: /已选择/.test(p.innerText || '') };
})
"""

ATM_BATCH_OFF = r"""
(async () => {
  const p = document.querySelector('.automation-main-page');
  if (!p) return { ok: false };
  if (!/已选择/.test(p.innerText || '')) return { ok: true, already: true };
  const b = findBtn(p, '退出管理');
  if (!b) return { err: '没有「退出管理」按钮' };
  realClick(b);
  await sleep(1200);
  return { ok: true };
})
"""

ATM_BATCH_CONFIRM = r"""
(async () => {
  const m = Array.from(document.querySelectorAll('.wb-modal'))
    .filter(x => x.querySelector('button'))
    .find(x => /删除选中的/.test(x.innerText || ''));
  if (!m) return { modal: false };
  const btn = Array.from(m.querySelectorAll('button'))
    .filter(b => (b.innerText || '').trim() === '删除')
    .sort((a, b) => /danger/.test(b.className.toString())
                  - /danger/.test(a.className.toString()))[0];
  if (!btn) return { modal: true, err: '确认框里没有删除按钮' };
  realClick(btn);
  await sleep(2200);
  return { modal: true, clicked: true };
})
"""

EDITOR_SEL = ".automation-editor-modal"
OK_EXPR = (
    "Array.from(document.querySelectorAll('%s button'))"
    ".find(b => (b.innerText||'').trim() === '确定')" % EDITOR_SEL
)
NAME_EXPR = "document.querySelector('%s input.atm-modal-input')" % EDITOR_SEL
SLATE_EXPR = "document.querySelector('%s [contenteditable=true]')" % EDITOR_SEL
FREQ_EXPR = "document.querySelector('%s .atm-schedule-trigger')" % EDITOR_SEL
DATE_EXPR = (
    "document.querySelector('%s .atm-schedule-date-input .wb-datepicker-trigger')"
    % EDITOR_SEL
)
FALLBACK_EXPR = (
    "document.querySelector('.automation-permission-confirm__fallback-link')"
)
CF_CB_EXPR = "document.querySelector('.automation-permission-confirm__checkbox')"
CF_OK_EXPR = (
    "Array.from(document.querySelectorAll("
    "'.automation-permission-confirm__dialog button'))"
    ".find(b => /确认创建/.test(b.innerText||''))"
)
DAY_EXPR_T = (
    "Array.from(document.querySelectorAll('.wb-datepicker-grid__cell'))"
    ".filter(c => !/other-month|disabled/.test(c.className.toString()) "
    "&& (c.innerText||'').trim() === %s)[0]"
)


def do_automation_1(
    name="每日喝水提醒",
    prompt="每天提醒我喝水。",
    day=None,
):
    """automation_1：设置 1 个自动化任务。

    链路：定时任务页 → 添加 → 名称（原生 setter）→ 提示词（Slate，先真鼠标点再
    Input.insertText）→ 频率浮层 → 日期改到未来 → 确定 → 权限二次确认
    （勾「我已知悉并确认」+ 点「确认创建」= 完全访问权限）→ 回读列表校验。
    """
    out = {"name": name}
    r = _js_block(ATM_OPEN2, timeout=120)
    out["open"] = r
    if not r.get("opened"):
        out["ok"] = False
        out["err"] = "打开对话框失败：%s" % r
        return out

    out["name_type"] = U.js(
        "(async()=>{const i=%s;if(!i)return{err:'no input'};i.focus();"
        "const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
        "s.call(i,%s);i.dispatchEvent(new Event('input',{bubbles:true}));"
        "i.dispatchEvent(new Event('change',{bubbles:true}));return{v:i.value};})()"
        % (NAME_EXPR, json.dumps(name))
    )

    out["prompt_type"] = type_into(SLATE_EXPR, prompt)

    out["m_freq"] = U.mouse_click(FREQ_EXPR)
    time.sleep(1.6)
    out["freq_open"] = U.js(
        "(()=>({popover:!!document.querySelector('.atm-frequency-popover'),"
        "editor:!!document.querySelector('.automation-editor-modal')}))()"
    )
    out["m_date"] = U.mouse_click(DATE_EXPR)
    time.sleep(1.6)
    out["date_open"] = U.js(
        "(()=>({panel:!!document.querySelector('.wb-datepicker-panel-container'),"
        "editor:!!document.querySelector('.automation-editor-modal'),"
        "cells:document.querySelectorAll('.wb-datepicker-grid__cell').length}))()"
    )
    pick = _js_block(ATM_PICK_FUTURE, timeout=90)
    out["day_pick"] = pick
    if pick.get("day"):
        out["m_day"] = U.mouse_click(DAY_EXPR_T % json.dumps(pick["day"]))
        time.sleep(1.4)
    out["schedule"] = _js_block(ATM_ERRORS, timeout=60)

    out["m_name"] = U.mouse_click(NAME_EXPR)
    time.sleep(0.9)

    out["m_ok"] = U.mouse_click(OK_EXPR)
    time.sleep(2.5)
    out["confirm1"] = _js_block(ATM_PERM_STATE, timeout=60)
    if out["confirm1"].get("confirm"):
        # 勾「我已知悉并确认」→ 点「确认创建」（完全访问权限）
        out["m_cb"] = U.mouse_click(CF_CB_EXPR)
        time.sleep(1.1)
        out["m_cf_ok"] = U.mouse_click(CF_OK_EXPR)
        time.sleep(3.0)
        cf2 = _js_block(ATM_PERM_STATE, timeout=60)
        out["confirm2"] = cf2
        if cf2.get("confirm"):
            # 完全访问没走通时退回「默认权限」至少把任务建出来
            out["m_fb"] = U.mouse_click(FALLBACK_EXPR)
            time.sleep(1.8)
            out["m_ok2"] = U.mouse_click(OK_EXPR)
            time.sleep(3.0)
            cf3 = _js_block(ATM_PERM_STATE, timeout=60)
            out["confirm3"] = cf3
            if cf3.get("confirm"):
                U.mouse_click(CF_OK_EXPR)
                time.sleep(3.0)

    time.sleep(1.5)
    out["after"] = _js_block(ATM_ERRORS, timeout=60)
    out["list"] = _js_block(ATM_LIST, timeout=120)
    out["created"] = bool(out["list"].get("txt") and name in out["list"]["txt"])
    out["ok"] = out["created"]
    if not out["ok"]:
        out["err"] = "提交后列表里没出现该任务；after=%s" % out["after"]
    return out


def do_automation_delete(names):
    """在「定时任务」页用批量管理删掉指定名称的自动化。"""
    if isinstance(names, str):
        names = [names]
    out = {"want": names, "deleted": [], "failed": []}
    r = _js_block(ATM_BATCH_ON, timeout=120)
    out["batch_on"] = r
    if not r.get("ok"):
        st0 = _js_block(ATM_BATCH_STATE, timeout=60)
        if st0.get("onPage") and not (st0.get("names") or []):
            out["ok"] = True
            out["already_clean"] = True
            return out
        out["ok"] = False
        out["err"] = "进不了批量管理：%s" % r
        return out
    time.sleep(0.8)
    for nm in names:
        sel = (
            r"Array.from(document.querySelectorAll('div.atm-row'))"
            r".filter(r => { const e = r.querySelector('.atm-row-name');"
            r" return e && (e.innerText||'').replace(/\s+/g,' ').trim() === %s; })[0]"
            % json.dumps(nm)
        )
        found = U.js("(()=>{const r=%s;return r?true:false;})()" % sel)
        if not found:
            out["failed"].append({"name": nm, "err": "列表里没有这条"})
            continue
        U.mouse_click(sel + ".querySelector('.atm-row-checkbox')")
        time.sleep(0.7)
        out["deleted"].append(nm)
    if not out["deleted"]:
        out["ok"] = False
        out["err"] = "没有任何一条被勾选"
        _js_block(ATM_BATCH_OFF, timeout=60)
        return out
    U.mouse_click(
        "Array.from(document.querySelectorAll('.automation-main-page button'))"
        ".find(b => (b.innerText||'').trim() === '删除')"
    )
    time.sleep(1.6)
    out["confirm"] = _js_block(ATM_BATCH_CONFIRM, timeout=90)
    time.sleep(1.5)
    _js_block(ATM_BATCH_OFF, timeout=60)
    time.sleep(1.0)
    st = _js_block(ATM_BATCH_STATE, timeout=60)
    out["after"] = st
    left = [n for n in out["deleted"] if n in (st.get("names") or [])]
    out["failed"] += [{"name": n, "err": "删除后仍在列表里"} for n in left]
    out["deleted"] = [n for n in out["deleted"] if n not in left]
    out["ok"] = bool(out["deleted"]) and not out["failed"]
    return out


def do_automation_1_and_clean(
    name="每日喝水提醒",
    prompt="每天提醒我喝水。",
    keep=True,
):
    """创建自动化任务，默认保留不删除；编排层确认服务端积分到账后，再显式调用do_automation_delete删除。"""
    out = do_automation_1(name, prompt)
    if out.get("ok") and not keep:
        out["clean"] = do_automation_delete(name)
    return out

