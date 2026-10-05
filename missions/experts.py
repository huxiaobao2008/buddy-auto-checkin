#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""专家中心驱动（expert_5 / Expert_lighthouse / Expert_team_use_3 的召唤原语）。

结构（实测）：
  侧边栏入口 = button.conversation-list-tab-button「专家·技能·连接器」（精确文本匹配）
  页内顶部   = .um-tab（专家 / 技能 / 连接器）
  列表切换   = .ec-list-tab（「专家」/「专家团」）
  分类       = .ec-category-tab（全部 / 腾讯专家 / …）
  卡片       = article.ec-expert-card，召唤按钮 = .ec-card-summon-btn
  搜索框     = .ec-search-wrapper input

关键结论（实测）：
  - **召唤本身不计分**：召唤只是把专家预填进输入框（还带引导语），
    服务端只在真正产生一次专家对话时才计数 —— 必须再发一句话。
  - 专家中心首屏很重：主线程阻塞 ~45s，JS 内 await sleep 被节流放大十几倍。
    所以 JS 只做「点击+立刻返回」，等待全部交给 Python 的 time.sleep（分步驱动）。
  - pre_must_ok=True：前置步骤失败立刻中止，绝不把消息发进普通对话（否则任务永远不计分）。
"""

import json
import time

from engine import driver as U

# --------------------------------------------------------------------------- #
# 分步驱动：每步一次独立 CDP 调用
# --------------------------------------------------------------------------- #
EXPERT_ENTER_STEP = r"""
(async () => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const loaded = () => !!document.querySelector('.ec-list-tab');
  if (!loaded()) {
    const e = Array.from(document.querySelectorAll('button.conversation-list-tab-button'))
      .find(b => T(b) === '专家·技能·连接器');
    if (!e) return { ok: false, err: '侧边栏找不到「专家·技能·连接器」入口' };
    realClick(e);
  }
  // 等专家中心真正挂载出来再返回（首屏很重，立刻返回会让后续步骤赶在挂载前执行）
  for (let i = 0; i < 40; i++) {
    if (loaded()) return { ok: true, clicked: true, waited: i * 0.5 };
    await sleep(500);
  }
  return { ok: false, err: '专家中心 20s 内没挂载出来' };
})()
"""

EXPERT_PICK_STEP = r"""
(async (kind, kw) => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  if (!document.querySelector('.ec-list-tab')) {
    return { ok: false, err: '专家中心没加载出来' };
  }
  if (kind === 'search') {
    const si = document.querySelector('.ec-search-wrapper input');
    if (!si) return { ok: false, err: '专家中心没有搜索框' };
    return { ok: true, searchBox: true };
  }
  const tabName = (kind === 'team') ? '专家团' : '专家';
  const tab = Array.from(document.querySelectorAll('.ec-list-tab')).find(x => T(x) === tabName);
  if (!tab) return { ok: false, err: '没有列表 tab ' + tabName };
  if (tab.className.toString().indexOf('is-active') === -1) realClick(tab);
  const cat = Array.from(document.querySelectorAll('.ec-category-tab')).find(x => T(x) === '全部');
  if (cat && cat.className.toString().indexOf('is-active') === -1) realClick(cat);
  return { ok: true, tab: tabName };
})()
"""

EXPERT_SUMMON_STEP = r"""
(async (kind, idx, used) => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const usedNames = Array.isArray(used) ? used : [];
  // 名字口径与 Python 侧 _used_experts 一致：优先取标题元素，失败退卡片整段文本
  const nameOf = c => {
    const t = c.querySelector('.ec-card-title, h3, [class*="title"], [class*="name"]');
    const raw = (t && T(t)) ? T(t) : T(c).replace(/^召唤\s*/, '').trim();
    return raw.replace(/\s+/g, ' ').slice(0, 30);
  };
  // 卡片是虚拟列表（react-virtuoso）异步渲染的：切 tab / 搜索后必须轮询等它出来
  let cards = [];
  for (let i = 0; i < 30; i++) {
    cards = Array.from(document.querySelectorAll('.ec-expert-card'));
    if (cards.length) break;
    if (i === 6) {
      const ms = document.querySelector('.ec-main-scroll');
      if (ms) ms.scrollTop = Math.min(260, ms.scrollHeight - ms.clientHeight);
    }
    await sleep(600);
  }
  if (!cards.length) {
    const g = document.querySelector('.ec-expert-grid');
    return { ok: false, err: '卡片列表为空',
             gridItems: document.querySelectorAll('.ec-expert-grid-item').length,
             gridLen: g ? g.innerHTML.length : -1,
             featCards: document.querySelectorAll('.ec-featured-scene-card').length,
             count: (document.querySelector('.ec-expert-count') || {}).innerText || null };
  }
  // 搜索召唤（轻量云）：搜索结果通常只有目标卡，直接取第一张
  if (kind === 'search') {
    const card = cards[0];
    const name = nameOf(card);
    const btn = card.querySelector('.ec-card-summon-btn')
      || Array.from(card.querySelectorAll('button')).find(b => /召唤/.test(T(b)));
    if (!btn) return { ok: false, err: '卡片没有召唤按钮', name: name };
    realClick(btn);
    return { ok: true, name: name, clicked: true, nCards: cards.length };
  }
  // 专家（expert_5）：服务端按「不同专家」计分——虚拟列表只渲染一屏，
  // 必须滚动收集全量卡片，再跳过该账号已召唤过的专家名，否则 idx 越界回落
  // cards[0] = 重复召唤第 1 位 → 永远 +0。
  const seen = {}; const order = [];
  const collect = () => {
    for (const c of document.querySelectorAll('.ec-expert-card')) {
      const n = nameOf(c);
      if (n && !seen[n]) { seen[n] = 1; order.push(c); }
    }
  };
  collect();
  const scrollers = Array.from(document.querySelectorAll('*')).filter(e =>
    e.scrollHeight > e.clientHeight + 60 &&
    (e.querySelector('.ec-expert-card') || e.contains(document.querySelector('.ec-expert-card'))));
  for (const sc of scrollers.slice(0, 4)) {
    try {
      for (let step = 0; step < 20; step++) {
        const before = sc.scrollTop;
        sc.scrollTop = sc.scrollTop + Math.max(300, sc.clientHeight * 0.8);
        await sleep(450);
        collect();
        if (sc.scrollTop === before || sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 4) break;
      }
      sc.scrollTop = 0;
    } catch (e) {}
    collect();
  }
  await sleep(400);
  collect();
  const fresh = order.find(c => usedNames.indexOf(nameOf(c)) === -1);
  if (!fresh) {
    return { ok: false, err: '收集到的专家均已被召唤过，需清空记录重来',
             used: usedNames,
             names: order.map(nameOf).slice(0, 40),
             nCards: order.length };
  }
  const card = fresh;
  const name = nameOf(card);
  const btn = card.querySelector('.ec-card-summon-btn')
    || Array.from(card.querySelectorAll('button')).find(b => /召唤/.test(T(b)));
  if (!btn) return { ok: false, err: '卡片没有召唤按钮', name: name };
  try { card.scrollIntoView({block: 'center'}); await sleep(400); } catch (e) {}
  realClick(btn);
  return { ok: true, name: name, clicked: true, nCards: order.length, fresh: true };
})()
"""

# 点完「继续使用」后客户端要切到新会话视图，contenteditable 输入框挂载有延迟 → 轮询等
EXPERT_STATE_STEP = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let ed = null;
  for (let i = 0; i < 40; i++) {
    ed = document.querySelector('[contenteditable=true]');
    if (ed) break;
    await sleep(500);
  }
  return { ok: !!ed, composer: !!ed,
           guide: ed ? (ed.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 50) : null,
           stillDialog: !!(document.querySelector('.ec-team-summon-confirm-overlay')
             || Array.from(document.querySelectorAll('[class*="_dialogOverlay"]'))
               .some(el => (el.innerText || '').indexOf('我已知悉并确认') !== -1)) };
})()
"""


