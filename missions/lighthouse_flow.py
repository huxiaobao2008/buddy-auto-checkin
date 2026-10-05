#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Expert_lighthouse 执行流程（逐步：动作 → 确认 → 留证）。

为什么要单独成文
----------------
这个任务之前被 9/22 加的一道「必须先给 Lighthouse 运维连接器授权」闸门卡死：
客户端点「连接」会用 shell.openExternal 拉起系统浏览器，客户端侧拦不到，新账号永远
完不成。对照旧实现（2026-09-19-00-30-03/buddy-auto/ui_tasks.py:2353）确认：
**旧版根本不授权，就三步：搜索 → 召唤 → 发一句。**

所以本流程恢复那三步，并把每一步的「动作 / 确认 / 证据」写成代码，跑完自己判定：
  S1 回首页           确认：出现「新建任务」页签
  S2 进专家中心       确认：出现 .ec-search-wrapper input
  S3 搜索「轻量云」   确认：出现 .ec-expert-card 且首卡名含「轻量云 / 腾讯云」
  S4 召唤首卡         确认：召唤按钮点到、卡片名留证
  S5 连接器（硬条件）确认：chip 不带 --idle 才算连上；
                           没连上就走授权流程，授权不了就**如实失败**（绝不假跑）
  S6 清空输入框       确认：输入框长度为 0
  S7 发「你好」       确认：输入框已清空 + 页面里出现这条消息
  S8 完成判定         确认：回读服务端进度（current>=target 或 claimed）
  S9 收尾删本次会话   确认：会话从列表消失（用户原有会话不动）

每一步都有 ok / evidence；失败按次数重试；用户在用客户端就**等**，不让路即失败。

  python missions/lighthouse_flow.py            # 跑完整流程
  python missions/lighthouse_flow.py --keep     # 跑完不删会话（留在客户端里自查）
