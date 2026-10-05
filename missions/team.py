#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""专家团任务（Expert_team_use_3）：召唤并使用 3 个**不同**专家团队。

实测规则（以此为准）：
  1. 必须 3 个不同团队；整轮收尾后才计分（首回复就收工删会话会把团队打断 → +0）。
  2. 无账号级冷却（已实测确认，不加冷却闸）。
  3. 永久尝试账本（data/team_state.json）：试过没计分的团队不再重试；
     候选池空了如实报「需人工接手」，绝不盲试。
  4. 任务完成且积分领取成功后，才清理该任务的会话（由编排层的清理闸负责）。

召唤路径（真人路径，三次真机实验验证）：
  点整张卡片 → 团队详情弹窗 → 点弹窗里的「召唤专家团」→ 勾「我已知悉并确认」
  → 点「继续使用」。卡片角落小按钮点了不弹确认框、团队不成立（nAg=1，永远 +0）。

自包含指令：团队自带推荐词大多要用户给素材（CSV/代码/持仓），会反问挂起 →
永远不计分。所以必须在推荐词后追加自包含执行指令：不反问、不等材料、一轮给结论。
"""

import json
import os
import time

from core import config as CFG
from engine import driver as U

TEAM_MODEL = os.environ.get("TEAM_MODEL") or None

# 2026-09-24 实测定案（account_s/g/k 三次成功验证）：
# 服务端认的是 TeamCreate 事件（子专家被召集），不是最终交付质量。
# 短提示词"介绍团队"足够触发完整协作流程；长"执行规则"反而触发团队反问挂起。
TEAM_SELF_CONTAINED_PROMPT = "请让你们团队每个成员介绍一下自己的团队能力。"

# 候选优先顺序 = 从「自包含、能自己收尾」到「要补材料/多阶段长流程」
TEAM_PREFERRED = [
    "独董会",
    "财税合规专家团",
    "专业文档生成团队",
    "内容变现商业化专家团",
    "腾讯自选股股票投研专家团",
    "腾讯云技术支持",
    "软件开发团队",
    "交易分析团队",
    "内容创作专家团",
    "深度研究团队",
    "科研专家团",
    "产品战略团队",
]

# 账本比对表：会话标题特征词 → 团队名（chip 实测校准）
TEAM_LEDGER_SIGNS = [
    (["分析CSV", "CSV文件生成", "CSV数据画像", "数据画像报告"], "智数分析专家团"),
    (["新产品品牌视觉", "情绪板"], "内容创作专家团"),
    (["调研AI Agent", "AI Agent企业级"], "深度研究团队"),
    (["独立审议起手卡", "关键决策", "独立审议"], "独董会"),
    (["茅台", "基本面与估值", "持仓"], "交易分析团队"),
    (["贪吃蛇", "游戏并自我介绍"], "软件开发团队"),
    (["工程审查", "事故响应", "工程保障"], "工程保障团队"),
    (["DID", "政策处理效应", "发表级表格"], "科研专家团"),
    (["云资源风险", "轻量云", "云资源"], "腾讯云技术支持"),
    (["WriteFlow", "滚动叙事"], "软件工坊"),
    (["MVP", "产品原型"], "MVP开发专家团"),
    (["游戏开发", "游戏工作室"], "游戏开发工作室"),
    (["功能规格书", "竞品分析", "产品路线图"], "产品战略团队"),
]


# --------------------------------------------------------------------------- #
# 永久尝试账本
# --------------------------------------------------------------------------- #
def _team_state_file():
    return os.path.join(CFG.DATA_DIR, "team_state.json")


def load_team_state():
    return CFG.load_json(_team_state_file(), {}) or {}


def record_team_attempt(key, entry):
    p = _team_state_file()
    st = load_team_state()
    st.setdefault(key, {}).setdefault("attempts", []).append(entry)
    CFG.save_json(p, st)


def ledger_used_teams():
    """读任务区会话标题 → 反推该账号已经用过的专家团名集合。"""
    titles = U.task_titles() or []
    blob = " ".join(str(t) for t in titles)
    used = set()
    for keys, name in TEAM_LEDGER_SIGNS:
        if any(k in blob for k in keys):
            used.add(name)
    return sorted(used), titles


# --------------------------------------------------------------------------- #
# 召唤原语
# --------------------------------------------------------------------------- #
def _team_name_eq(card_title, want):
    a = (card_title or "").strip()
    b = (want or "").strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def list_team_cards(timeout=40.0, log=None):
    """运行时读「专家团」tab 的真实卡片名单（含滚动加载，懒加载列表只渲染一屏）。

    以前是 150s × js 内 3 次重试 × 2 轮 = 最坏 15 分钟**静默**；切号 reload 一命中
    就表现为「切过去了但什么都没做」。现在单轮上限可控，且每轮都有日志可见。
    """
    say = log or (lambda *a, **k: None)
    for attempt in range(2):
        _t0 = time.time()
        say(
            "    读取专家团卡片名单（第 %d 轮，单轮上限 %.0fs）…"
            % (attempt + 1, timeout)
        )
        r = U.js(
            r"""