# --------------------------------------------------------------------------- #
# 轻量云专家的前置闸门：连接器「Lighthouse 运维」必须先授权连上
#
# 用户实测（2026-09-22）：轻量的任务能不能完成，卡点在「连接器有没有连上」——
#   点连接器 → 「微信快捷登录」→「允许」→ 连接成功 → 之后发一句话（哪怕只是「你好」）才算完成。
#   没连上就发消息 = 白跑。所有账号都用同一个微信账号授权，所以每个账号都要走一次。
# 判据：chip 的 class 带 --idle = 未连接；不带 = 已连接（已连接则直接放行，不重复授权）。
# --------------------------------------------------------------------------- #
LH_CONNECTOR = "Lighthouse 运维"

LH_CONNECT_GATE = r"""
(async (name) => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const areaOf = () => document.querySelector('.conversation-mcp-connection');
  const connectBtnOf = () => areaOf() ? areaOf().querySelector('.conversation-mcp-connection__connect') : null;
  const isConnected = () => {
    const area = areaOf();
    if (!area) return true;
    const btn = connectBtnOf();
    if (!btn) return true;
    return T(btn) !== '连接';
  };
  let area = null;
  for (let i = 0; i < 20; i++) {
    area = areaOf();
    if (area) break;
    await sleep(500);
  }
  if (!area) return { ok: true, chip: false, note: 'no connector area' };
  if (isConnected()) return { ok: true, chip: true, connected: true, authorized: true };
  const btn = connectBtnOf();
  if (!btn) return { ok: false, err: 'no connect button' };
  realClick(btn);
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    if (isConnected()) break;
  }
  let wx = null;
  for (let i = 0; i < 20; i++) {
    wx = Array.from(document.querySelectorAll('button')).filter(vis)
      .find(e => T(e) === '微信快捷登录' || T(e) === '微信登录');
    if (wx) break;
    await sleep(500);
  }
  if (wx) {
    realClick(wx);
    await sleep(2500);
    let allow = null;
    for (let i = 0; i < 20; i++) {
      allow = Array.from(document.querySelectorAll('button')).filter(vis)
        .find(e => T(e) === '允许');
      if (allow) break;
      await sleep(500);
    }
    if (allow) { realClick(allow); await sleep(4000); }
  }
  const connected = isConnected();
  return { ok: connected, chip: true, connected: connected,
           wx: wx ? T(wx).slice(0,20) : null };
})()
"""