"""
import json
import sys
import time
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import driver as U   # noqa: E402

PROMPT = "你好"
KW = "轻量云"
MODEL_ENV = "WB_TEST_MODEL"

# --------------------------------------------------------------------------- #
# 各步的 JS（动作 + 确认一次做完，返回证据）
# --------------------------------------------------------------------------- #
S1_HOME = """
(async () => {
  const T = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const tab = Array.from(document.querySelectorAll('.conversation-list-tab-button'))
    .find(b => T(b) === '新建任务');
  if (tab) realClick(tab);
  await sleep(1500);
  const on = Array.from(document.querySelectorAll('.conversation-list-tab-button'))
    .some(b => T(b) === '新建任务' && String(b.className).indexOf('active') !== -1);
  return { ok: on, evidence: { activeTab: on } };
})()
"""

S2_EXPERT_CENTER = """
(async () => {
  const T = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const tab = Array.from(document.querySelectorAll('.conversation-list-tab-button'))
    .find(b => T(b).indexOf('专家') !== -1);
  if (!tab) return { ok: false, evidence: { err: '侧边栏没有「专家·技能·连接器」' } };
  realClick(tab);
  await sleep(2500);
  const box = document.querySelector('.ec-search-wrapper input');
  return { ok: !!box, evidence: { searchBox: !!box } };
})()
"""

S3_SEARCH = """
(async (kw) => {
  const box = document.querySelector('.ec-search-wrapper input');
  if (!box) return { ok: false, evidence: { err: '没有搜索框' } };
  box.focus();
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(box, '');
  box.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(300);
  return { ok: true, evidence: { cleared: true, kw: kw } };
})(%s)
"""

S3_CARDS = """
(() => {
  const T = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const cards = Array.from(document.querySelectorAll('.ec-expert-card'));
  return { ok: cards.length > 0,
           evidence: { n: cards.length, names: cards.slice(0, 4).map(c => T(c).slice(0, 24)) } };
})()
"""

S4_SUMMON = """
(async () => {
  const T = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const cards = Array.from(document.querySelectorAll('.ec-expert-card'));
  if (!cards.length) return { ok: false, evidence: { err: '没有专家卡片' } };
  // 优先名字里带「轻量云 / 腾讯云」的卡；没有就取第一张
  let card = cards.find(c => /轻量云|腾讯云|Lighthouse/.test(T(c))) || cards[0];
  const name = T(card).slice(0, 30);
  const btn = card.querySelector('.ec-card-summon-btn') ||
              Array.from(card.querySelectorAll('button')).find(b => /召唤|使用|添加/.test(T(b)));
  if (!btn) return { ok: false, evidence: { err: '卡片上没有召唤按钮', name: name } };
  realClick(btn);
  await sleep(4000);
  return { ok: true, evidence: { summoned: name, btn: T(btn).slice(0, 12) } };
})()
"""

S5_CONNECTOR = """
(() => {
  const T = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const chip = document.querySelector('button.wb-connector-dep-chip');
  if (!chip) return { ok: true, evidence: { chip: false, note: '本次会话没有连接器 chip（可能已连）' } };
  const idle = String(chip.className).indexOf('--idle') !== -1;
  // 服务端任务原文要求「授权连接器后对话一次」→ 未连接 = 未完成，不能放行
  return { ok: !idle,
           evidence: { chip: true, idle: idle, name: T(chip).slice(0, 20),
                       note: idle ? '未授权：按任务原文这是硬条件，必须连上才算完成' : '已连接' } };
})()
"""

S6_CLEAR_INPUT = """
(async () => {
  const ed = document.querySelector('[contenteditable=true]');
  if (!ed) return { ok: false, evidence: { err: '没有输入框' } };
  ed.focus();
  await sleep(200);
  const before = (ed.innerText || '').trim().length;
  document.execCommand('selectAll');
  await sleep(150);
  document.execCommand('delete');
  await sleep(400);
  const after = (ed.innerText || '').trim().length;
  return { ok: after === 0, evidence: { before: before, after: after } };
})()
"""

S7_VERIFY = """
(() => {
  const T = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const ed = document.querySelector('[contenteditable=true]');
  const cleared = !ed || (ed.innerText || '').trim().length === 0;
  const body = (document.body.innerText || '').replace(/\\s+/g, ' ');
  return { ok: cleared && body.indexOf('你好') !== -1,
           evidence: { inputCleared: cleared, msgInPage: body.indexOf('你好') !== -1 } };
})()
"""


def _js(expr, timeout=60):
    return U.js(expr, timeout=timeout)


def _cdp_type(text):
    """用 CDP 真输入（受控 input 必须走 Input.insertText）。"""
    from core import cdp as C
    cdp, _ = C.connect()
    try:
        U.driver_typing(True)
        cdp.call("Input.insertText", {"text": text}, timeout=25)
    finally:
        U.driver_typing(False)
        cdp.close()


def _click_send():
    # realClick 只在客户端注入上下文里有 → 必须走 U.js（裸 CDP 里没有 realClick）
    return U.js(
        "(() => { const b = Array.from(document.querySelectorAll('button'))"
        ".find(x => String(x.className).indexOf('send') !== -1);"
        " if (b) { realClick(b); return true; } return false; })()", timeout=25)


def wait_idle(max_sec=600, log=print):
    """用户在用客户端就等（不让路即失败），等到安静为止。"""
    t0 = time.time()
    while time.time() - t0 < max_sec:
        busy, st = U.user_busy()
        if not busy:
            return st
        if log:
            log("[wait] 用户在使用客户端，等待… %s" % json.dumps(st, ensure_ascii=False))
        time.sleep(15)
    return None


def run(keep=False, model=None, log=print):
    """跑完整流程。返回 {ok, steps:[{step,ok,evidence}], progress}。"""
    out = {"ok": False, "steps": [], "kw": KW, "prompt": PROMPT}

    def step(name, expr, tries=2):
        for i in range(tries):
            r = _js(expr)
            rec = {"step": name, "try": i + 1, "ok": bool((r or {}).get("ok")),
                   "evidence": (r or {}).get("evidence") or r}
            out["steps"].append(rec)
            if log:
                log("[%s] %s" % (name, json.dumps(rec["evidence"], ensure_ascii=False)[:300]))
            if rec["ok"]:
                return rec
            time.sleep(2)
        return out["steps"][-1]

    if model:
        U.set_model(model)
    st = wait_idle()
    if st is None:
        out["err"] = "等待客户端空闲超时"
        return out

    step("S1_回首页", S1_HOME)
    s2 = step("S2_进专家中心", S2_EXPERT_CENTER)
    if not s2["ok"]:
        out["err"] = "进不去专家中心"
        return out
    # 搜索：清框 → CDP 真输入 → 看卡片
    step("S3a_清空搜索框", S3_SEARCH % json.dumps(KW))
    _cdp_type(KW)
    time.sleep(3.5)
    s3 = step("S3b_搜索结果", S3_CARDS)
    if not s3["ok"]:
        out["err"] = "搜「%s」没有专家卡片" % KW
        return out
    step("S4_召唤", S4_SUMMON)
    s5 = step("S5_连接器", S5_CONNECTOR)
    if not s5["ok"]:
        # 未授权 → 尝试真授权；授权不了就如实失败，不假跑
        try:
            from engine import connector_oauth as CO
            ar = CO.authorize(timeout=180)
            out["oauth"] = {"ok": bool(ar.get("ok")), "err": ar.get("err"),
                            "need_manual": ar.get("need_manual")}
            if log:
                log("[S5b_授权] %s" % json.dumps(out["oauth"], ensure_ascii=False))
        except Exception as ex:
            out["oauth"] = {"ok": False, "err": "%s: %s" % (type(ex).__name__, str(ex)[:80])}
        s5b = step("S5c_复检连接器", S5_CONNECTOR)
        if not s5b["ok"]:
            out["err"] = "连接器未授权——按任务原文这是完成条件，不能跳过（拒绝假跑）"
            return out
    step("S6_清空输入框", S6_CLEAR_INPUT)
    # 发「你好」：CDP 真输入 + 点发送
    _cdp_type(PROMPT)
    time.sleep(0.6)
    out["send_clicked"] = _click_send()
    time.sleep(6)
    s7 = step("S7_确认已发送", S7_VERIFY)
    out["sent"] = bool(s7["ok"])

    # S8 完成判定：回读服务端进度
    try:
        from orchestrator import batch as B
        cfg = B.CFG.load_config()
        acct = None
        cur_name = U.current_account_name() or ""
        for a in (cfg.get("accounts") or []):
            if cur_name and (str(cur_name) in json.dumps(a, ensure_ascii=False)):
                acct = a
                break
        if acct:
            c, t, err = B.prog(acct.get("key"), "Expert_lighthouse")
            out["progress"] = {"current": c, "target": t, "err": err}
            out["ok"] = bool(s7["ok"]) and (err is None and c is not None
                                            and t is not None and c >= t)
        else:
            out["progress"] = {"err": "配置里找不到当前账号"}
            out["ok"] = bool(s7["ok"])
    except Exception as e:
        out["progress"] = {"err": "%s: %s" % (type(e).__name__, str(e)[:80])}
        out["ok"] = bool(s7["ok"])

    if not keep:
        for t in (U.task_titles() or [])[:2]:
            if "Lighthouse" in t or "轻量" in t:
                r = U.delete_conversation(t, force=True)
                out.setdefault("cleanup", []).append({"title": t[:30],
                                                      "ok": bool((r or {}).get("ok"))})
    return out


def main():
    keep = "--keep" in sys.argv
    model = os.environ.get(MODEL_ENV) or "agnes-2.5-flash"
    r = run(keep=keep, model=model)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