(async () => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const entry = () => Array.from(document.querySelectorAll('button.conversation-list-tab-button'))
    .find(b => T(b) === '专家·技能·连接器');
  const up = () => !!document.querySelector('.ec-list-tab');
  for (let round = 0; round < 3; round++) {
    if (up()) break;
    const e = entry();
    if (!e) { await sleep(1200); continue; }
    realClick(e);
    for (let i = 0; i < 24; i++) { await sleep(500); if (up()) break; }
  }
  if (!up()) return { ok: false, err: '专家中心打不开' };
  await sleep(1500);
  const tab = Array.from(document.querySelectorAll('.ec-list-tab')).find(x => T(x) === '专家团');
  if (tab && (tab.className || '').toString().indexOf('is-active') === -1) {
    realClick(tab);
    await sleep(3000);
  }
  await sleep(1500);
  let cards = [];
  for (let i = 0; i < 30; i++) {
    cards = Array.from(document.querySelectorAll('.ec-expert-card'));
    if (cards.length) break;
    await sleep(600);
  }
  // 滚动加载：团队列表是懒加载/虚拟滚动，视口内只有前一屏，不滚动会误判下架
  const seen = {}; const order = [];
  const collect = () => {
    for (const c of document.querySelectorAll('.ec-expert-card')) {
      const t = c.querySelector('.ec-card-title, h3, [class*="title"]');
      const name = (t && T(t)) ? T(t) : T(c).replace(/^召唤\s*/, '').slice(0, 24);
      if (name && !seen[name]) { seen[name] = 1; order.push(name); }
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
  return { ok: order.length > 0, names: order };
})()
""",
            timeout=timeout,
        )
        say(
            "    读取结束（%.1fs）：ok=%s，名单 %d 个"
            % (
                time.time() - _t0,
                (r or {}).get("ok"),
                len((r or {}).get("names") or []),
            )
        )
        if isinstance(r, dict) and r.get("ok"):
            return [str(x) for x in (r.get("names") or [])]
    return []


def summon_team_by_index(idx, want_name=None):
    """一步完成：开面板 → 切「专家团」→ 点第 idx 张卡片 → 点弹窗内「召唤专家团」。

    合并成一步的原因：专家中心面板在两次独立 CDP 调用之间会被关掉
    （app 单视图、切号/重绘后回落），分步做经常报「卡片列表为空」。
    """
    r1 = U.js(
        r"""
(async () => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const entry = () => Array.from(document.querySelectorAll('button.conversation-list-tab-button'))
    .find(b => T(b) === '专家·技能·连接器');
  const up = () => !!document.querySelector('.ec-list-tab');
  for (let round = 0; round < 3; round++) {
    if (up()) break;
    const e = entry();
    if (!e) return { ok: false, stage: 'entry', err: '侧边栏找不到「专家·技能·连接器」入口' };
    realClick(e);
    for (let i = 0; i < 24; i++) { await sleep(500); if (up()) break; }
  }
  if (!up()) return { ok: false, stage: 'panel', err: '专家中心打不开' };
  await sleep(1500);
  const tab = Array.from(document.querySelectorAll('.ec-list-tab')).find(x => T(x) === '专家团');
  if (tab && (tab.className || '').toString().indexOf('is-active') === -1) {
    realClick(tab);
    await sleep(3000);
  }
  await sleep(1500);
  let cards = [];
  for (let i = 0; i < 30; i++) {
    cards = Array.from(document.querySelectorAll('.ec-expert-card'));
    if (cards.length) break;
    await sleep(600);
  }
  return { ok: true, stage: 'panel-ok', nCards: cards.length };
})()
""",
        timeout=180,
    )
    if not (isinstance(r1, dict) and r1.get("ok")):
        return r1

    if not want_name:
        return {
            "ok": False,
            "stage": "args",
            "err": "必须按名字召唤（下标盲点已废弃，防止跑错团队）",
        }
    # 搜索框按名找团（与专家搜索同原语）：专家中心切到「专家团」后搜索框过滤的就是团队卡
    from missions.experts import search_expert

    sr = search_expert(want_name)
    if not (isinstance(sr, dict) and sr.get("ok")):
        return {
            "ok": False,
            "stage": "search",
            "want": want_name,
            "err": "团队搜索失败：%s" % (sr or {}),
        }
    r2 = U.js(
        r"""