def _as_fn(src):
    """把 '(async (a) => {...})()' 还原成函数表达式 '(async (a) => {...})'。
    （这些常量写成 IIFE 是为了能直接当 pre_steps 用；当函数传给别的代码时必须去尾部 ()）。"""
    s = (src or "").rstrip()
    if s.endswith("()"):
        s = s[:-2]
    return s


def search_expert(kw):
    """在专家中心的搜索框里输入关键词（受控 input，必须 CDP Input.insertText）。"""
    from core import cdp as C

    cdp, _ = C.connect()
    try:
        ok = cdp.evaluate(
            "(function(){var e=document.querySelector('.ec-search-wrapper input');"
            "if(!e) return false; e.focus();"
            "var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
            "s.call(e,''); e.dispatchEvent(new Event('input',{bubbles:true})); return true;})()",
            timeout=20,
        )
        if not ok:
            return {"ok": False, "err": "找不到专家中心搜索框"}
        U.driver_typing(True)
        cdp.call("Input.insertText", {"text": kw}, timeout=25)
        time.sleep(3.5)
        U.driver_typing(False)
        cards = cdp.evaluate(
            "(function(){return Array.from(document.querySelectorAll('.ec-expert-card')).length;})()",
            timeout=20,
        )
        return {"ok": True, "kw": kw, "nCards": cards}
    finally:
        try:
            U.driver_typing(False)
        except Exception:
            pass
        cdp.close()


