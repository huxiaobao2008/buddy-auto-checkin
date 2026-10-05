#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""场景类任务：create_canvas（设计创意模式）/ playbook_prompt（探索优秀灵感）。
- create_canvas：复用统一模板选择流程，优先设计创意分类下可用模板，流程与template_5完全一致
- playbook_prompt：真实鼠标hover左侧「更多」→点浮层「灵感」→选卡片做同款→替换→发送，发射即走
"""

import json
import time

from engine import driver as U

# --------------------------------------------------------------------------- #
# 灵感任务入口：真实鼠标hover「更多」打开浮层，进入灵感页
# --------------------------------------------------------------------------- #
def _enter_inspiration():
    """真实鼠标wiggle hover左侧「更多」，弹出浮层后点击「灵感」，等待灵感页加载完成。"""
    # 找「更多」按钮位置
    rect = U.js(r"""
(function(){
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>8&&r.height>8; };
  const el = Array.from(document.querySelectorAll('button,div,span,a,li')).filter(vis)
    .find(x => (x.innerText||'').replace(/\s+/g,' ').trim() === '更多');
  if(!el) return null;
  const r = el.getBoundingClientRect();
  return {x: r.left + r.width/2, y: r.top + r.height/2};
})()
""", timeout=20)
    if not rect:
        return {"ok": False, "err": "找不到左侧「更多」按钮"}

    from core import cdp as C
    # wiggle鼠标稳定触发hover浮层
    def wiggle_hover():
        c, _ = C.connect()
        try:
            c.call("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": max(0, rect["x"]-6), "y": max(0, rect["y"]-6), "button": "none"
            }, timeout=20)
            time.sleep(0.18)
            c.call("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": rect["x"], "y": rect["y"], "button": "none"
            }, timeout=20)
        finally:
            try: c.close()
            except Exception: pass

    def check_float_open():
        return U.js(r"""
(function(){
  const items = Array.from(document.querySelectorAll('.wb-dropdown__item'));
  const insp = items.find(x => (x.innerText||'').trim() === '灵感');
  return {open: !!insp, candidates: items.map(x => (x.innerText||'').trim()).slice(0,10)};
})()
""", timeout=10)

    # 最多重试3次打开浮层
    opened = None
    for _ in range(3):
        wiggle_hover()
        time.sleep(2)
        opened = check_float_open()
        if opened and opened.get("open"):
            break
        # 移开鼠标重置状态
        c, _ = C.connect()
        try:
            c.call("Input.dispatchMouseEvent", {"type":"mouseMoved","x":4,"y":4,"button":"none"}, timeout=10)
        finally:
            try: c.close()
            except Exception: pass
        time.sleep(0.5)
    if not (opened and opened.get("open")):
        return {"ok": False, "err": "hover后浮层未弹出「灵感」", "candidates": opened and opened.get("candidates")}

    # 真实点击「灵感」
    pos = U.js(r"""
(function(){
  const li = Array.from(document.querySelectorAll('.wb-dropdown__item'))
    .find(x => (x.innerText||'').trim() === '灵感');
  if(!li) return null;
  const r = li.getBoundingClientRect();
  return {x:r.left+r.width/2, y:r.top+r.height/2};
})()
""", timeout=10)
    if not pos:
        return {"ok": False, "err": "浮层已开但找不到「灵感」位置"}
    c, _ = C.connect()
    try:
        c.call("Input.dispatchMouseEvent", {"type":"mouseMoved","x":pos["x"],"y":pos["y"],"button":"none"}, timeout=10)
        time.sleep(0.12)
        c.call("Input.dispatchMouseEvent", {"type":"mousePressed","x":pos["x"],"y":pos["y"],"button":"left","clickCount":1}, timeout=10)
        time.sleep(0.09)
        c.call("Input.dispatchMouseEvent", {"type":"mouseReleased","x":pos["x"],"y":pos["y"],"button":"left","clickCount":1}, timeout=10)
    finally:
        try: c.close()
        except Exception: pass

    # 等待灵感页卡片加载完成，不写死时间
    loaded = False
    t0 = time.time()
    while time.time() - t0 < 20:
        card_check = U.js(r"""
(() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>40&&r.height>40; };
  const cards = Array.from(document.querySelectorAll('.dc-playbook-card,.dc-card,.wb-related-playbooks__card')).filter(vis);
  return {ok: cards.length > 0, count: cards.length};
})()
""", timeout=10)
        if isinstance(card_check, dict) and card_check.get("ok"):
            loaded = True
            break
        time.sleep(1)
    return {"ok": loaded, "entered": loaded, "card_count": card_check and card_check.get("count")}


def _select_inspiration_card_and_fire(idx=0):
    """选灵感卡片→点做同款→处理替换→发送→验证发起，成功即返回。"""
    # 选安全的文本类卡片，过滤媒体类无模板卡片
    card_info = U.js(r"""
(function(idx){
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>40&&r.height>40; };
  const all = Array.from(document.querySelectorAll('.dc-playbook-card,.dc-card,.wb-related-playbooks__card')).filter(vis);
  const T = el => (el.innerText||'').replace(/\s+/g,' ').trim();
  const safe = all.filter(c => !media_blocked(T(c)));
  const pick = safe[Math.min(idx, safe.length-1)] || all[0];
  if(!pick) return {ok:false, err:'无可用灵感卡片'};
  pick.setAttribute('data-insp-target','card');
  return {ok:true, selector:"document.querySelector('[data-insp-target=\"card\"]')", name:T(pick).slice(0,60)};
})(%d)
""" % idx, timeout=20)
    if not (isinstance(card_info, dict) and card_info.get("ok")):
        return {"ok": False, "err": "找不到灵感卡片", "detail": card_info}
    rc = U.mouse_click(card_info["selector"], timeout=20)
    if not rc.get("ok"):
        return {"ok": False, "err": "点击灵感卡片失败"}
    time.sleep(1)

    # 点带+号的「做同款」，无则兜底普通做同款
    same_ok = False
    for _ in range(5):
        same_btn = U.js(r"""
(function(){
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>8&&r.height>8; };
  const btns = Array.from(document.querySelectorAll('button')).filter(vis);
  const T = el => (el.innerText||'').replace(/\s+/g,'').trim();
  let btn = btns.find(b => T(b).includes('做同款') && T(b).includes('+'));
  if(!btn) btn = btns.find(b => T(b).includes('做同款'));
  if(!btn) return {ok:false};
  Array.from(document.querySelectorAll('[data-insp-target]')).forEach(e=>e.removeAttribute('data-insp-target'));
  btn.setAttribute('data-insp-target','same');
  return {ok:true, selector:"document.querySelector('[data-insp-target=\"same\"]')"};
})()
""", timeout=10)
        if isinstance(same_btn, dict) and same_btn.get("ok"):
            click_rc = U.mouse_click(same_btn["selector"], timeout=10)
            if click_rc.get("ok"):
                same_ok = True
                break
        time.sleep(1)
    if not same_ok:
        return {"ok": False, "err": "点击「做同款」失败"}
    time.sleep(1)

    # 处理替换弹窗
    for _ in range(5):
        rep_btn = U.js(r"""
(function(){
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>8&&r.height>8; };
  const dlg = document.querySelector('.dc-confirm-dialog');
  let btn = dlg ? Array.from(dlg.querySelectorAll('button')).find(b => (b.innerText||'').trim()==='替换') : null;
  if(!btn) btn = Array.from(document.querySelectorAll('button')).filter(vis).find(b => /^替换$/.test((b.innerText||'').trim()));
  if(!btn) return {ok:false};
  Array.from(document.querySelectorAll('[data-insp-target]')).forEach(e=>e.removeAttribute('data-insp-target'));
  btn.setAttribute('data-insp-target','replace');
  return {ok:true, selector:"document.querySelector('[data-insp-target=\"replace\"]')"};
})()
""", timeout=10)
        if isinstance(rep_btn, dict) and rep_btn.get("ok"):
            U.mouse_click(rep_btn["selector"], timeout=10)
            break
        time.sleep(1)

    # 等待提示词填充
    filled = False
    t0 = time.time()
    while time.time() - t0 < 15:
        chk = U.js(r"""