(async (want) => {
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const eq = (a, b) => { a = (a || '').trim(); b = (b || '').trim();
    return a && b && (a === b || a.indexOf(b) !== -1 || b.indexOf(a) !== -1); };
  const titleOf = c => {
    const t = c.querySelector('.ec-card-title, h3, [class*="title"]');
    return (t && T(t)) ? T(t) : T(c).replace(/^召唤\s*/, '').slice(0, 30);
  };
  Array.from(document.querySelectorAll('[data-wb-team-target]'))
    .forEach(el => el.removeAttribute('data-wb-team-target'));
  let card = null;
  for (let i = 0; i < 20; i++) {
    card = Array.from(document.querySelectorAll('.ec-expert-card')).find(c => eq(titleOf(c), want));
    if (card) break;
    await sleep(600);
  }
  if (!card) return { ok: false, stage: 'cards', want: want,
                      err: '搜索「' + want + '」后 12s 内找不到同名团队卡（可能已下架或名称不符）' };
  const name = titleOf(card);
  card.setAttribute('data-wb-team-target', '1');
  const target = card.querySelector('.ec-card-main') || card;
  target.scrollIntoView({ block: 'center', behavior: 'instant' });
  await sleep(600);
  return { ok: true, stage: 'card-located', name: name };
})(%s)
"""
        % json.dumps(want_name),
        timeout=120,
    )
    if not (isinstance(r2, dict) and r2.get("ok")):
        return r2
    card_sel = (
        "(document.querySelector('[data-wb-team-target=\"1\"]') || {})"
        ".querySelector('.ec-card-main')"
    )

    rc_card = U.mouse_click(card_sel)
    btn_state = None
    for _i in range(40):
        time.sleep(0.5)
        btn_state = U.js(
            "(() => { const b = document.querySelector('.ec-modal-summon-btn');"
            " return b ? {ok: true, disabled: !!b.disabled} : {ok: false}; })()",
            timeout=30,
        )
        if isinstance(btn_state, dict) and btn_state.get("ok"):
            break
    if not (isinstance(btn_state, dict) and btn_state.get("ok")):
        return {
            "ok": False,
            "stage": "modal",
            "name": r2.get("name"),
            "click": rc_card,
            "err": "团队详情弹窗没出来（真实点击后 20s 内无 .ec-modal-summon-btn）",
        }
    if btn_state.get("disabled"):
        return {
            "ok": False,
            "stage": "modal",
            "err": "「召唤专家团」是禁用态",
            "name": r2.get("name"),
        }

    # CDP 真实鼠标点弹窗里的「召唤专家团」→ 等「使用提醒」确认框（可能没有）
    rc_btn = U.mouse_click("document.querySelector('.ec-modal-summon-btn')")
    r3 = U.js(
        r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const ACK = '我已知悉并确认';
  const ackVisible = () => Array.from(document.querySelectorAll('*'))
    .some(el => (el.innerText || '').indexOf(ACK) !== -1);
  for (let i = 0; i < 30; i++) {
    if (ackVisible()) return { ok: true, stage: 'summoned', dialog: true };
    await sleep(500);
  }
  return { ok: true, stage: 'summoned', dialog: false, note: '无确认框（该类团队不需要）' };
})()
""",
        timeout=180,
    )
    return {
        "ok": bool(isinstance(r3, dict) and r3.get("ok")),
        "stage": (r3 or {}).get("stage"),
        "name": r2.get("name"),
        "nCards": r2.get("nCards"),
        "dialog": (r3 or {}).get("dialog"),
        "err": (r3 or {}).get("err"),
        "panel": r1,
        "modal": r2,
        "summon": r3,
        "click_card": rc_card,
        "click_btn": rc_btn,
    }