def _search_step(kw):
    def _do():
        return search_expert(kw)

    return _do


def _expert_steps(kind, kw, idx, need_connector=False, used=None):
    """构造 (pre_steps, pre_gap)。kind: expert / search。

    need_connector=True（轻量云）：召唤后、发消息前插一道连接器授权闸门——
    连不上就发消息是白跑，所以这道闸门必须排在发送之前。
    used：本账号已召唤过的专家名列表（expert_5 去重用）。召唤步骤会先滚动
    加载全量卡片、跳过 used 里的名字，从「未召唤过」的专家中选卡。
    """
    pick_fn = _as_fn(EXPERT_PICK_STEP)
    summon_fn = _as_fn(EXPERT_SUMMON_STEP)
    steps = [EXPERT_ENTER_STEP]
    gaps = [6]
    if kind == "search":
        steps.append(
            "(async () => { const f = %s; return await f(%s, null, 0); })()"
            % (pick_fn, json.dumps(kind))
        )
        gaps.append(2)
        steps.append(_search_step(kw))
        gaps.append(7)
    else:
        steps.append(
            "(async () => { const f = %s; return await f(%s, null, 0); })()"
            % (pick_fn, json.dumps(kind))
        )
        gaps.append(5)
    steps.append(
        "(async () => { const f = %s; return await f(%s, %d, %s); })()"
        % (summon_fn, json.dumps(kind), idx, json.dumps(used or []))
    )
    gaps.append(5)
    if need_connector:
        steps.append(
            "(async () => { const f = %s; return await f(%s); })()"
            % (_as_fn(LH_CONNECT_GATE), json.dumps(LH_CONNECTOR))
        )
        gaps.append(2)
    steps.append(EXPERT_STATE_STEP)
    gaps.append(2)
    return steps, gaps


def run_expert_once(
    kind="expert",
    kw=None,
    idx=0,
    prompt=None,
    model=None,
    delete_after=False,
    max_wait=60,
    verbose=False,
    expect_nick=None,
    poll=False,
    need_connector=False,
    used=None,
):
    """召唤一个专家 → 发一句话 → 等对话结束 →（默认）删掉会话。

    kind: expert（按索引召唤）/ search（搜索召唤，轻量云用）
    expect_nick：账号硬闸 —— 传了就必须先核对当前登录昵称，对不上拒绝执行。
    poll=False：发完消息立刻返回、不等收尾（并行发射模式；收尾由调用方统一等）。
    used：本账号已召唤过的专家名（expert_5 去重），穿透给召唤步骤。
    """
    if prompt is None:
        prompt = "用一句话简单介绍你自己的能力，直接回答，不要反问。"
    if model is None:
        # 2026-09-23 用户要求：不能让会话静默跑在默认模型（Hy3/Hy4）上，
        # 必须显式落到省量（免费）模型链的第一个；链为空才沿用当前模型。
        chain = U.cheap_models() or []
        if chain:
            model = chain[0]
    if expect_nick:
        cur = U.current_account_name()
        if not (cur and str(expect_nick) in str(cur)):
            return {
                "ok": False,
                "expect_nick": expect_nick,
                "current_nick": cur,
                "err": "账号核对失败：当前=%s，期望=%s —— 拒绝在错误账号上执行"
                % (cur, expect_nick),
            }
    steps, gaps = _expert_steps(kind, kw, idx, need_connector=need_connector, used=used)
    r = U.run_task(
        template=None,
        prompt=prompt,
        model=model,
        use_template=False,
        pre_steps=steps,
        pre_gap=gaps,
        keep_input=False,
        pre_clear=True,
        pre_must_ok=True,
        delete_after=delete_after,
        max_wait=max_wait,
        poll=poll,
        pre_retry=(2 if need_connector else 0),
    )
    if verbose:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    return r