(() => {
  const ed = document.querySelector('[contenteditable=true]');
  const t = ed ? (ed.innerText||'') : '';
  return {ok: ed && t.length>10, len: t.length};
})()
""", timeout=10)
        if isinstance(chk, dict) and chk.get("ok"):
            filled = True
            break
        time.sleep(1)
    if not filled:
        return {"ok": False, "err": "做同款后提示词未填充"}

    # 滚动输入框校验参考文件（灵感模板同样需要挂载校验）
    mount_ok = False
    for _ in range(3):
        U.js("(async()=>{ await scrollInputToBottom(); return true; })()", timeout=10)
        ref = U.js("(async()=>{ return await checkReferenceFile(); })()", timeout=10)
        if isinstance(ref, dict) and ref.get("ok"):
            mount_ok = True
            break
        time.sleep(1)
    if not mount_ok:
        return {"ok": False, "err": "灵感模板未挂载"}

    # 发送前强制切到免费模型（2026-09-26 修复：与 fire_template_once 对齐，
    # 旧实现沿用当前自定义模型发送，频繁报 UNKNOWN_STOP_REASON）
    try:
        _m_cands = list(U.cheap_models())
    except Exception:
        _m_cands = []
    _m_used = None
    if _m_cands:
        for _m_name in _m_cands:
            _m_res = U.js(
                "(async () => { const m = await pickModel(%s); return m; })()"
                % json.dumps(_m_name),
                timeout=60,
            )
            if isinstance(_m_res, dict) and _m_res.get("ok"):
                _m_used = _m_name
                break
        if not _m_used:
            return {"ok": False, "err": "免费模型切换失败（候选 %s），已中止发送" % _m_cands, "step": "model_switch"}
        _recheck = U.js(r'''
(() => {
  const ed = document.querySelector('[contenteditable=true]');
  const t = ed ? txt(ed) : '';
  return {ok: ed && t.length > 10, len: t.length};
})()
''', timeout=10)
        if not (isinstance(_recheck, dict) and _recheck.get("ok")):
            return {"ok": False, "err": "切模型后灵感提示词丢失", "step": "model_switch", "detail": _recheck}

    # 发送并验证发起
    mark_send = U.js("(function(){ return markSendBtn(); })()", timeout=10)
    if not (isinstance(mark_send, dict) and mark_send.get("ok")):
        return {"ok": False, "err": "发送按钮不可用"}
    U.mouse_click(mark_send["selector"], timeout=10)

    started = False
    t0 = time.time()
    start_check = None
    while time.time() - t0 < 25:
        start_check = U.js("(async()=>{ return await verifyConversationStarted(); })()", timeout=10)
        if isinstance(start_check, dict):
            if start_check.get("error"):
                return {
                    "ok": False,
                    "err": "会话发起失败（%s）" % start_check["error"],
                    "step": "verify_start",
                    "detail": start_check,
                }
            if start_check.get("started"):
                started = True
                break
        time.sleep(1)
    # 标记灵感模板已使用（计入全局模板账本，避免template_5重复选）
    if started and card_info.get("name"):
        U._tpl_mark_used(U._current_account_key if hasattr(U, '_current_account_key') else "inspiration", "灵感::"+card_info["name"])
    return {"ok": started, "started": started, "template_name": card_info.get("name"), "detail": start_check}


# --------------------------------------------------------------------------- #
# 对外任务入口
# --------------------------------------------------------------------------- #
def do_generic_template(account_key, preferred_cats=None, poll=False):
    """通用模板发射：按preferred_cats优先级选择未使用过的可用模板，发射即走。
    对齐template_5/create_canvas两类任务的统一调用逻辑。
    """
    if preferred_cats is None:
        preferred_cats = ["日常任务", "日常办公", "网站设计", "设计创意"]
    # 收集所有可用模板
    all_tpl, err = U.collect_all_available_templates()
    if err or not all_tpl:
        return {"ok": False, "err": "无可用模板：%s" % (err or "未枚举到模板")}
    used = U._tpl_get_used(account_key)
    # 按优先级分类筛选未使用模板
    target = None
    # 先找优先分类里的
    for cat_pref in preferred_cats:
        target = next(
            (t for t in all_tpl
             if cat_pref in str(t.get("cat") or "") and t.get("id") not in used),
            None
        )
        if target:
            break
    # 优先分类没找到，选任意未使用模板
    if not target:
        target = next((t for t in all_tpl if t.get("id") not in used), None)
    if not target:
        return {"ok": False, "err": "该账号所有可用模板均已使用"}
    return U.fire_template_once(account_key, target)


def do_create_canvas(account_key=None, **kwargs):
    """体验「设计创意模式」：优先选择设计创意分类下未使用模板。"""
    return do_generic_template(
        account_key,
        preferred_cats=["设计创意", "网站设计", "日常任务", "日常办公"]
    )


def do_playbook_prompt(case_idx=2, verbose=False, delete_after=False, poll=False, **kwargs):
    """探索优秀灵感：进入灵感页→选第case_idx张卡片（默认第3张）→做同款发射，成功即返回。"""
    enter_res = _enter_inspiration()
    if not enter_res.get("ok"):
        return enter_res
    return _select_inspiration_card_and_fire(idx=case_idx)