def ack_team_summon_confirm(**kw):
    """用真实鼠标点掉「积分消耗提醒」确认框。

    勾选框是 React 受控组件，合成 MouseEvent 点 label 不触发 onChange →
    「继续使用」一直 disabled → 授权不通过。必须真实鼠标。
    确认框没弹 = 授权事件没发 = 不计分：再清一次 suppress 键并等 15s，仍不弹就判失败。
    """

    def _state():
        return U.js(
            r"""
(() => {
  const ov = document.querySelector('.ec-team-summon-confirm-overlay')
          || document.querySelector('[class*="_dialogOverlay"]');
  const ack = Array.from(document.querySelectorAll('*'))
    .some(el => (el.innerText || '').indexOf('我已知悉并确认') !== -1);
  const go = document.querySelector('[data-track-id=expert_team_confirm_continue]');
  const cb = document.querySelector('.ec-confirm-dialog-checkbox');
  return { has: !!ov && ack, go: !!go, goDisabled: go ? go.disabled : null,
           cb: !!cb,
           cbChecked: (() => { if (!cb) return null;
             const inp = cb.querySelector('input') || cb.previousElementSibling;
             return inp && 'checked' in inp ? inp.checked : null; })() };
})()
""",
            timeout=40,
        )

    st = _state()
    if not (isinstance(st, dict) and st.get("has")):
        try:
            U.reset_team_summon_suppress()
        except Exception:
            pass
        for _ in range(30):
            time.sleep(0.5)
            st = _state()
            if isinstance(st, dict) and st.get("has"):
                break
        if not (isinstance(st, dict) and st.get("has")):
            return {
                "ok": False,
                "dialog": False,
                "err": "确认框未弹出（已再清一次 suppress 键并等 15s）——授权事件不会触发，拒绝继续",
            }

    r1 = U.mouse_click(
        "document.querySelector('.ec-confirm-dialog-checkbox')"
        " || Array.from(document.querySelectorAll('label'))"
        ".find(l => (l.innerText || '').indexOf('我已知悉并确认') !== -1)"
    )
    checked = False
    for _ in range(20):
        time.sleep(0.4)
        st = _state()
        if isinstance(st, dict) and st.get("go") and st.get("goDisabled") is False:
            checked = True
            break
        U.mouse_click(
            "document.querySelector('.ec-confirm-dialog-checkbox')"
            " || Array.from(document.querySelectorAll('label'))"
            ".find(l => (l.innerText || '').indexOf('我已知悉并确认') !== -1)"
        )
    if not checked:
        return {
            "ok": False,
            "err": "勾选「我已知悉并确认」未生效（继续使用仍禁用）",
            "click": r1,
            "state": st,
        }

    r2 = U.mouse_click(
        "document.querySelector('[data-track-id=expert_team_confirm_continue]')"
    )
    for _ in range(20):
        time.sleep(0.5)
        st = _state()
        if isinstance(st, dict) and not st.get("has"):
            return {
                "ok": True,
                "dialog": True,
                "checked": True,
                "confirmed": True,
                "click_cb": r1,
                "click_go": r2,
            }
    return {
        "ok": False,
        "err": "点了「继续使用」确认框仍在",
        "click_go": r2,
        "state": st,
    }


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
           guide: ed ? (ed.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 50) : null };
})()
"""


def run_team_once(
    team_name=None,
    idx=0,
    delete_after=False,
    max_wait=60,
    expect_nick=None,
    model=None,
):
    """召唤一个专家团 → 清空原有引导语 → 发送固定提示词 → 验证会话发起成功即返回，不等团队创建/回复/报告。"""
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
    if model is None:
        model = TEAM_MODEL
    steps = [
        U.reset_team_summon_suppress,
        (lambda idx=idx, nm=team_name: summon_team_by_index(idx, want_name=nm)),
        ack_team_summon_confirm,
        EXPERT_STATE_STEP,
    ]
    gaps = [3, 5, 5, 3]
    r = U.run_task(
        template=None,
        prompt=TEAM_SELF_CONTAINED_PROMPT,
        model=model,
        use_template=False,
        pre_steps=steps,
        pre_gap=gaps,
        keep_input=False,
        pre_clear=True,
        pre_must_ok=True,
        delete_after=delete_after,
        max_wait=max_wait,
        poll=False,
    )
    return r


def _team_fail_counts(att):
    """按尝试账本统计「团队不行」的失败次数（未计分才算）。

    排除 team-not-formed：那是 2026-09-24 判据缺陷时代的误杀产物
    （判据在 busy 时也判死、时间窗小于团队真实 30+ 分钟生命周期），
    团队本身有能力，只是脚本砍太早。修复判据后不再按「团队不行」拉黑。
    """
    fail = {}
    for a in att:
        if not a.get("scored"):
            if a.get("reason") == "team-not-formed":
                continue
            fail[a["team"]] = fail.get(a["team"], 0) + 1
    return fail


def pick_candidates(key, log=None):
    """给某账号挑专家团候选：真实名单 → 偏好排序 → 剔除账本已用 + 历史已试。

    返回 (cands, info)。cands 为空 = 设计行为「需人工接手」，调用方如实上报。
    """
    say = log or (lambda *a, **k: None)
    live = list_team_cards(log=say)
    if not live:
        return [], {"err": "读不到专家团真实卡片名单（DOM 探测失败），拒绝按旧下标盲试"}
    cands = [n for n in TEAM_PREFERRED if n in live] + [
        n for n in live if n not in TEAM_PREFERRED
    ]
    used_teams, titles = ledger_used_teams()
    before = len(cands)
    cands = [n for n in cands if n not in used_teams]
    say(
        "    账本：%d 条会话 → 已用团队 %s；候选 %d→%d 个"
        % (len(titles), sorted(used_teams) or "(无)", before, len(cands))
    )
    state = load_team_state().get(key, {})
    att = [a for a in (state.get("attempts") or []) if a.get("team") and a.get("sent")]
    fail = _team_fail_counts(att)
    tried = {a.get("team") for a in att if a.get("scored")} | {
        t for t, n in fail.items() if n >= 2
    }
    b2 = len(cands)
    cands = [n for n in cands if n not in tried]
    if tried:
        say(
            "    历史尝试账本：已试过 %s；候选 %d→%d 个"
            % (sorted(tried), b2, len(cands))
        )
    # 任何账号真实计过分的团队排最前（第一发必须打在刀刃上）
    proven = set()
    for kv in load_team_state().values():
        for a in kv.get("attempts") or []:
            if a.get("team") and a.get("scored"):
                proven.add(a["team"])
    cands.sort(key=lambda n: (0 if n in proven else 1,))
    if proven & set(cands):
        say("    [排序] 优先打已验证可计分的团队：%s" % sorted(proven & set(cands)))
    return cands, {
        "used": sorted(used_teams),
        "tried": sorted(tried),
        "live": live,
        "proven": sorted(proven),
    }