def _finish_expert(out, runs):
    out["runs"] = runs
    out["n_ok"] = sum(1 for x in runs if x["ok"])
    out["ok"] = out["n_ok"] >= out["need"]
    if not out["ok"]:
        out["err"] = "只成功 %d/%d 次" % (out["n_ok"], out["need"])
    return out


def do_expert_5(need=5, start=0, budget=0, account_key=None, **kw):
    """expert_5：串行召唤need位该账号未召唤过的专家，各发指定提示词，验证发起成功立刻召唤下一个，不等回复。"""
    out = {"need": need, "kind": "expert"}
    runs = []
    # 加载已召唤专家账本
    used = list(U._expert_get_used(account_key)) if hasattr(U, '_expert_get_used') and account_key else []
    t0 = time.time()
    for i in range(need):
        if U.stop_requested():
            out["stopped"] = True
            out["aborted"] = "收到停止指令，中止剩余 %d 位专家" % (need - i)
            break
        if budget and (time.time() - t0) > budget:
            out["aborted"] = "超时保护：已达预算 %.0fs，中止剩余 %d 位专家" % (
                budget,
                need - i,
            )
            out["aborted_at"] = i
            break
        r = run_expert_once(kind="expert", idx=start + i, used=used, **kw)
        pre = r.get("pre") or {}
        expert_name = pre.get("name")
        ok = bool(r.get("sent") or r.get("ok"))
        if ok and expert_name and hasattr(U, '_expert_mark_used') and account_key:
            U._expert_mark_used(account_key, expert_name)
            used.append(expert_name)
        runs.append(
            {
                "i": i,
                "idx": start + i,
                "ok": ok,
                "expert": expert_name,
                "summoned": bool(pre.get("ok")),
                "created": r.get("created_title"),
                "err": r.get("err") or pre.get("err"),
            }
        )
        time.sleep(1)
    return _finish_expert(out, runs)


def do_expert_lighthouse(kw_search="轻量云", **kw):
    """Expert_lighthouse：连上「Lighthouse 运维」连接器 → 召唤腾讯轻量云专家 → 发一句。

    ⚠️ 完成条件取自服务端任务原文（2026-09-23 读接口核对，账号 4717）：
        「召唤专家，**在对话框上方授权连接器后**，对话一次，免费领取一个月轻量应用服务器」
      → **连接器授权是硬条件**，没连上就发消息 = 假跑，不算完成。
      → 所以这里必须 need_connector=True：连不上就中止，绝不假装成功。

    注：2026-09-23 我曾照旧实现（9/19）拆掉过这道闸门，那是错的——旧实现当年能跑通
    是因为那批账号早就授权过；新账号必须真授权。已回滚。
    """
    kw.setdefault("prompt", "你好")
    kw["need_connector"] = True
    r = run_expert_once(kind="search", kw=kw_search, idx=0, **kw)
    pre = r.get("pre") or {}
    # pre 是各前置步骤的合并字典，闸门字段直接取；pre_must_ok 保证连不上就不会发消息
    gate = {
        k: pre.get(k)
        for k in ("connected", "chip", "authorized", "wx", "allow", "note")
        if pre.get(k) is not None
    } or None
    out = {"need": 1, "kind": "search", "kw": kw_search, "connector": gate}
    return _finish_expert(
        out,
        [
            {
                "i": 0,
                "ok": bool(r.get("ok")),
                "expert": pre.get("name"),
                "summoned": bool(pre.get("ok")),
                "created": r.get("created_title"),
                "deleted": bool((r.get("deleted") or {}).get("ok")),
                "err": (gate or {}).get("err") or r.get("err") or pre.get("err"),
            }
        ],
    )
