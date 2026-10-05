#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端 UI 驱动核心（CDP）：run_task 流水线 + 输入/点击原语 + 护栏。

成长任务进度由服务端检测「真实产品使用事件」，纯 API 刷不动，必须驱动客户端本体。
本模块通过 CDP 在渲染进程里执行真实鼠标/键盘事件序列。

关键坑（全部实战验证，勿回退）：
1. 编辑器是 Slate 受控组件：改 innerText / dispatch input 无效，必须用 CDP
   Input.insertText 打字、Input.dispatchKeyEvent 发 Ctrl+A/Backspace 清空。
2. 只发模板标签不带指令时 AI 会反问挂「待确认」，对话不算完成 —— 必须补 prompt。
3. WorkBuddy 会把视图切回「活跃会话」——整条序列必须连贯跑完，中途不插别的操作。
4. 完成判定不能只看发送按钮 --stop（等用户回答时也不消失）。可靠信号：
   .cr-message-list-viewport 内 processing 消失 + 结构连续 N 拍不变。
5. 切号会整页 reload：Runtime.evaluate 报 "Execution context was destroyed"，
   js() 内置重连重试；动手前先 wait_page_ready。
6. 用户正在打字时驱动器必须让路（user_busy），openNewTask 会吃掉用户草稿。
"""

import json
import os
import re
import time

from core import cdp as C
from core import config as CFG

STOP_FILE = CFG.STOP_FILE


def stop_requested():
    return CFG.stop_requested()


# --------------------------------------------------------------------------- #
# 「用户正在输入」护栏
# --------------------------------------------------------------------------- #
USER_WATCH = r"""
(() => {
  // V2 变量名：老版本监听器一旦装上摘不掉，会污染判断；换名让老监听器写入失效。
  if (window.__wbWatchV !== 2) {
    window.__wbWatchV = 2;
    window.__wbUserKey2 = Date.now() - 60000;
    window.__wbDriverTyping = false;
    window.__wbDriverUntil = 0;
    const mark = (e) => {
      // 页面内合成事件（isTrusted=false）一律不算用户（驱动器自己发 Escape 关浮层用的就是这种）
      if (e && e.isTrusted === false) return;
      if (window.__wbDriverTyping) return;
      if (Date.now() < (window.__wbDriverUntil || 0)) return;
      window.__wbUserKey2 = Date.now();
    };
    document.addEventListener('keydown', mark, true);
    document.addEventListener('beforeinput', mark, true);
    document.addEventListener('paste', mark, true);
  }
  const ed = document.querySelector('[contenteditable=true]');
  return {
    idleSec: Math.round((Date.now() - window.__wbUserKey2) / 1000),
    edLen: ed ? (ed.innerText || '').trim().length : 0,
    focused: !!(ed && document.activeElement === ed),
    driverTyping: !!window.__wbDriverTyping,
    installed: true
  };
})()
"""

# 用户安静多久算「没在用」。15s 经验值：真人打字词间停顿 <5s；
# 绝不能设太大（45s 时驱动器自己的合成 Escape 误标记会让后续任务集体让路）。
USER_IDLE_SEC = 15


def user_busy(idle_sec=None):
    """用户是不是正在用客户端。判据只看：最近 idle_sec 秒内有没有真的敲过键。"""
    idle = USER_IDLE_SEC if idle_sec is None else idle_sec
    try:
        st = js(USER_WATCH, timeout=30)
    except Exception as e:
        return False, {"err": "%s: %s" % (type(e).__name__, e)}
    if not isinstance(st, dict):
        return False, {"err": "watch 返回异常"}
    i = st.get("idleSec")
    if i is not None and i < idle:
        return True, st
    return False, st


def driver_typing(on=True):
    """告诉页面「现在是驱动器在操作」，别把按键算到用户头上（含 3s 异步投递容忍窗）。"""
    if on:
        expr = (
            "(() => { window.__wbDriverTyping = true;"
            " window.__wbDriverUntil = Date.now() + 3000; return true; })()"
        )
    else:
        expr = "(() => { window.__wbDriverTyping = false; return true; })()"
    try:
        return js(expr, timeout=20)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 模型策略：完成任务一律用「积分消耗最少或免费」的模型；任务点名模型时才用点名模型
# --------------------------------------------------------------------------- #
def _norm_model(s):
    """normModel 的 Python 版（与 PRELUDE 里的 JS 实现同规则）。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def rank_models(items, avoid=None):
    """把选择器抓到的模型项按「免费 → 自定义(无费率) → 费率升序」排序（纯函数，可单测）。

    items: [{name, rate(float|None), free(bool), disabled(bool)}]
    规则：tier0=显式免费（含「免费」字样或费率 0.00x）；tier1=无费率的自定义模型
    （用户自建模型不显示费率，按免费对待，排在显式免费之后）；tier2=按费率升序。
    同名去重（按归一化名），剔除回避名单与禁用项。
    """
    avoid = {_norm_model(x) for x in (avoid if avoid is not None else AVOID_MODELS)}
    seen, ranked = set(), []
    for it in items or []:
        name = (it.get("name") or "").strip()
        if not name or it.get("disabled"):
            continue
        n = _norm_model(name) or name  # 中文名（快速/均衡/极致）归一化为空时用原名去重
        if not n or n in seen or n in avoid:
            continue
        seen.add(n)
        rate = it.get("rate")
        free = bool(it.get("free")) or rate == 0.0
        # 2026-09-26 修复: 免费优先 → 自定义(无费率) → 收费最低
        # （旧实现把自定义模型 rate=None 排最前，导致 agnes-3.0-flash 被优先选中、请求频繁报错）
        tier = 0 if free else (1 if rate is None else 2)
        ranked.append((tier, rate if rate is not None else 0.0, name))
    ranked.sort(key=lambda x: (x[0], x[1]))
    return [n for _, _, n in ranked]


_MODEL_CACHE = {"ts": 0.0, "items": None}
_MODEL_CACHE_TTL = 300.0


def list_models_live(ttl=_MODEL_CACHE_TTL):
    """瞬开模型选择器抓取全部模型项（只读，抓到就关，不改当前模型）。

    返回 [{name, rate, free, disabled}]；选择器不存在/抓取失败返回 None。
    带 TTL 缓存（默认 300s），避免每次选模型都开关一次下拉。
    """
    now = time.time()
    if _MODEL_CACHE["items"] is not None and now - _MODEL_CACHE["ts"] < ttl:
        return _MODEL_CACHE["items"]
    r = js(
        r"""
(async () => {
  const trig = document.querySelector('.cr-model-selector__trigger');
  if (!trig) return { ok: false, err: 'no trigger' };
  realClick(trig);
  await sleep(1200);
  const items = Array.from(document.querySelectorAll('.cr-model-selector__item')).map(it => {
    const nm = it.querySelector('.cr-model-selector__item-name');
    const full = (it.innerText || '').replace(/\s+/g, ' ').trim();
    const m = full.match(/(\d+(?:\.\d+)?)\s*x\s*$/i);
    return { name: nm ? (nm.innerText || '').trim() : (full.split(' ')[0] || ''),
             rate: m ? parseFloat(m[1]) : null,
             free: /免费/.test(full),
             disabled: /disabled/.test((it.className || '').toString()) };
  });
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await sleep(400);
  if (document.querySelector('.cr-model-selector__item')) { realClick(trig); await sleep(300); }
  return { ok: true, items: items };
})()
""",
        timeout=60,
    )
    if isinstance(r, dict) and r.get("ok") and r.get("items"):
        _MODEL_CACHE["items"] = r["items"]
        _MODEL_CACHE["ts"] = now
        return r["items"]
    return None


_TPL_CACHE = {"ts": 0.0, "names": None}


def list_templates_live(ttl=_MODEL_CACHE_TTL):
    """读输入框上方模板分类列表（WorkBuddy 5.6.2：
       遍历大分类 button.wb-scene-tabs__pill，收集每个下的子项 button.quick-actions__item）。
    只读不点；返回全部子项名列表；抓不到返回 None。带 TTL 缓存。
    """
    now = time.time()
    if _TPL_CACHE["names"] is not None and now - _TPL_CACHE["ts"] < ttl:
        return _TPL_CACHE["names"]
    r = js(
        r"""
(async () => {
  await openNewTask();
  await sleep(2500);
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const seen = [];
  const cats = Array.from(document.querySelectorAll('button.wb-scene-tabs__pill')).filter(vis);
  for (const cat of cats) {
    const cls = String(cat.className || '');
    if (!cls.includes('active')) {
      realClick(cat);
      await sleep(2500);
    } else {
      await sleep(800);
    }
    let items = Array.from(document.querySelectorAll('button.quick-actions__item')).filter(vis);
    if (!items.length) {
      items = Array.from(document.querySelectorAll('button.quick-actions-sub__item')).filter(vis);
    }
    for (const b of items) {
      const t = T(b);
      if (t && seen.indexOf(t) === -1) seen.push(t);
    }
  }
  return { ok: true, templates: seen };
})()
""",
        timeout=90,
    )
    if isinstance(r, dict) and r.get("ok") and r.get("templates"):
        _TPL_CACHE["names"] = r["templates"]
        _TPL_CACHE["ts"] = now
        return r["templates"]
    return None



# --------------------------------------------------------------------------- #
# 完整模板选择与发射流程（对齐最终流程图，所有点击走真实CDP，状态判断不写死时间）
# --------------------------------------------------------------------------- #
from core import config as _CFG_TPL

TPL_USAGE_PATH = os.path.join(_CFG_TPL.DATA_DIR, "template_usage.json")


def _tpl_load_usage():
    return _CFG_TPL.load_json(TPL_USAGE_PATH, {}) or {}


def _tpl_get_used(account_key):
    data = _tpl_load_usage()
    return set(data.get(account_key) or [])


def _tpl_mark_used(account_key, template_id):
    if not account_key or not template_id:
        return
    data = _tpl_load_usage()
    used = set(data.get(account_key) or [])
    used.add(str(template_id))
    data[account_key] = sorted(used)
    try:
        _CFG_TPL.save_json(TPL_USAGE_PATH, data)
    except Exception:
        pass



# --------------------------------------------------------------------------- #
# 专家使用账本
# --------------------------------------------------------------------------- #
EXPERT_USAGE_PATH = os.path.join(_CFG_TPL.DATA_DIR, "expert_usage.json")

def _expert_load_usage():
    return _CFG_TPL.load_json(EXPERT_USAGE_PATH, {}) or {}

def _expert_get_used(account_key):
    data = _expert_load_usage()
    return set(data.get(account_key) or [])

def _expert_mark_used(account_key, expert_name):
    if not account_key or not expert_name:
        return
    data = _expert_load_usage()
    used = set(data.get(account_key) or [])
    used.add(str(expert_name))
    data[account_key] = sorted(used)
    try:
        _CFG_TPL.save_json(EXPERT_USAGE_PATH, data)
    except Exception:
        pass

# 当前账号key全局标记（调度层设置）
_current_account_key = None

def _scene_chip_present():
    """首页输入框内是否挂着场景 chip（说明当前停在三级态）。"""
    return bool(js(r"""
(() => {
  const w = Array.from(document.querySelectorAll('span.phrase-content-wrapper'))
    .find(e => e.querySelector('[data-content-block-meta-type="scene"]'));
  if (w) return true;
  return !!document.querySelector('span[class*="_hasCloseIcon"][data-content-block-meta-type="scene"]');
})()
""", timeout=10))


_KEY_CODES = {
    #name: (key, code, windowsVirtualKeyCode)
    "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27),
    "enter": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46),
    "up": ("ArrowUp", "ArrowUp", 38),
    "down": ("ArrowDown", "ArrowDown", 40),
    "left": ("ArrowLeft", "ArrowLeft", 37),
    "right": ("ArrowRight", "ArrowRight", 39),
}


def hotkey(name, modifiers=0, timeout=10):
    """派发一次真实按键事件（修复：此前 hotkey() 未定义，调用被 except 静默吞掉，
    导致 reset_to_clean_home 的 Esc 关闭浮层从未真正生效）。

    与本模块既有键盘实现一致，走 CDP Input.dispatchKeyEvent。
    modifiers 位掩码：Alt=1 Ctrl=2 Meta=4 Shift=8。
    返回 {"ok": bool, "err": str}，不抛异常。
    """
    k = str(name or "").strip().lower()
    spec = _KEY_CODES.get(k)
    if not spec:
        return {"ok": False, "err": "不支持的按键：%s" % name}
    key, code, vk = spec
    try:
        cdp, _page = C.connect()
    except Exception as e:
        return {"ok": False, "err": "取会话失败：%s" % e}
    payload = {
        "modifiers": modifiers,
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
    }
    try:
        cdp.call("Input.dispatchKeyEvent", dict(payload, type="rawKeyDown"), timeout=timeout)
        cdp.call("Input.dispatchKeyEvent", dict(payload, type="keyUp"), timeout=timeout)
    except Exception as e:
        return {"ok": False, "err": "派发按键失败：%s" % e}
    return {"ok": True, "key": key}


def reset_to_clean_home(retries=3, log=None):
    """强力回到干净的「新建任务」首页：关弹窗 → 点新建任务 → 验证场景 tab 无遮挡。"""
    newtask_expr = (
        "Array.from(document.querySelectorAll('*')).find(e=>(e.innerText||'').trim()=='新建任务'"
        "&&e.getBoundingClientRect().top<160&&e.getBoundingClientRect().left<200)"
    )
    for i in range(retries):
        try:
            js("(function(){ return closeOverlays(); })()", timeout=8)
        except Exception:
            pass
        time.sleep(0.5)
        # 修复：原先此处 NameError 被 except 吞掉，Esc 从未发出。
        # 现在 hotkey 已实现，并保留失败日志便于排障。
        hk = hotkey("escape")
        if not hk.get("ok") and log:
            log("  [回首页] Esc 关闭浮层失败：%s" % hk.get("err"))
        mc = mouse_click(newtask_expr, timeout=12)
        if not mc.get("ok") and log:
            log("  [回首页] 点新建任务失败：%s" % mc.get("err"))
        time.sleep(3.0)
        try:
            ready = js("(function(){ return homeReady(); })()", timeout=8)
        except Exception:
            ready = None
        if isinstance(ready, dict) and ready.get("ok"):
            return {"ok": True}
        time.sleep(1.0)
    return {"ok": False, "err": "无法回到干净首页", "ready": ready}


def reset_scene_to_subitems(timeout=20):
    """三级态 → 二级列表：hover 场景 chip → 点其尾部关闭 svg → 等二级按钮恢复。
    点二级项后二级行会被三级行替换，不复位则后续 clickSubItem 全部找不到按钮。"""
    host = js(r"""
(() => {
  const w = Array.from(document.querySelectorAll('span.phrase-content-wrapper'))
    .find(e => e.querySelector('[data-content-block-meta-type="scene"]'));
  if (w) { w.setAttribute('data-scene-chip-host','1');
    return {ok:true, selector:"document.querySelector('[data-scene-chip-host=\"1\"]')"}; }
  const t = document.querySelector('span[class*="_hasCloseIcon"][data-content-block-meta-type="scene"]');
  if (t) { t.setAttribute('data-scene-chip-host','1');
    return {ok:true, selector:"document.querySelector('[data-scene-chip-host=\"1\"]')"}; }
  return {ok:true, already:true};
})()
""", timeout=10)
    if not (isinstance(host, dict) and host.get("ok")):
        return host if isinstance(host, dict) else {"ok": False, "err": "复位定位失败"}
    if host.get("already"):
        return {"ok": True, "already": True}
    hv = mouse_hover(host["selector"], timeout=15)
    if not (isinstance(hv, dict) and hv.get("ok")):
        return hv
    x = js(r"""
(() => {
  const host = document.querySelector('[data-scene-chip-host]') || document;
  const tag = host.querySelector('span[class*="_hasCloseIcon"]') || host;
  const svgs = Array.from(tag.querySelectorAll('svg'));
  let x = svgs.find(s => !s.closest('span[class*="_iconContainer"]')) || svgs[svgs.length-1];
  if (!x) return {ok:false, err:'no close svg'};
  x.setAttribute('data-scene-close-x','1');
  return {ok:true, selector:"document.querySelector('[data-scene-close-x=\"1\"]')"};
})()
""", timeout=10)
    if not (isinstance(x, dict) and x.get("ok")):
        return x
    cc = mouse_click(x["selector"], timeout=15)
    if not (isinstance(cc, dict) and cc.get("ok")):
        return cc
    state = None
    t0 = time.time()
    while time.time() - t0 < 8:
        state = js(r"""
(() => {
  const w = Array.from(document.querySelectorAll('span.phrase-content-wrapper'))
    .find(e => e.querySelector('[data-content-block-meta-type="scene"]'));
  return {chipGone: !w, items: document.querySelectorAll('button.quick-actions__item').length};
})()
""", timeout=8)
        if isinstance(state, dict) and state.get("chipGone") and state.get("items", 0) > 0:
            return {"ok": True}
        time.sleep(0.8)
    return {"ok": False, "err": "复位后二级列表未恢复", "state": state}


_TEMPLATE_POOL_CACHE = None


def collect_all_available_templates(timeout_per_subcat=15, force_refresh=False):
    """动态遍历所有一级分类→二级子分类，收集二级页面下的具体可用模板卡片。
    自动跳过无模板的二级分类（生图/生视频/品牌设计/代码开发下非网站设计等）。
    返回结构：[ {"cat":一级分类, "subcat":二级分类, "name":模板名, "type":"final", "id":唯一ID}, ... ]
    完全不写死分类，基于页面真实加载结果。

    模板目录在一次运行中是静态的、与账号无关（账号差异只在已用账本），
    所以全进程缓存：首次枚举后复用，避免每发一个模板就重枚举（约 212s/次）。
    需要强制刷新时传 force_refresh=True。
    """
    global _TEMPLATE_POOL_CACHE
    if not force_refresh and _TEMPLATE_POOL_CACHE is not None:
        return _TEMPLATE_POOL_CACHE
    # 健壮性：枚举前回到干净首页，并设整体熔断（防止页面状态差时渲染主线程卡死）
    try:
        reset_to_clean_home(retries=2)
    except Exception:
        pass
    _collect_deadline = time.time() + 260
    # 第一步：获取所有一级大分类
    cats_res = js("(async () => { return await getSceneCats(); })()", timeout=30)
    if not (isinstance(cats_res, dict) and cats_res.get("ok")):
        return [], "无法加载一级分类列表"
    cats = [c["name"] for c in cats_res.get("cats") or []]
    all_templates = []
    seen_ids = set()

    for cat_name in cats:
        if time.time() > _collect_deadline:
            break
        # 切换一级分类
        click_res = js(
            "(async (n) => { return await clickSceneCat(%s); })()" % json.dumps(cat_name),
            timeout=30,
        )
        if not (isinstance(click_res, dict) and click_res.get("ok")):
            continue
        if click_res.get("needClick"):
            click_rc = mouse_click(click_res["selector"], timeout=20)
            if not click_rc.get("ok"):
                continue
        # 等二级子项列表加载
        sub_loaded = False
        t0 = time.time()
        sub_data = None
        while time.time() - t0 < 15:
            sub_data = js("(async () => { return await collectCurrentSubItems(); })()", timeout=15)
            if isinstance(sub_data, dict) and sub_data.get("ok"):
                sub_loaded = True
                break
            time.sleep(1)
        if not sub_loaded or not sub_data:
            continue
        # 该分类可能记忆在三级态：先复位回二级列表，否则拿到的是三级项、clickSubItem 失败
        if _scene_chip_present():
            reset_scene_to_subitems()
            sub_data = js("(async () => { return await collectCurrentSubItems(); })()", timeout=15)
        subcats = (sub_data or {}).get("items") or []
        # 遍历每个二级子分类
        for subcat_name in subcats:
            if time.time() > _collect_deadline:
                break
            # 内容红线：媒体消耗型二级分类（生图/生视频/海报/插画等）整体跳过
            if media_blocked(cat_name, subcat_name):
                continue
            # 残留三级态先复位，保证二级按钮存在
            if _scene_chip_present():
                reset_scene_to_subitems()
            # 点击进入二级子分类
            sc_res = js(
                "(async (n) => { return await clickSubItem(%s); })()" % json.dumps(subcat_name),
                timeout=20,
            )
            if not (isinstance(sc_res, dict) and sc_res.get("ok")):
                continue
            if sc_res.get("needClick"):
                sc_click = mouse_click(sc_res["selector"], timeout=15)
                if not sc_click.get("ok"):
                    continue
            # 轮询等待二级页面具体模板加载
            tpl_loaded = False
            t1 = time.time()
            tpl_data = None
            while time.time() - t1 < timeout_per_subcat:
                tpl_data = js("(async () => { return await collectSubTemplates(); })()", timeout=15)
                if isinstance(tpl_data, dict) and tpl_data.get("ok"):
                    tpl_loaded = True
                    break
                time.sleep(1)
            if not tpl_loaded or not tpl_data:
                # 该二级分类下无模板：复位后再继续下一个
                if _scene_chip_present():
                    reset_scene_to_subitems()
                continue
            # 收集所有具体模板（过滤媒体消耗型）
            for tpl_name in tpl_data.get("templates") or []:
                if media_blocked(cat_name, subcat_name, tpl_name):
                    continue
                tpl_id = "%s::%s::%s" % (cat_name, subcat_name, tpl_name)
                if tpl_id not in seen_ids:
                    seen_ids.add(tpl_id)
                    all_templates.append({
                        "cat": cat_name,
                        "subcat": subcat_name,
                        "name": tpl_name,
                        "type": "final",
                        "id": tpl_id
                    })
            # 复位回二级列表再枚举下一个（三级行会替换二级行）
            if _scene_chip_present():
                reset_scene_to_subitems()

    # 排序：日常分类优先→网站/代码→设计创意
    def _sort_key(t):
        cat = t["cat"]
        if "日常" in cat:
            return 0
        if "网站" in cat or "代码" in cat:
            return 1
        if "设计创意" in cat:
            return 2
        return 3
    all_templates.sort(key=_sort_key)
    _TEMPLATE_POOL_CACHE = (all_templates, "")
    return _TEMPLATE_POOL_CACHE


def _click_and_wait(locate_js, step_name, timeout=20):
    """通用：定位元素→真实点击→等待动作完成，返回是否成功"""
    locate_res = js(locate_js, timeout=20)
    if not (isinstance(locate_res, dict) and locate_res.get("ok")):
        return False, locate_res
    click_rc = mouse_click(locate_res["selector"], timeout=timeout)
    if not click_rc.get("ok"):
        return False, click_rc
    return True, locate_res


def fire_template_once(account_key, template_info=None, timeout=60):
    """完整执行单个模板：切一级→进二级→选具体模板→做同款→替换→挂载校验→发送→验证发起→标记账本。
    发射成功即返回，不等AI回复，符合发射即走规则。
    """
    result = {"template": template_info}
    # 未指定模板则自动收集并选一个未用过的
    if not template_info:
        all_tpl, err = collect_all_available_templates()
        if err or not all_tpl:
            return {"ok": False, "err": "无可用模板：%s" % err}
        used = _tpl_get_used(account_key)
        available = [t for t in all_tpl if t["id"] not in used]
        if not available:
            return {"ok": False, "err": "该账号所有模板均已使用"}
        template_info = available[0]

    cat_name = template_info["cat"]
    subcat_name = template_info.get("subcat") or ""
    tpl_name = template_info["name"]
    tpl_id = template_info["id"]

    # 1. 切换到一级大分类
    click_res = js(
        "(async (n) => { return await clickSceneCat(%s); })()" % json.dumps(cat_name),
        timeout=20,
    )
    if not (isinstance(click_res, dict) and click_res.get("ok")):
        return {"ok": False, "err": "切换一级分类失败", "step": "cat"}
    if click_res.get("needClick"):
        rc = mouse_click(click_res["selector"], timeout=20)
        if not rc.get("ok"):
            return {"ok": False, "err": "点击一级分类失败", "step": "cat_click"}
    # 等二级子项列表加载
    t0 = time.time()
    sub_loaded = False
    while time.time() - t0 < 15:
        sub_data = js("(async () => { return await collectCurrentSubItems(); })()", timeout=15)
        if isinstance(sub_data, dict) and sub_data.get("ok"):
            sub_loaded = True
            break
        time.sleep(1)
    if not sub_loaded:
        return {"ok": False, "err": "一级分类内容加载失败", "step": "cat_load"}
    # 该分类可能记忆在三级态：先复位回二级列表，否则 clickSubItem 找不到二级按钮
    if _scene_chip_present():
        _rr = reset_scene_to_subitems()
        if not (isinstance(_rr, dict) and _rr.get("ok")):
            return {"ok": False, "err": "复位二级列表失败：%s" % _rr.get("err"), "step": "reset"}

    # 2. 点击进入二级子分类
    if subcat_name:
        sc_res = js(
            "(async (n) => { return await clickSubItem(%s); })()" % json.dumps(subcat_name),
            timeout=20,
        )
        if not (isinstance(sc_res, dict) and sc_res.get("ok")):
            return {"ok": False, "err": "切换二级分类失败", "step": "subcat"}
        if sc_res.get("needClick"):
            sc_rc = mouse_click(sc_res["selector"], timeout=15)
            if not sc_rc.get("ok"):
                return {"ok": False, "err": "点击二级分类失败", "step": "subcat_click"}
        # 等二级页面模板列表加载
        t1 = time.time()
        tpl_list_loaded = False
        while time.time() - t1 < 15:
            tpl_list = js("(async () => { return await collectSubTemplates(); })()", timeout=15)
            if isinstance(tpl_list, dict) and tpl_list.get("ok"):
                tpl_list_loaded = True
                break
            time.sleep(1)
        if not tpl_list_loaded:
            return {"ok": False, "err": "二级页面模板加载失败", "step": "subcat_load"}

    # 3. 选中具体模板卡片
    locate_js = "(async (n) => { return await locateTemplateCard(%s); })()" % json.dumps(tpl_name)
    ok, locate_res = _click_and_wait(locate_js, "select_template")
    if not ok:
        return {"ok": False, "err": "选中具体模板失败", "step": "select", "detail": locate_res}
    time.sleep(1)

    # 4. 等详情弹窗挂载，轮询点击「做同款」（弹窗/按钮异步出现，单次定位会抢跑）
    same_clicked = False
    _t = time.time()
    while time.time() - _t < 10:
        same_res = js("(async () => { return await locateDoSameBtn(); })()", timeout=10)
        if isinstance(same_res, dict) and same_res.get("ok"):
            _sc = mouse_click(same_res["selector"], timeout=10)
            if _sc.get("ok"):
                same_clicked = True
                break
        time.sleep(0.8)
    if not same_clicked:
        # 部分模板点击后直接填充提示词、无详情弹窗/做同款按钮，属于正常情况，放行
        pass
    # 做同款后可能弹「使用提醒/免责」，自动勾选继续；团队「积分消耗提醒」自动取消
    try:
        js("(async () => { return await autoDismissDialog(); })()", timeout=10)
    except Exception:
        pass

    # 4. 处理替换弹窗
    replace_tries = 0
    replaced = False
    while replace_tries < 5:
        try:
            js("(async () => { return await autoDismissDialog(); })()", timeout=10)
        except Exception:
            pass
        replace_res = js("(async () => { return await locateReplaceBtn(); })()", timeout=10)
        if isinstance(replace_res, dict) and replace_res.get("ok"):
            rc = mouse_click(replace_res["selector"], timeout=10)
            if rc.get("ok"):
                replaced = True
                break
        replace_tries += 1
        time.sleep(1)
    # 等待提示词填充完成
    input_ready = False
    t0 = time.time()
    while time.time() - t0 < 15:
        try:
            js("(async () => { return await autoDismissDialog(); })()", timeout=10)
        except Exception:
            pass
        ed_check = js(r"""
(() => {
  const ed = document.querySelector('[contenteditable=true]');
  const t = ed ? txt(ed) : '';
  return {ok: ed && t.length > 10, len: t.length};
})()
""", timeout=10)
        if isinstance(ed_check, dict) and ed_check.get("ok"):
            input_ready = True
            break
        time.sleep(1)
    if not input_ready:
        return {"ok": False, "err": "模板提示词未填充", "step": "fill_prompt"}

    # 5.1 在模板提示词末尾追加「自主交付约束」（真机定位：思考快与慢等模板会触发
    #     “确认生成图片（每张5-10积分）”弹窗，专家反问/索要确认会让任务卡在「待确认」
    #     无法交付、服务端不计分）。追加后专家改用内联SVG/CSS，全程不提问、不烧媒体积分。
    AUTONOMOUS_SUFFIX = (
        "　【执行要求】请直接自主完成以上任务：不要向我提问、不要要求我确认任何方案或资料；"
        "不要生成图片或视频、不要调用任何图片/视频生成工具，所有配图一律用内联SVG或纯CSS色块实现；"
        "直接产出最终成品。"
    )
    try:
        ap = js(
            "(()=>{const ed=document.querySelector('[contenteditable=true]');"
            "ed.focus();"
            "const sel=window.getSelection();const rg=document.createRange();"
            "rg.selectNodeContents(ed);rg.collapse(false);sel.removeAllRanges();sel.addRange(rg);"
            "document.execCommand('insertText',false,%s);"
            "return {ok:true,len:txt(ed).length};})()"
            % json.dumps(AUTONOMOUS_SUFFIX),
            timeout=12,
        )
        if not (isinstance(ap, dict) and ap.get("len", 0) > 20):
            return {"ok": False, "err": "自主约束追加未生效", "step": "append_suffix"}
    except Exception as e:
        return {"ok": False, "err": "追加自主约束失败：%s" % str(e)[:60], "step": "append_suffix"}

    # 5.5 发送前强制切到免费模型（2026-09-26 修复：旧实现不选模型，直接沿用
    #    客户端当前的自定义模型（如 agnes-3.0-flash），其网络不稳定频繁报
    #    UNKNOWN_STOP_REASON，导致发送失败；用户明确「只选免费模型，不用自定义模型」）
    _m_cands = list(cheap_models())
    _m_used = None
    if _m_cands:
        _limited = {}
        for _m_name in _m_cands:
            # 持久化冷却中（此前确证超限、未到重置时间）→ 直接跳过，不重复踩雷
            _cd = _rl_cooldown(_m_name)
            if _cd:
                _limited[_m_name] = _cd
                continue
            _m_res = js(
                "(async () => { const m = await pickModel(%s); return m; })()"
                % json.dumps(_m_name),
                timeout=60,
            )
            if not (isinstance(_m_res, dict) and _m_res.get("ok")):
                continue
            time.sleep(1.2)
            _rl = js("(function(){ return readRateLimit(); })()", timeout=10)
            # 横幅明确提示当前模型超限 → 记录并跳过，尝试下一个免费模型
            if isinstance(_rl, dict) and _rl.get("model") and (
                _rl["model"] in _m_name or _m_name in _rl["model"]
            ):
                import datetime as _dexp
                _banner_expired = False
                try:
                    _rts = _dexp.datetime.strptime(
                        str(_rl.get("reset")).strip(), "%Y-%m-%d %H:%M:%S")
                    _banner_expired = _rts <= _dexp.datetime.now()
                except Exception:
                    _banner_expired = False
                if _banner_expired:
                    # 横幅重置点已过 = 模型已恢复，直接采用，不再跳过
                    _m_used = _m_name
                    break
                _rl_record(_rl["model"], _rl.get("reset"))
                _limited[_m_name] = _rl.get("reset")
                continue
            _m_used = _m_name
            break
        if not _m_used:
            # 免费模型均频率限制：不发送、不消耗，返回最早重置时间（到点即可用该模型重试）
            _wait = min(_limited.values()) if _limited else None
            return {"ok": False, "err": "免费模型均超出频率限制，已中止发送",
                    "step": "rate_limit", "limited": _limited, "wait_until": _wait}
        # 切换模型后确认模板提示词仍挂载，防止切模型清空输入
        _recheck = js(r'''
(() => {
  const ed = document.querySelector('[contenteditable=true]');
  const t = ed ? txt(ed) : '';
  return {ok: ed && t.length > 10, len: t.length};
})()
''', timeout=10)
        if not (isinstance(_recheck, dict) and _recheck.get("ok")):
            return {"ok": False, "err": "切模型后模板提示词丢失", "step": "model_switch", "detail": _recheck}
        result["model"] = _m_used

    # 5. 滚动输入框到底，校验参考文件/模板挂载
    mount_ok = False
    for scroll_try in range(3):
        js("(async () => { return await scrollInputToBottom(); })()", timeout=10)
        ref_check = js("(async () => { return await checkReferenceFile(); })()", timeout=10)
        if isinstance(ref_check, dict) and ref_check.get("ok"):
            mount_ok = True
            break
        time.sleep(1)
    if not mount_ok:
        # 模板未真正挂载，返回失败，不发送
        return {"ok": False, "err": "模板未挂载（无参考文件）", "step": "mount_check"}

    # 6. 标记并真实点击发送
    mark_send = js("(function() { return markSendBtn(); })()", timeout=10)
    if not (isinstance(mark_send, dict) and mark_send.get("ok")):
        return {"ok": False, "err": "发送按钮不可用", "step": "mark_send"}
    send_rc = mouse_click(mark_send["selector"], timeout=10)
    if not send_rc.get("ok"):
        return {"ok": False, "err": "点击发送失败", "step": "send_click"}

    # 7. 验证会话真的发起、任务开始执行（轮询观察会话真实状态）
    #    2026-09-26 修复：出现错误文案（UNKNOWN_STOP_REASON/额度/网络等）立即判失败，
    #    返回给上层重新发起，绝不把失败发射标记为成功。
    started = False
    t0 = time.time()
    start_check = None
    saw_start = False
    while time.time() - t0 < 40:
        start_check = js("(async () => { return await verifyConversationStarted(); })()", timeout=10)
        if isinstance(start_check, dict):
            if start_check.get("error"):
                try:
                    _evl = js("(function(){ return readRateLimit(); })()", timeout=8)
                    if isinstance(_evl, dict) and _evl.get("model"):
                        _rl_record(_evl["model"], _evl.get("reset"))
                except Exception:
                    pass
                return {
                    "ok": False,
                    "err": "会话发起失败（%s）" % start_check["error"],
                    "step": "verify_start",
                    "detail": start_check,
                }
            if start_check.get("started"):
                saw_start = True
        # 任务已开始且稳定执行满 20 秒无错误，才确认真实发起（捕获延迟出现的频率错误）
        if saw_start and (time.time() - t0) >= 20:
            started = True
            break
        time.sleep(1.5)
    if not started:
        if saw_start:
            started = True
        else:
            return {"ok": False, "err": "会话未发起", "step": "verify_start", "detail": start_check}

    # 8. 标记模板已使用
    _tpl_mark_used(account_key, tpl_id)
    result.update({
        "ok": True,
        "started": True,
        "replaced": replaced,
        "template_id": tpl_id,
        "template_name": tpl_name,
        "category": cat_name
    })
    return result


def _rl_path():
    return os.path.join("data", "rate_limits.json")


def _rl_load():
    try:
        with open(_rl_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _rl_save(d):
    try:
        os.makedirs("data", exist_ok=True)
        with open(_rl_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _rl_record(model, reset_str):
    import datetime as _dt
    try:
        ts = _dt.datetime.strptime(str(reset_str).strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return
    d = _rl_load()
    old = d.get(model)
    if old is None or ts > float(old):
        d[model] = ts
        _rl_save(d)


def _rl_cooldown(model, now=None):
    now = time.time() if now is None else now
    for m, ts in _rl_load().items():
        if m == model or m in model or model in m:
            try:
                if now < float(ts):
                    import datetime as _dt
                    return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
    return None


def cheap_models():
    """省量模型回退链（按 免费 → 最少用量 排序），不写死：

    优先级：config.json 的 cheap_models 显式列表 > 动态抓取客户端模型列表按费率排序
    > 环境变量 WB_CUSTOM_MODELS（逗号分隔）/ WB_CUSTOM_MODEL > 内置通用默认。
    动态抓取失败（选择器不在/页面未就绪）自动落到静态链，不阻塞任务。
    """
    try:
        from core import config as _C

        lst = (_C.load_config() or {}).get("cheap_models")
        if isinstance(lst, list) and lst:
            return [str(x) for x in lst if x]
    except Exception:
        pass
    try:
        live = list_models_live()
        avoid = None
        try:
            from core import config as _C2

            cfg_avoid = (_C2.load_config() or {}).get("avoid_models")
            if isinstance(cfg_avoid, list) and cfg_avoid:
                # 叠加（不是替换）内置回避名单：config 只增不减
                avoid = list(AVOID_MODELS) + [
                    str(x)
                    for x in cfg_avoid
                    if x
                    and _norm_model(str(x))
                    not in {_norm_model(a) for a in AVOID_MODELS}
                ]
        except Exception:
            pass
        ranked = rank_models(live, avoid=avoid)
        if ranked:
            return ranked
    except Exception:
        pass
    env_list = os.environ.get("WB_CUSTOM_MODELS", "")
    if env_list.strip():
        return [x.strip() for x in env_list.split(",") if x.strip()]
    env_one = os.environ.get("WB_CUSTOM_MODEL", "")
    if env_one.strip():
        return [env_one.strip()]
    # 通用兜底：不给任何静态模型名 —— 返回空，run_task 将沿用当前模型（对所有人都成立）
    return []


TASK_MODEL_RULES = {
    "Model_chat_GLM5.2": "GLM-5.2",
    "black_cat": "GLM-5.2",
}
# 拉黑模型（2026-09-26 清理：历史黑名单来自已删除的污染 README，作废；
#   免费模型 Hy4 preview / Hy3 必须可用，不再拉黑）
AVOID_MODELS = []

# 模型显示名 -> localStorage 里的模型 id（可选加速映射；未收录的名字按归一化规则动态推导，
# 见 set_model_via_storage 的 or 分支 —— 映射不存在不影响功能）
MODEL_NAME_TO_ID = {
    "GLM-5.2": "glm-5.2",
    "Hy3": "hy3",
    "Deepseek-V4.1-Flash": "deepseek-v4.1-flash",
}

# 首页模板（产品级通用兜底：客户端输入框上方那排分类按钮；
# 正常路径走 list_templates_live() 动态枚举，抓不到才用这份）
#
# ⚠️ 内容红线（用户 2026-09-23 明确）：会话任务里**绝不允许出现生图/生视频/音频类
# 产出** —— 那不是"免费"，图/视频一多就扣模型本身积分。只允许：文本、代码、
# （少量）免费图。所以模板池与兜底池一律是纯文本/数据/代码向，
# 「视频生成」「绘图/海报/封面」等模板一律不进池（见 MEDIA_BLOCK_KEYWORDS）。
# 5.6.2 实测输入框上方 8 分类：文档处理/金融服务/数据分析及可视化/个人工作台/
# 幻灯片/深度研究/视频生成/产品管理 —— 「视频生成」是媒体向，兜底池排除。
TEMPLATE_NAMES = [
    "文档处理",
    "金融服务",
    "数据分析及可视化",
    "个人工作台",
    "幻灯片",
    "深度研究",
    "产品管理",
]

TEMPLATE_PROMPTS = {
    "文档处理": "把这句话整理成一句话：今天开会讨论了排期、人手和预算。直接给结果，不用解释。",
    "金融服务": "用一句话说明什么是复利。只给答案。",
    "数据分析及可视化": "数字 1,2,3,4,5 的平均值是多少？一句话回答。",
    "幻灯片": "只要一页：用一句话介绍人工智能。不用生成文件，不要配图，直接给要点文字。",
    "深度研究": "用两句话说明什么是碳中和。直接给结论，不要展开调研。",
    "产品管理": "用一句话说明 MVP 的核心思想。",
}

# --------------------------------------------------------------------------- #
# 内容风控：任何「消耗型产出」指令一律拦截（生图 / 生视频 / 生音频 / 数字人 …）
# 只对**文本与代码**放行。命中即换成安全提示词，并记录到 out["content_guard"]。
# --------------------------------------------------------------------------- #
MEDIA_BLOCK_KEYWORDS = [
    "视频生成",
    "生视频",
    "图生视频",
    "文生视频",
    "文生图",
    "生图",
    "生成图片",
    "图片生成",
    "绘图",
    "作图",
    "画图",
    "画一张",
    "海报",
    "封面",
    "插画",
    "漫画",
    "写真",
    "数字人",
    "配音",
    "音频生成",
    "生成音乐",
    "音乐生成",
    "渲染一张",
    "以图生图",
    "图生图",
    "AI绘画",
    "AI 绘画",
    "绘画",
]
# 出现消耗型意图时的替代指令：纯文本、不产生任何媒体文件
SAFE_TEXT_PROMPT = "用一句话回答：1+1 等于几？只给文字答案，不要生成图片、视频或音频。"


def media_blocked(*texts):
    """命中消耗型（生图/生视频/生音频）关键词则返回命中词列表，否则返回 []。"""
    blob = " ".join(str(t or "") for t in texts)
    return [k for k in MEDIA_BLOCK_KEYWORDS if k in blob]


def guard_text_only(prompt, template=None):
    """把「会烧积分的产出指令」换成纯文本指令。返回 (prompt, hits)。"""
    hits = media_blocked(prompt, template)
    if hits:
        return SAFE_TEXT_PROMPT, hits
    return prompt, []


def pick_model_for(task_code=None):
    """任务点名的模型返回点名模型；其余返回 None（交给 run_task 按 cheap_models() 自动回退）。"""
    if task_code and task_code in TASK_MODEL_RULES:
        return TASK_MODEL_RULES[task_code]
    return None


def model_label(model):
    if model:
        return model
    chain = cheap_models()
    return "自动最省(%s)" % (" → ".join(chain) if chain else "当前模型")


# --------------------------------------------------------------------------- #
# 注入页面的公共 JS 前置
# --------------------------------------------------------------------------- #
PRELUDE = r"""
/* 同一页面会被反复注入：一律用 var / function 声明（const/let 重复声明会抛错）。 */
var sleep = ms => new Promise(r => setTimeout(r, ms));
var txt = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
function normModel(s) {
  return (s || '').toString().toLowerCase().replace(/[^a-z0-9]/g, '');
}
function realClick(el) {
  const r = el.getBoundingClientRect();
  const opt = { bubbles: true, cancelable: true, composed: true,
                clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
                view: window, button: 0 };
  el.dispatchEvent(new PointerEvent('pointerdown', opt));
  el.dispatchEvent(new MouseEvent('mousedown', opt));
  el.dispatchEvent(new PointerEvent('pointerup', opt));
  el.dispatchEvent(new MouseEvent('mouseup', opt));
  el.dispatchEvent(new MouseEvent('click', opt));
}
function hover(el) {
  const r = el.getBoundingClientRect();
  const opt = { bubbles: true, cancelable: true, composed: true,
                clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, view: window };
  ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'mousemove'].forEach(t =>
    el.dispatchEvent(t.startsWith('pointer') ? new PointerEvent(t, opt) : new MouseEvent(t, opt)));
}
var exact = (sel, t) => Array.from(document.querySelectorAll(sel)).find(el => txt(el) === t);
var has = (sel, t) => Array.from(document.querySelectorAll(sel)).find(el => txt(el).includes(t));
function deepest(sel, t) {
  const cands = Array.from(document.querySelectorAll(sel)).filter(el => txt(el) === t);
  return cands.length ? cands[cands.length - 1] : null;
}
async function openNewTask() {
  const nb = exact('button', '新建任务');
  if (nb) { realClick(nb); await sleep(2500); return true; }
  return !!document.querySelector('.wb-home-composer__chips');
}
async function pickModel(name) {
  const trig = document.querySelector('.cr-model-selector__trigger');
  if (!trig) return { ok: false, err: '找不到模型选择器' };
  const want = (name || '').toString();
  const wantNorm = normModel(want);
  const trigTxt = txt(trig);
  if (trigTxt.indexOf(want) !== -1 || normModel(trigTxt) === wantNorm)
    return { ok: true, already: true };
  realClick(trig);
  await sleep(1500);
  for (const it of document.querySelectorAll('.cr-model-selector__item')) {
    const nm = it.querySelector('.cr-model-selector__item-name');
    if (!nm) continue;
    const t = txt(nm);
    const hit = (t === want)
              || (normModel(t) === wantNorm)
              || (t.toLowerCase().indexOf(want.toLowerCase()) !== -1);
    if (hit) {
      realClick(it);
      await sleep(1500);
      const after = txt(document.querySelector('.cr-model-selector__trigger'));
      return { ok: after.indexOf(want) !== -1 || normModel(after) === wantNorm, after };
    }
  }
  return { ok: false, err: '模型列表里没有 ' + name };
}
// 模板选择辅助JS：所有元素仅做定位/标记，真实点击由Python侧mouse_click执行，分步状态判断
async function getSceneCats() {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = txt;
  await openNewTask();
  const cats = Array.from(document.querySelectorAll('button.wb-scene-tabs__pill')).filter(vis);
  return {
    ok: !!cats.length,
    cats: cats.map(b => ({name: T(b), active: (b.className||'').includes('active')}))
  };
}
async function clickSceneCat(name) {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = txt;
  const cats = Array.from(document.querySelectorAll('button.wb-scene-tabs__pill')).filter(vis);
  const cat = cats.find(b => T(b) === name || T(b).indexOf(name) !== -1);
  if (!cat) return {ok:false, err:'未找到分类「'+name+'」', available: cats.map(T)};
  if ((cat.className||'').includes('active')) return {ok:true, already:true, name:T(cat)};
  Array.from(document.querySelectorAll('[data-tpl-target]')).forEach(el => el.removeAttribute('data-tpl-target'));
  cat.setAttribute('data-tpl-target', 'cat');
  return {ok:true, needClick:true, selector:"document.querySelector('[data-tpl-target=\"cat\"]')", name:T(cat)};
}
async function collectCurrentSubItems() {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = txt;
  let items = Array.from(document.querySelectorAll('button.quick-actions__item')).filter(vis);
  if (!items.length) items = Array.from(document.querySelectorAll('button.quick-actions-sub__item')).filter(vis);
  // 同时收集模板卡片（部分分类直接显示模板卡）
  let cards = Array.from(document.querySelectorAll('button.wb-related-playbooks__card, div.wb-related-playbooks__card')).filter(vis);
  return {
    ok: !!(items.length || cards.length),
    items: items.map(b => T(b)),
    cards: cards.map(c => T(c).slice(0,60))
  };
}
// 点击二级子分类按钮（如日常办公下的「文档处理」）
async function clickSubItem(name) {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = txt;
  let qa = Array.from(document.querySelectorAll('button.quick-actions__item')).filter(vis);
  if (!qa.length) qa = Array.from(document.querySelectorAll('button.quick-actions-sub__item')).filter(vis);
  const btn = qa.find(b => T(b) === name || T(b).indexOf(name) !== -1);
  if (!btn) return {ok:false, err:'未找到二级子项「'+name+'」'};
  // 已选中状态直接返回
  if ((btn.className||'').includes('active')) return {ok:true, already:true, name:T(btn)};
  Array.from(document.querySelectorAll('[data-tpl-target]')).forEach(el => el.removeAttribute('data-tpl-target'));
  btn.setAttribute('data-tpl-target', 'subcat');
  return {ok:true, needClick:true, selector:"document.querySelector('[data-tpl-target=\"subcat\"]')", name:T(btn)};
}
// 收集当前二级分类页面下的所有具体模板卡片
async function collectSubTemplates() {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 80 && r.height > 80; };
  const T = txt;
  // 模板卡片选择器（覆盖二级页面模板列表）
  let cards = Array.from(document.querySelectorAll(
    '.wb-related-playbooks__card, .dc-template-card, .quick-template-card, [class*="template-card"]'
  )).filter(vis);
  // 去重，返回模板名称
  var names = [];
  var seen = new Set();
  for (var i=0; i<cards.length; i++) {
    var c = cards[i];
    var name = T(c).slice(0, 80);
    if (name && !seen.has(name)) {
      seen.add(name);
      names.push(name);
    }
  }
  return {
    ok: names.length > 0,
    templates: names,
    count: names.length
  };
}
async function locateSubItem(name) {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = txt;
  let qa = Array.from(document.querySelectorAll('button.quick-actions__item')).filter(vis);
  if (!qa.length) qa = Array.from(document.querySelectorAll('button.quick-actions-sub__item')).filter(vis);
  const btn = qa.find(b => T(b) === name || T(b).indexOf(name) !== -1);
  if (!btn) return {ok:false, err:'未找到子项「'+name+'」'};
  Array.from(document.querySelectorAll('[data-tpl-target]')).forEach(el => el.removeAttribute('data-tpl-target'));
  btn.setAttribute('data-tpl-target', 'sub');
  return {ok:true, selector:"document.querySelector('[data-tpl-target=\"sub\"]')", name:T(btn)};
}
async function locateTemplateCard(name) {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = txt;
  let cards = Array.from(document.querySelectorAll('button.wb-related-playbooks__card, div.wb-related-playbooks__card')).filter(vis);
  const card = cards.find(c => name ? T(c).indexOf(name) !== -1 : true) || cards[0];
  if (!card) return {ok:false, err:'未找到模板卡片'};
  Array.from(document.querySelectorAll('[data-tpl-target]')).forEach(el => el.removeAttribute('data-tpl-target'));
  card.setAttribute('data-tpl-target', 'card');
  return {ok:true, selector:"document.querySelector('[data-tpl-target=\"card\"]')", name:T(card).slice(0,60)};
}
async function locateDoSameBtn() {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = txt;
  const btns = Array.from(document.querySelectorAll('button')).filter(vis);
  // 优先带+号的做同款按钮
  let btn = btns.find(b => T(b).indexOf('做同款') !== -1 && T(b).indexOf('+') !== -1);
  if (!btn) btn = btns.find(b => T(b) === '做同款' || /做同款/.test(T(b)));
  if (!btn) return {ok:false, err:'未找到「做同款」按钮'};
  Array.from(document.querySelectorAll('[data-tpl-target]')).forEach(el => el.removeAttribute('data-tpl-target'));
  btn.setAttribute('data-tpl-target', 'same');
  return {ok:true, selector:"document.querySelector('[data-tpl-target=\"same\"]')", text:T(btn)};
}
async function locateReplaceBtn() {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; };
  const T = txt;
  // 优先弹窗内的替换按钮
  const dlg = document.querySelector('.dc-confirm-dialog');
  let btn = dlg ? Array.from(dlg.querySelectorAll('button')).find(b => T(b) === '替换') : null;
  if (!btn) btn = Array.from(document.querySelectorAll('button')).filter(vis).find(b => /^替换$|确认替换/.test(T(b)));
  if (!btn) return {ok:false, err:'未找到「替换」按钮', noDialog: !dlg};
  Array.from(document.querySelectorAll('[data-tpl-target]')).forEach(el => el.removeAttribute('data-tpl-target'));
  btn.setAttribute('data-tpl-target', 'replace');
  return {ok:true, selector:"document.querySelector('[data-tpl-target=\"replace\"]')", text:T(btn)};
}
async function clearTplTargets() {
  Array.from(document.querySelectorAll('[data-tpl-target]')).forEach(el => el.removeAttribute('data-tpl-target'));
  return true;
}
async function scrollInputToBottom() {
  const ed = document.querySelector('[contenteditable=true]');
  if (!ed) return {ok:false, err:'找不到输入框'};
  // 聚焦输入框，滚动内部到底
  ed.focus();
  const inputBox = ed.closest('.cr-input-box, .wb-home-route__input-box') || ed.parentElement;
  if (inputBox) inputBox.scrollTop = inputBox.scrollHeight;
  // 移动光标到内容末尾
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(ed);
  range.collapse(false);
  sel.removeAllRanges();
  sel.addRange(range);
  return {ok:true, inputLen: txt(ed).length};
}
async function autoDismissDialog() {
  // 自动处理两类阻断弹窗：
  //  1) 使用提醒/免责声明（金融、财富类专家）：勾选「我已知悉并确认」→ 继续使用
  //  2) 积分消耗提醒（专家团/多专家协同，数倍消耗）：取消，避免高额消耗
  const dlgs = Array.from(document.querySelectorAll('.ec-confirm-dialog, .dc-confirm-dialog'));
  const vis = dlgs.filter(d => { const r = d.getBoundingClientRect(); return r.width > 60 && r.height > 60; });
  const acted = [];
  for (const dlg of vis) {
    const t = dlg.innerText || '';
    if (/积分消耗提醒|多位专家协同|消耗通常为单个专家的数倍/.test(t)) {
      const c = Array.from(dlg.querySelectorAll('button')).find(b => /取消/.test(b.innerText || ''));
      if (c) { c.click(); acted.push('cancel_team'); }
      continue;
    }
    if (/使用提醒|仅供参考|无法替代专业|我已知悉/.test(t)) {
      const cbs = Array.from(dlg.querySelectorAll('.ec-confirm-dialog-checkbox input, input[type=checkbox]'));
      let cn = 0;
      cbs.forEach(c => { if (!c.checked && cn < 1) { c.click(); cn++; } });
      const b = Array.from(dlg.querySelectorAll('button')).find(x => /继续使用|^确认$|允许|继续/.test((x.innerText || '').trim()));
      if (b) { b.click(); acted.push('accept_usage'); }
    }
  }
  return { acted };
}
function readRateLimit() {
  // 解析免费模型频率限制横幅：返回 {model, reset}
  const el = document.querySelector('.credits-insufficient-bar-head');
  if (!el) return null;
  const t = (el.innerText || '').replace(/\s+/g, ' ');
  const m = t.match(/当前您在(.+?)模型的免费使用量已超出频率限制，可在(.+?)重置/);
  if (!m) return null;
  const rt = new Date(m[2].trim().replace(/-/g,'/'));
  // 已过重置点 = 额度已恢复，横幅是页面残留 → 视为无限制（否则会把可用模型误判超限）
  if (!isNaN(rt) && rt.getTime() <= Date.now()) return null;
  return {model: m[1].trim(), reset: m[2].trim()};
}
function closeOverlays() {
  // 关闭所有遮挡弹窗：优先取消/关闭/拒绝（避免误接受积分消耗）
  document.querySelectorAll('.ec-confirm-dialog,.dc-confirm-dialog,[class*="modal"],[class*="dialog"]').forEach(d=>{
    const c = Array.from(d.querySelectorAll('button')).find(b=>/取消|关闭|拒绝/.test(b.innerText||''));
    if (c) c.click();
  });
  const dc = document.querySelector('button.dc-detail-round-btn[aria-label="Close"]');
  if (dc) dc.click();
  return 1;
}
function homeReady() {
  // 验证已在干净新建任务首页：场景 tab 可见、点击中心无遮挡、无弹窗
  const pill = document.querySelector('button.wb-scene-tabs__pill');
  if (!pill) return {ok:false, why:'no_pill'};
  const r = pill.getBoundingClientRect();
  const el = document.elementFromPoint(r.x+r.width/2, r.y+r.height/2);
  const hit = pill === el || pill.contains(el);
  const blocked = document.querySelector('.ec-confirm-dialog,.dc-confirm-dialog');
  return {ok: hit && !blocked && r.width > 0, why: blocked?'blocked':(hit?'':'covered')};
}
function deliveryState() {
  // 模板交付状态：processing=仍在执行；err=出现失败文案（用于区分真交付与错误中断）
  const p = document.querySelector('.cr-agent--processing')
    || document.querySelector('[class*="send-button--stop"]');
  const b = txt(document.body);
  const ERR = ['发生未知错误','超出频率限制','UNKNOWN_STOP','请求以意外状态完成',
    '请求失败','额度已用尽','免费额度已耗尽','网络异常','连接已断开'];
  const err = ERR.find(x => b.indexOf(x) !== -1) || '';
  return { processing: !!p, err: err };
}
async function checkReferenceFile() {
  const T = txt;
  // 查找参考文件/模板引用区域
  const allEls = Array.from(document.querySelectorAll('*')).filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 50 && r.height > 10;
  });
  const refArea = allEls.find(el => /参考文件|模板引用|附件|参考资料/.test(T(el)));
  let refText = refArea ? T(refArea).slice(0,200) : '';
  // 检查chip/模板标记
  const chips = Array.from(document.querySelectorAll('.phrase-content-wrapper, [class*="chip"]')).map(T).filter(t=>t);
  return {
    ok: !!(refText || chips.length),
    hasRefArea: !!refArea,
    refText,
    chips,
    inputLen: T(document.querySelector('[contenteditable=true]')||{innerText:''}).length
  };
}
function markSendBtn() {
  const b = document.querySelector('button.cr-send-button');
  if (!b || b.disabled) return {ok:false, err:'发送按钮不可用'};
  Array.from(document.querySelectorAll('[data-tpl-target]')).forEach(el => el.removeAttribute('data-tpl-target'));
  b.setAttribute('data-tpl-target', 'send');
  return {ok:true, selector:"document.querySelector('[data-tpl-target=\"send\"]')"};
}
async function verifyConversationStarted() {
  // 判断是否进入会话页、出现停止按钮（任务正在执行）
  // 2026-09-26 修复：旧判据「消息区有内容=已发起」会把请求失败误判成成功
  // （自定义模型 agnes-3.0-flash 发 UNKNOWN_STOP_REASON 也出现在消息区），
  // 导致服务端无事件、任务死卡。现轮询检查会话真实状态：出现错误文案→未发起。
  const stopBtn = document.querySelector('[class*="send-button--stop"]');
  const msgList = document.querySelector('.cr-message-list-viewport');
  const ed = document.querySelector('[contenteditable=true]');
  const body = txt(document.body);
  const ERROR_PATS = [
    'UNKNOWN_STOP_REASON', '请求以意外状态完成', '发生未知错误', '请求失败',
    '超出频率限制', '频率限制', '额度不足', '额度已用尽', '免费额度已耗尽',
    'quota', 'rate limit', '网络异常', '网络错误', '请检查网络', '连接已断开'
  ];
  const err = ERROR_PATS.find(p => body.toLowerCase().indexOf(p.toLowerCase()) !== -1) || '';
  const hasMsg = !!(stopBtn || (msgList && msgList.children.length > 0));
  return {
    started: hasMsg && !err,
    error: err,
    isChat: !!msgList,
    inputCleared: ed ? txt(ed).length < 20 : false
  };
}

async function waitDone(maxSec) {
  const t0 = Date.now();
  let started = false;
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    if (document.querySelector('[class*=send-button--stop]')) { started = true; break; }
  }
  if (!started) return { started: false, secs: Math.round((Date.now() - t0) / 1000) };
  for (let i = 0; i < maxSec * 2; i++) {
    await sleep(500);
    if (!document.querySelector('[class*=send-button--stop]')) {
      return { started: true, settled: true, secs: Math.round((Date.now() - t0) / 1000) };
    }
  }
  return { started: true, settled: false, secs: maxSec };
}
var convoCount = () => document.querySelectorAll('.conversation-item').length;
function pageState() {
  const body = txt(document.body);
  return {
    model: txt(document.querySelector('.cr-model-selector__trigger') || { innerText: '' }),
    sending: !!document.querySelector('[class*=send-button--stop]'),
    unknownError: body.includes('发生未知错误'),
    rateLimited: body.includes('超出频率限制'),
    taskCount: (body.match(/任务 \((\d+)\)/) || [])[1] || null
  };
}
function convoItems() {
  return Array.from(document.querySelectorAll('.conversation-item'));
}
function findConvo(title) {
  return convoItems().find(it => txt(it).includes(title)) || null;
}
function convoList() {
  return convoItems().map(it => {
    const card = it.querySelector('.cb-agent-card') || it;
    const cls = card.className.toString();
    return {
      title: txt(it).slice(0, 60),
      selected: cls.indexOf('_selected_') !== -1,
      deletable: !!it.querySelector('button.agent-card-more-button') && cls.indexOf('_selected_') === -1
    };
  });
}
/* 用户自己的会话，任何自动删除都不得碰。
   会话标题是应用自动生成/会变的，按关键字匹配且宁可多列。
   真正的安全兜底是：run_task 只按 created_title 精确删自己刚建的那条。 */
var PROTECT_TITLES = ['Buddy加油站自动签到', '询问当前项目进度', '短剧转场生硬待优化',
                      '继续执行任务', '生成包公偏见录', '包公偏见录', '包拯漫剧'];
function protectedTitle(t) {
  return PROTECT_TITLES.some(p => t.indexOf(p) !== -1);
}
async function deleteConvoEl(it, force) {
  const out = { title: txt(it).slice(0, 40), steps: [] };
  if (protectedTitle(out.title) && !force) {
    out.err = '命中保护名单（用户自己的会话），拒绝删除';
    return out;
  }
  const card = it.querySelector('.cb-agent-card') || it;
  if (card.className.toString().indexOf('_selected_') !== -1) {
    out.err = '是当前正在使用的会话，拒绝删除';
    return out;
  }
  hover(card); await sleep(450);
  const more = it.querySelector('button.agent-card-more-button');
  if (!more) { out.err = '找不到更多按钮'; return out; }
  realClick(more); await sleep(1400);
  out.steps.push('more');
  const menu = document.querySelector('.conversation-context-menu');
  if (!menu) { out.err = '菜单未弹出'; return out; }
  const del = Array.from(menu.querySelectorAll('button')).find(b => txt(b).includes('删除'));
  if (!del) { out.err = '菜单里没有删除项'; return out; }
  realClick(del); await sleep(1500);
  out.steps.push('menu-delete');
  let confirm = null;
  for (let i = 0; i < 20; i++) {
    /* 2026-09-23 真机 dump：确认框是 div[role=dialog].wb-popover-compact，
       确认按钮 class 含 _okButtonDanger/_okButton，文案「停止并删除」（运行中）
       或「删除/确认删除」（空闲），取消按钮 class 是 _cancelButton。
       旧判据 wb-button--danger / 文案=确认删除 与真实 DOM 对不上，故废弃。 */
    const dlg = document.querySelector('[role=dialog].wb-popover-compact, [role=dialog][class*=popover]');
    if (dlg) {
      confirm = Array.from(dlg.querySelectorAll('button')).find(b => {
        const r = b.getBoundingClientRect();
        if (r.width < 20 || r.height < 14) return false;
        const c = b.className.toString();
        if (c.indexOf('_cancelButton') !== -1) return false;
        const t = txt(b);
        return c.indexOf('okButton') !== -1 ||
               /^(停止并删除|删除|确认删除|确认)$/.test(t);
      });
    }
    if (!confirm) {
      confirm = Array.from(document.querySelectorAll('button')).find(b => {
        const r = b.getBoundingClientRect();
        if (r.width < 20 || r.height < 14) return false;
        const c = b.className.toString();
        const t = txt(b);
        return (c.indexOf('okButtonDanger') !== -1 && c.indexOf('_cancelButton') === -1) ||
               t === '停止并删除';
      });
    }
    if (confirm) break;
    await sleep(300);
  }
  if (!confirm) {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    out.err = '没等到确认按钮';
    return out;
  }
  realClick(confirm); await sleep(2200);
  out.steps.push('confirmed');
  out.gone = !document.contains(it);
  out.ok = out.gone;
  if (!out.gone) out.err = '点了确认但会话仍在';
  return out;
}
async function deleteConvo(title, force) {
  const it = findConvo(title);
  if (!it) return { title: title, err: '侧边栏找不到该会话' };
  return await deleteConvoEl(it, force);
}
/* 删「刚创建的那条」：只看任务区第一条（新会话必置顶），且标题必须对得上才动手 ——
   标题对不上 = 宁可不删，绝不猜（防误删用户会话的铁律）。 */
async function deleteNewTask(expectTitle) {
  const items = Array.from(document.querySelectorAll('.conversation-section-tasks .conversation-item'));
  if (!items.length) return { ok: false, err: '任务区为空' };
  const it = items[0];
  const title = txt(it).replace(/\s+\d+\s*(秒|分钟|小时|天)前$/, '')
                        .replace(/\s+刚刚$/, '').trim();
  const exp = (expectTitle || '').trim();
  const match = exp && (title.indexOf(exp) === 0 || exp.indexOf(title) === 0);
  if (!match) {
    return { ok: false, err: '任务区第一条不是本次新建的会话（防误删，拒绝）',
             top: title.slice(0, 40), expect: exp.slice(0, 40) };
  }
  return await deleteConvoEl(it, false);
}
function topTaskTitle() {
  // 5.6.x：任务区 class 是 .conversation-section--tasks（旧版无横线）
  const sec = document.querySelector('.conversation-section--tasks')
            || document.querySelector('.conversation-section-tasks');
  const it = sec ? sec.querySelector('.conversation-item')
                 : document.querySelector('.conversation-item');
  if (!it) return null;
  return txt(it).replace(/\s+\d+\s*(秒|分钟|小时|天)前$/, '').replace(/\s+刚刚$/, '').slice(0, 50);
}
/* 「任务」区全部标题（去相对时间后缀）。跑前拍快照、跑完再拍，只删新增的那条 ——
   切号后侧边栏延迟重绘，读到的「第一条」可能是上一个账号的会话。 */
function taskTitles() {
  // 5.6.x 任务区 class 是 .conversation-section--tasks（带双横线）；只取「任务」区，
  // 绝不碰「空间」区（那是用户的工作区会话，任何自动删除都不得删）。
  const sec = document.querySelector('.conversation-section--tasks')
            || document.querySelector('.conversation-section-tasks');
  const base = sec || document;
  return Array.from(base.querySelectorAll('.conversation-item'))
    .map(it => txt(it).replace(/\s+\d+\s*(秒|分钟|小时|天)前$/, '')
                       .replace(/\s+刚刚$/, '').slice(0, 50))
    .filter(Boolean);
}
function isVisible(el) {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 8 && r.height > 8;
}
async function closeOverlays(maxRound) {
  const out = { closed: [] };
  const rounds = maxRound || 3;
  for (let i = 0; i < rounds; i++) {
    const overlays = Array.from(document.querySelectorAll(
      '[class*=modal-overlay], [class*=dialog-overlay], [class*=modal_], [class*=settings-modal]'))
      .filter(el => isVisible(el) && el.className.toString().indexOf('__') === -1);
    const open = overlays.filter(el => {
      const cls = el.className.toString();
      return (cls.indexOf('overlay') !== -1 || cls.indexOf('settings-modal') !== -1);
    });
    if (!open.length) break;
    let hit = null;
    for (const ov of open) {
      const cls = ov.className.toString();
      hit = ov.querySelector('[class*=__close]')
         || Array.from(ov.querySelectorAll('button')).find(b =>
              isVisible(b) && /关闭|取消|知道了|返回/.test(txt(b)))
         || (cls.indexOf('overlay') !== -1 ? ov : null);
      if (hit) { realClick(hit); out.closed.push(hit.className.toString().slice(0, 60)); break; }
    }
    if (!hit) {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      out.closed.push('escape');
    }
    await sleep(900);
  }
  return out;
}
async function setScene(name) {
  const pills = Array.from(document.querySelectorAll('.wb-scene-tabs__pill'))
    .filter(p => isVisible(p));
  const cur = pills.find(p => p.className.toString().indexOf('--active') !== -1);
  if (cur && txt(cur) === name) return { ok: true, already: true, scene: name };
  const pill = pills.find(p => txt(p) === name);
  if (!pill) return { ok: false, err: '找不到场景 ' + name,
                      scenes: pills.map(p => txt(p)) };
  realClick(pill);
  await sleep(2200);
  const after = Array.from(document.querySelectorAll('.wb-scene-tabs__pill'))
    .filter(p => isVisible(p) && p.className.toString().indexOf('--active') !== -1)
    .map(p => txt(p))[0];
  return { ok: after === name, scene: after };
}

/* 内容风控（页面 JS 版，与 Python 端 media_blocked 同口径）：
   scenes 灵感流程在页面内过滤消耗型卡片时调用。命中返回命中词数组，否则返回 []。 */
var MEDIA_BLOCK_KEYWORDS_JS = [
  '视频生成','生视频','图生视频','文生视频','文生图','生图','生成图片','图片生成',
  '绘图','作图','画图','画一张','海报','封面','插画','漫画','写真','数字人','配音',
  '音频生成','生成音乐','音乐生成','渲染一张','以图生图','图生图','AI绘画','AI 绘画'
];
function media_blocked() {
  var blob = Array.from(arguments).join(' ');
  return MEDIA_BLOCK_KEYWORDS_JS.filter(function(k){ return blob.indexOf(k) !== -1; });
}

/* 发送原语（JS 版）：markSendBtn 定位发送按钮 → 合成点击 → 验证会话真实发起。
   run_task 的 Python 侧优先用 click_send()（CDP 真实鼠标，更可靠），
   此 JS 版供其它页面内流程兜底，避免 ReferenceError。 */
async function send() {
  var m = markSendBtn();
  if (!m.ok) return m;
  var btn = document.querySelector('button.cr-send-button');
  realClick(btn);
  await sleep(800);
  var vc = null;
  for (var i=0;i<10;i++) {
    vc = await verifyConversationStarted();
    if (vc.error) return { ok:false, err: vc.error, verify: vc };
    if (vc.started) return { ok:true, verify: vc };
    await sleep(900);
  }
  return { ok:false, err: '会话未发起', verify: vc };
}

/* 选模板（兜底，当前无调用方；保留以保证 PRELUDE 自洽）：
   在首页按名字找快捷动作/场景按钮并点击。 */
async function useTemplate(name) {
  if (!name) return { ok: false, err: 'empty template name' };
  var cands = Array.from(document.querySelectorAll(
    'button.quick-actions__item, button.quick-actions-sub__item, button.wb-scene-tabs__pill, button'))
    .filter(function(b){ return isVisible(b) && txt(b) === name; });
  var b = cands[0];
  if (!b) return { ok: false, err: '找不到模板 ' + name };
  realClick(b);
  await sleep(1800);
  return { ok: true, name: name };
}
"""


def js(code, timeout=120.0):
    """在渲染进程里跑一段 JS（可用 PRELUDE 工具函数）。

    整段包进 IIFE（页面长期存活且反复注入，顶层声明会残留冲突）。
    切号 reload 会销毁 JS 执行上下文：这类错重连一次就能拿到新上下文，重试最多 3 次。
    """
    wrapped = "(async () => {\n" + PRELUDE + "\nreturn await (\n" + code + "\n);\n})()"
    last = None
    for _attempt in range(3):
        cdp, page = C.connect()
        try:
            return cdp.evaluate(wrapped, timeout=timeout)
        except Exception as e:
            last = e
            msg = str(e)
            if (
                "Execution context was destroyed" in msg
                or "Cannot find context" in msg
                or "Execution context" in msg
            ):
                # 切号/reload 后必然命中这里。以前是静默重试（最多 3×timeout 一声不吭，
                # 表现为「切过去后什么都没做」）——现在必须看得见。
                try:
                    evidence(
                        "js.retry",
                        "CDP 执行上下文销毁，第 %d/3 次重连（单次 timeout=%.0fs）"
                        % (_attempt + 2, timeout),
                        data={"msg": msg[:200]},
                    )
                except Exception:
                    pass
                time.sleep(2.5)
                continue
            raise
        finally:
            try:
                cdp.close()
            except Exception:
                pass
    if last is not None:
        raise last


# --------------------------------------------------------------------------- #
# 观测证据：执行过程可见 —— 每一步「看到了什么 / 做了什么 / 结果如何」落盘到
# logs/ui_evidence/，供工作台与复盘核对。原则：决策必须基于 probe 的真实读数，
# 不靠猜、不靠预设数据；失败时必须能说清「当时页面上实际有什么」。
# --------------------------------------------------------------------------- #
EVIDENCE_DIR = os.path.join(CFG.LOG_DIR, "ui_evidence")
EVIDENCE_ENABLED = os.environ.get("BR_EVIDENCE", "1") != "0"
_EV_SEQ = {"n": 0}


def _json_safe(v):
    try:
        json.dumps(v, ensure_ascii=False)
        return v
    except Exception:
        return str(v)[:600]


def evidence(tag, what, data=None, shot=False):
    """记录一条执行证据。tag=步骤名；what=一句人话（看到了什么/做什么/结果）；
    data=原始观测读数；shot=True 时同时截当前客户端画面。返回证据文件名。"""
    if not EVIDENCE_ENABLED:
        return None
    try:
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
        _EV_SEQ["n"] += 1
        name = "%s_%03d_%s" % (
            time.strftime("%H%M%S"),
            _EV_SEQ["n"] % 1000,
            re.sub(r"[^a-zA-Z0-9_.-]", "_", str(tag))[:40],
        )
        rec = {
            "ts": CFG.now_str(),
            "tag": tag,
            "what": str(what)[:500],
            "data": _json_safe(data),
        }
        path = os.path.join(EVIDENCE_DIR, name + ".json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(path + ".tmp", path)
        if shot:
            try:
                cdp, _p = C.connect()
                C.screenshot(cdp, os.path.join(EVIDENCE_DIR, name + ".png"))
                cdp.close()
                rec["shot"] = name + ".png"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return name
    except Exception:
        return None


def probe(expr, tag, what="", timeout=60.0, shot=False):
    """观测式执行：跑一段（通常只读的）JS，把返回值作为证据落盘。返回观测结果。
    驱动流程里的关键决策点一律先 probe 再动手——看到的才是依据。"""
    r = js(expr, timeout=timeout)
    evidence(tag, what or "probe: %s" % str(r)[:180], data=r, shot=shot)
    return r


# 「当时页面上实际有什么」——失败时自动抓现场，回答『到底怎么了』而不是干等超时。
DIAG_EXPR = r"""
(() => {
  const T = el => (el.innerText||'').replace(/\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>6 && r.height>6; };
  const btns = Array.from(document.querySelectorAll('button')).filter(vis)
    .map(b => T(b)).filter(t => t && t.length < 20).slice(0, 25);
  const view = document.querySelector('.cr-message-list-viewport') ? 'chat'
             : (document.querySelector('.wb-home-route__input-box') ? 'home'
             : (document.querySelector('.ec-list-tab') ? 'expert-center'
             : (document.querySelector('[class*=settings-modal],[class*=settings]') ? 'settings'
             : '?')));
  return { view: view, url: location.href.slice(0, 90),
           nick: ((document.querySelector('.user-menu-trigger')||{}).innerText||'').slice(0,30),
           buttons: btns,
           cards: document.querySelectorAll('.ec-expert-card').length,
           editorLen: (document.querySelector('[contenteditable=true]')||{innerText:''}).innerText.trim().length,
           modal: !!document.querySelector('[class*=modal],[class*=overlay],[class*=dialog]'),
           body: (document.body.innerText||'').replace(/\s+/g,' ').slice(0, 240) };
})()
"""


def diagnose(tag="diag"):
    """抓当前页面的「现场」：在哪个视图、有哪些可点按钮、有没有弹层、编辑器有没内容。
    用于失败时回答『当时到底怎么了』，而不是只报一句超时。"""
    try:
        d = js(DIAG_EXPR, timeout=30)
    except Exception as e:
        d = {"err": "%s: %s" % (type(e).__name__, str(e)[:160])}
    evidence(tag, "现场诊断", data=d, shot=True)
    return d


# 黄金规则七：逐层结构法（用户 2026-09-24 定）。
# 禁止大范围 querySelectorAll + 逐节点几何计算（= 全页遍历 = 渲染进程卡死）。
# 正确方式：每进入一个界面，只读**该层 children** 的结构（tag/class/文本摘要，零几何），
# 选中层后继续下钻——一层一层用真实数据定位，直到目标元素。
PEEK_JS = r"""
(args) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const root = args.sel ? document.querySelector(args.sel) : document.body;
  if (!root) return { err: '容器不存在: ' + args.sel };
  const layer = (el, d) => {
    const out = [];
    let i = 0;
    for (const c of el.children) {
      if (i >= 10) { out.push({ more: true, hint: '用更精确的 sel 下钻' }); break; }
      const cls = (typeof c.className === 'string' ? c.className : '').split(/\s+/).slice(0, 2).join(' ');
      out.push({
        tag: c.tagName,
        cls: cls.slice(0, 44),
        txt: norm(c.innerText || '').slice(0, 30),
        kids: d > 1 ? layer(c, d - 1) : undefined
      });
      i++;
    }
    return out;
  };
  return layer(root, args.depth || 2);
}
"""


def peek_structure(sel=None, depth=2, timeout=20):
    """逐层读界面结构（黄金规则七的标准实现）。

    只读 children 的 tag/class/文本摘要，**零 getBoundingClientRect、零全量遍历**。
    用法：先 peek_structure() 看顶层 → 从返回的 cls 里选中目标容器 →
    peek_structure(sel='.那个class', depth=2) 继续下钻，直到拿到目标元素的真实结构。
    """
    args = json.dumps({"sel": sel, "depth": depth}, ensure_ascii=False)
    expr = "(%s)(%s)" % (PEEK_JS.strip(), args)
    try:
        r = js(expr, timeout=timeout)
    except Exception as e:
        return {"err": "%s: %s" % (type(e).__name__, str(e)[:100])}
    return r if isinstance(r, (list, dict)) else {"err": "异常返回: %s" % str(r)[:100]}


# 防呆①：发射/发送后立刻自检是否落进「待确认 / 等回答 / 选项」态。
# 判据只认当前屏幕文本，不认固定选择器：命中即如实报告，绝不自行点选项、也不干等它自己好转。
PENDING_CHK = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vp = document.querySelector('.cr-message-list-viewport');
  // 只判**当前会话尾部**的强特征：全局 body 会混进侧栏其它会话（含用户自己正在用的会话）导致误报。
  const conv = vp ? norm(vp.innerText).slice(-500) : '';
  const strong = /(等待你的回答|等待你的回复|等待你的选择|等你回答|等你确认|请确认后继续|请选择一下)/.test(conv);
  const side = /待确认/.test(norm(document.body.innerText));   // 仅供参考，不参与判定
  return { pending: strong, side_pending: side, tail: conv.slice(-140) };
})()
"""

# 防呆②：所有发射提示词强制带这句，从源头不让 AI 进入提问/待确认态。
NO_ASK_SUFFIX = (
    "。请直接给出完整结果：不要向我提问、不要请求我确认、不要等待我的回答；"
    "信息不全时自行做合理假设并继续完成。"
)


def wait_until(expr, timeout=30.0, poll=1.0, tag="wait"):
    """等一个 JS 条件成立（返回真或 {ok:true}）——替代固定 sleep 的「干等」。
    条件一满足立刻返回，不再空耗。返回 (是否等到, 最后一次读数)。"""
    deadline = time.time() + max(0.0, timeout)
    last = None
    while time.time() < deadline:
        try:
            last = js(expr, timeout=min(30.0, max(5.0, poll * 4)))
        except Exception as e:
            last = {"err": "%s: %s" % (type(e).__name__, str(e)[:120])}
        if last is True or (isinstance(last, dict) and last.get("ok")):
            return True, last
        time.sleep(max(0.3, poll))
    return False, last


def assert_effect(tag, check_expr, timeout=20.0, what=""):
    """操作后断言「真的生效了」。等到就过；等不到立刻抓现场诊断，不干等到底。
    返回 (ok, 读数)；ok=False 时读数含 scene（现场）。"""
    ok, last = wait_until(check_expr, timeout=timeout, tag=tag)
    if ok:
        evidence(tag, "生效确认：%s" % (what or tag), data=last)
        return True, last
    scene = diagnose(tag + ".fail")
    evidence(
        tag,
        "生效失败：%s（现场已抓）" % (what or tag),
        data={"last": last, "scene": scene},
    )
    return False, {"last": last, "scene": scene}


def wait_page_ready(timeout=60.0, interval=3.0):
    """等渲染进程的 JS 执行上下文真正可用（切号/reload 之后）。返回 (是否就绪, 等待秒数)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if js("(() => 1 + 1)()", timeout=20) == 2:
                return True, round(time.time() - t0, 1)
        except Exception:
            pass
        time.sleep(interval)
    return False, round(time.time() - t0, 1)


# --------------------------------------------------------------------------- #
# 基础动作
# --------------------------------------------------------------------------- #
def status():
    return js(
        "(() => ({ url: location.href.slice(0, 90), ...pageState() }))()", timeout=30
    )


def shot(path, full=False):
    cdp, page = C.connect()
    try:
        n = C.screenshot(cdp, path, full=full)
        return {"path": path, "bytes": n}
    finally:
        cdp.close()


MODEL_LABEL_JS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const cand = Array.from(document.querySelectorAll('button,[role=button],div,span'))
    .filter(e => e.children.length === 0)
    .map(e => norm(e.innerText || ''))
    .filter(t => t && t.length < 26)
    .filter(t => /^(Hy4 preview|Hy3|Hy4|GLM-5\.\d|Deepseek|Kimi|agnes|MiniMax|快速|均衡|极致)/.test(t));
  return cand.length ? cand[0] : '';
})()
"""


def current_model():
    """读当前界面上显示的模型名（模型闸用：切模型前后比对，防改到用户当前会话）。"""
    try:
        r = js(MODEL_LABEL_JS, timeout=20)
    except Exception:
        return ""
    return r if isinstance(r, str) else ""


def set_model(name, guard=True):
    """切模型 + **模型闸**（防呆③）。

    项目 RUNBOOK 已知坑：`set_model` 虽先 openNewTask 再 pickModel，实测仍会改到
    **用户当前会话**的模型（曾烧掉 37.25 积分）。这里强制前后各读一次屏幕模型名，
    发现被改就切回原模型并复核；复核仍不一致则在返回值里如实标记，绝不假装没发生。
    """
    before = current_model() if guard else ""
    res = js(
        "(async () => { await openNewTask(); return await pickModel(%s); })()"
        % json.dumps(name)
    )
    if not guard:
        return res
    time.sleep(1.5)
    after = current_model()
    out = {
        "result": res,
        "model_before": before,
        "model_after": after,
        "user_model_changed": False,
    }
    if before and after and after != before:
        out["user_model_changed"] = True
        evidence(
            "model.guard",
            "切模型改到了当前会话模型（%s→%s），尝试恢复" % (before, after),
        )
        try:
            js(
                "(async () => { await pickModel(%s); })()" % json.dumps(before),
                timeout=40,
            )
            time.sleep(1.5)
        except Exception as e:
            out["restore_err"] = str(e)[:80]
        out["model_restored_to"] = current_model()
        evidence("model.guard", "恢复结果：%s" % out.get("model_restored_to"))
    return out


def set_model_via_storage(name):
    """兜底：直接写客户端 localStorage 的默认模型键（cb-newtask:model:<uid>）。"""
    mid = MODEL_NAME_TO_ID.get(name) or name.lower().replace(" ", "-")
    keys = js(
        "(function(){ try { return Object.keys(localStorage)"
        ".filter(function(k){ return k.indexOf('cb-newtask:model') === 0; }); }"
        " catch(e){ return []; } })()",
        timeout=20,
    )
    uid = None
    if isinstance(keys, list):
        for k in keys:
            if k.startswith("cb-newtask:model:") and len(k) > len("cb-newtask:model:"):
                uid = k[len("cb-newtask:model:") :]
                break
    store_key = ("cb-newtask:model:" + uid) if uid else "cb-newtask:model"
    payload = json.dumps({"id": mid, "isThinking": False, "reasoningEffort": None})
    ok = js(
        "(function(){ try { localStorage.setItem(%s, %s); return true; }"
        " catch(e){ return String(e); } })()"
        % (json.dumps(store_key), json.dumps(payload)),
        timeout=20,
    )
    return {"ok": ok is True, "id": mid, "key": store_key, "raw": ok}


def click_text(text, timeout=20):
    """按文本点击：**优先 CDP 真实鼠标**。

    2026-09-23 修复：原来只用合成事件 realClick（dispatchEvent），对 React 受控
    组件常常"点了没反应"，表现为任务日志里「动作执行=True」但服务端进度不动。
    真实鼠标（Input.dispatchMouseEvent）才是可靠的；只在真实点击找不到元素/落点
    被遮挡时才回退合成事件。
    """
    expr = "exact('button', %s) || deepest('*', %s)" % (
        json.dumps(text),
        json.dumps(text),
    )
    try:
        r = mouse_click(expr, timeout=timeout)
        if isinstance(r, dict) and r.get("ok"):
            return r
        fallback_err = (r or {}).get("err") or "未知"
    except Exception as e:
        fallback_err = str(e)[:60]
    r2 = js(
        "(async () => { const el = exact('button', %s) || deepest('*', %s);"
        " if (!el) return {ok:false, err:'没找到（真实鼠标：%s）'};"
        " realClick(el); await sleep(1500);"
        " return {ok:true, fallback:true, state: pageState()}; })()"
        % (json.dumps(text), json.dumps(text), fallback_err)
    )
    return r2


def clear_input():
    """清空输入框（Slate 受控：Ctrl+A 全选 + Backspace 删除，真实按键）。"""
    cdp, page = C.connect()
    try:
        ok = cdp.evaluate(
            "(function(){var e=document.querySelector('[contenteditable=true]');"
            "if(!e) return false; e.focus();"
            "window.__wbDriverTyping = true;"
            "window.__wbDriverUntil = Date.now() + 8000; return true;})()",
            timeout=20,
        )
        if not ok:
            return {"ok": False, "err": "找不到输入框"}

        def key(t, k, code, vk, mods=0):
            cdp.call(
                "Input.dispatchKeyEvent",
                {
                    "type": t,
                    "modifiers": mods,
                    "key": k,
                    "code": code,
                    "windowsVirtualKeyCode": vk,
                    "nativeVirtualKeyCode": vk,
                },
                timeout=15,
            )

        key("keyDown", "a", "KeyA", 65, 2)
        key("keyUp", "a", "KeyA", 65, 2)
        key("rawKeyDown", "Backspace", "Backspace", 8)
        key("keyUp", "Backspace", "Backspace", 8)
        time.sleep(0.7)
        left = cdp.evaluate(
            "(function(){var e=document.querySelector('[contenteditable=true]');"
            "window.__wbDriverTyping = false; window.__wbDriverUntil = 0;"
            "return {text: e ? (e.innerText||'').replace(/\\s+/g,' ').trim().slice(0,60) : null,"
            "        chips: document.querySelectorAll('.phrase-content-wrapper').length};})()",
            timeout=20,
        )
        return {"ok": True, "after": left}
    finally:
        try:
            driver_typing(False)
        except Exception:
            pass
        cdp.close()


_RECT_JS = (
    "(function(){var e=%s; if(!e) return null;"
    " var r=e.getBoundingClientRect();"
    " var cx=r.left+r.width/2, cy=r.top+r.height/2;"
    " var top=document.elementFromPoint(cx,cy);"
    " return {x:cx, y:cy, w:Math.round(r.width), h:Math.round(r.height),"
    "         inView: r.top>=0 && r.left>=0 && r.bottom<=window.innerHeight && r.right<=window.innerWidth,"
    "         hit: !!(top && (top===e || e.contains(top) || top.contains(e))),"
    "         hitCls: top ? String(top.className).slice(0,70) : null};})()"
)


def mouse_click(js_expr, timeout=30, scroll_block='center'):
    """用 CDP 真实鼠标事件点击元素（合成事件对 React 受控组件无效时的正解）。

    三步：滚动（instant）→ 重新读 rect → elementFromPoint 校验落点再点。
    （scrollIntoView 后立刻读 rect 会拿到平滑滚动途中的旧坐标。）

    scroll_block='nearest'：用于滚动容器（如模型下拉）里贴边的项——'center' 会把
    项滚出容器可视区（getBoundingClientRect 仍报可见坐标，但被 overflow 裁剪、
    elementFromPoint 命中的是分割线），'nearest' 只做最小滚动让项完整可见。
    """
    cdp, _ = C.connect()
    try:
        try:
            cdp.evaluate(
                "(function(){var e=%s; if(!e) return 0;"
                " var r=e.getBoundingClientRect();"
                " if (r.top<0 || r.bottom>window.innerHeight) {"
                "   if (e.scrollIntoView) e.scrollIntoView({block:%s, behavior:'instant'}); }"
                " return 1;})()" % (js_expr, json.dumps(scroll_block)),
                timeout=timeout,
            )
        except C.CdpError:
            return {"ok": False, "err": "找不到元素"}
        time.sleep(0.35)
        try:
            rect = cdp.evaluate(_RECT_JS % js_expr, timeout=timeout)
        except C.CdpError:
            return {"ok": False, "err": "找不到元素"}
        if not rect:
            # 2026-09-25: 自动诊断——原选择器找不到元素，用文字匹配找按钮（只找 button，不遍历整个页面）
            try:
                _fallback = cdp.evaluate(
                    "(function() {"
                    "  const btns = Array.from(document.querySelectorAll('button, [role=button]'));"
                    "  const visible = btns.filter(b => {"
                    "    const r = b.getBoundingClientRect();"
                    "    return r.width > 0 && r.height > 0 && !b.disabled;"
                    "  });"
                    "  if (visible.length === 0) return null;"
                    "  // 返回前5个可见按钮的文字和位置，供 fallback 用"
                    "  return visible.slice(0, 10).map(b => ({"
                    "    text: (b.innerText || '').trim().slice(0, 20),"
                    "    x: b.getBoundingClientRect().x + b.getBoundingClientRect().width/2,"
                    "    y: b.getBoundingClientRect().y + b.getBoundingClientRect().height/2"
                    "  }));"
                    "})()",
                    timeout=10,
                )
                return {
                    "ok": False,
                    "err": "找不到元素（fallback已扫描按钮）",
                    "fallback_buttons": _fallback,
                }
            except Exception:
                return {"ok": False, "err": "找不到元素"}
        if rect["w"] < 2 or rect["h"] < 2:
            return {"ok": False, "err": "元素不可见", "rect": rect}
        if not rect["hit"]:
            return {"ok": False, "err": "落点被遮挡", "rect": rect}
        x, y = rect["x"], rect["y"]
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
            timeout=15,
        )
        time.sleep(0.12)
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            timeout=15,
        )
        time.sleep(0.09)
        cdp.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            },
            timeout=15,
        )
        time.sleep(0.5)
        return {"ok": True, "rect": rect}
    finally:
        cdp.close()


def mouse_hover(js_expr, timeout=30):
    """CDP 真实鼠标移动到元素中心（scrollIntoView→取矩形→mouseMoved）。
    用于让 hover 才显现的控件（如 chip 关闭图标）出现。"""
    rect = js(
        "(function(){var b=%s;if(!b)return null;b.scrollIntoView({block:'center'});"
        "var r=b.getBoundingClientRect();"
        "return{x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height};})()"
        % js_expr,
        timeout=10,
    )
    if not rect or rect.get("w", 0) < 1 or rect.get("h", 0) < 1:
        return {"ok": False, "err": "元素不可见/找不到", "rect": rect}
    cdp, page = C.connect()
    try:
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": rect["x"], "y": rect["y"], "button": "none"},
            timeout=15,
        )
        time.sleep(0.4)
        return {"ok": True, "rect": rect}
    finally:
        cdp.close()


def _wheel_select_model(want):
    """下拉已打开时：真实 wheel 把目标项滚稳进可视区 → 校验命中目标 → 真实点击。

    slim-scroll 平滑滚动期间坐标会继续移动，必须等 scrollTop 连续稳定、且
    elementFromPoint 命中的确实是目标项名后再点（否则会点到滑入该位置的相邻项）。
    """
    cdp, _ = C.connect()
    try:
        def _snap():
            return js(
                r"""
((want)=>{
  var list=document.querySelector('.cr-model-selector__list');
  if(!list) return {err:'no list'};
  var lr=list.getBoundingClientRect();
  var its=Array.from(document.querySelectorAll('.cr-model-selector__item'));
  var hit=its.find(it=>{var nm=it.querySelector('.cr-model-selector__item-name');
    if(!nm) return false;
    var t=(nm.innerText||'').replace(/\s+/g,' ').trim();
    return t===want || t.toLowerCase().indexOf(want.toLowerCase())!==-1;});
  var item=null;
  if(hit){var r=hit.getBoundingClientRect();
    item={top:r.top,bottom:r.bottom,cx:r.x+r.width/2,cy:r.y+r.height/2};}
  return {listTop:lr.top,listBottom:lr.bottom,listX:lr.x+lr.width/2,
          listY:lr.top+lr.height/2,scrollTop:list.scrollTop,
          scrollH:list.scrollHeight,clientH:list.clientHeight,item:item};
})(%s)
"""
                % json.dumps(want),
                timeout=10,
            )

        snap = _snap()
        if snap.get("err"):
            return {"ok": False, "err": snap["err"]}
        lx, ly = snap["listX"], snap["listY"]
        lt, lb = snap["listTop"], snap["listBottom"]

        def _good(sn):
            it = sn.get("item")
            if not it:
                return False
            if not (it["cy"] > lt + 8 and it["cy"] < lb - 8):
                return False
            hitel = js(
                "(function(x,y){var el=document.elementFromPoint(x,y);var nm=(el&&el.closest)?"
                "el.closest('.cr-model-selector__item'):null;var t=nm?nm.querySelector"
                "('.cr-model-selector__item-name'):null;return t?(t.innerText||'')"
                ".split(String.fromCharCode(10)).join(' ').trim():null;})(%s,%s)" % (it["cx"], it["cy"]),
                timeout=8,
            )
            return bool(hitel and (hitel == want or hitel.lower().find(want.lower()) != -1))

        # 最多 40 次 wheel；按目标项相对可视区的位置决定方向/步长（双向）
        prev_top = None
        stable = 0
        for _i in range(40):
            if _good(snap):
                break
            it0 = snap.get("item")
            if not it0:
                delta = 120  # 项被虚拟列表回收，继续向下找
            elif it0["cy"] < lt + 8:
                delta = max(-220.0, it0["cy"] - (lt + (lb - lt) / 2))  # 在上方，向上
            elif it0["cy"] > lb - 8:
                delta = min(220.0, it0["cy"] - (lt + (lb - lt) / 2))  # 在下方，向下
            else:
                delta = 0
            cdp.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": lx, "y": ly, "button": "none", "buttons": 0},
                timeout=10,
            )
            time.sleep(0.04)
            if delta:
                cdp.call(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseWheel", "x": lx, "y": ly, "deltaX": 0, "deltaY": delta},
                    timeout=10,
                )
            time.sleep(0.24)
            snap = _snap()
            if snap.get("scrollTop") == prev_top:
                stable += 1
            else:
                stable = 0
            prev_top = snap.get("scrollTop")
            # 平滑滚动收尾：连续多帧 scrollTop 不变后再判 _good
        else:
            return {"ok": False, "err": "目标项未能滚入可视区"}

        # 等平滑滚动完全静止（连续 3 次坐标一致）
        it = snap["item"]
        lastcy = None
        hold = 0
        for _ in range(20):
            time.sleep(0.12)
            snap = _snap()
            it = snap.get("item")
            if it and it["cy"] == lastcy and _good(snap):
                hold += 1
                if hold >= 3:
                    break
            else:
                hold = 0
            lastcy = it["cy"] if it else None

        if not (it and _good(snap)):
            return {"ok": False, "err": "滚动未稳定/落点不是目标项", "snap": snap}

        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": it["cx"], "y": it["cy"], "button": "none", "buttons": 0},
            timeout=10,
        )
        time.sleep(0.1)
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": it["cx"], "y": it["cy"], "button": "left",
             "buttons": 1, "clickCount": 1},
            timeout=10,
        )
        time.sleep(0.08)
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": it["cx"], "y": it["cy"], "button": "left",
             "buttons": 0, "clickCount": 1},
            timeout=10,
        )
        return {"ok": True}
    finally:
        cdp.close()


def native_pick_model(name, timeout=30):
    """CDP 原生鼠标切模型（页面内 realClick 在长列表里不可靠）。

    开下拉 → 在 .cr-model-selector__list 里用 scroll_block='nearest' 把目标项
    滚进可视区 → elementFromPoint 校验落在项上 → 真实点击 → 核对 trigger 文案。
    返回 {ok, after|err}。
    """
    want = str(name)

    def _open_drop():
        return mouse_click(
            "document.querySelector('.cr-model-selector__trigger')", timeout=timeout
        )

    def _current_trigger():
        return js(
            "(()=>{var t=document.querySelector('.cr-model-selector__trigger');"
            "return t?(t.innerText||'').replace(/\\s+/g,' ').trim():null;})()",
            timeout=10,
        )

    def _mark_item():
        return js(
            r"""
((want)=>{
  var its=Array.from(document.querySelectorAll('.cr-model-selector__item'));
  var hit=its.find(it=>{var nm=it.querySelector('.cr-model-selector__item-name');
    if(!nm) return false;
    var t=(nm.innerText||'').replace(/\s+/g,' ').trim();
    return t===want || t.toLowerCase().indexOf(want.toLowerCase())!==-1;});
  if(!hit) return {ok:false};
  hit.setAttribute('data-mpick','1');
  return {ok:true};
})(%s)
"""
            % json.dumps(want),
            timeout=10,
        )

    try:
        trig = _current_trigger()
        if trig and (want in trig or trig.lower() == want.lower()):
            return {"ok": True, "after": trig, "already": True}
        for _attempt in range(2):
            oc = _open_drop()
            if not (isinstance(oc, dict) and oc.get("ok")):
                return {"ok": False, "err": "打开模型下拉失败", "detail": oc}
            time.sleep(1.4)
            # 下拉会保留上次 scrollTop，先归位（portal 动画期间列表可能还没就绪，重试）
            for _w in range(10):
                rr = js(
                    "(function(){var l=document.querySelector('.cr-model-selector__list');"
                    "if(!l)return false;l.scrollTop=0;return true;})()",
                    timeout=8,
                )
                if rr:
                    break
                time.sleep(0.25)
            time.sleep(0.6)
            mk = _mark_item()
            if mk.get("ok"):
                break
            time.sleep(0.6)
        else:
            return {"ok": False, "err": "模型列表里没有 %s" % want}
        # 长列表：slim-scroll 平滑滚动，mouse_click 的 scrollIntoView 会滚过头。
        # 改为在列表中心派发真实 wheel，轮询直到 scrollTop 与目标项位置稳定，
        # elementFromPoint 确认命中目标项，再用真实坐标点击。
        rc = _wheel_select_model(want)
        if not rc.get("ok"):
            return {"ok": False, "err": "点击模型项失败", "detail": rc}
        time.sleep(1.2)
        after = _current_trigger()
        if after and (want in after or after.lower() == want.lower()):
            return {"ok": True, "after": after}
        return {"ok": False, "err": "切换后触发标签为「%s」" % after, "after": after}
    finally:
        try:
            js("document.querySelectorAll('[data-mpick]').forEach(e=>e.removeAttribute('data-mpick'));1", timeout=8)
        except Exception:
            pass


def click_send(timeout=20):
    """定位发送按钮 → CDP 真实鼠标点击（不做结果判定，结果由调用方轮询
    verifyConversationStarted 核对）。真实鼠标对 React 受控组件可靠，
    合成 b.click() 常被吞。"""
    mark = js("(function(){ return markSendBtn(); })()", timeout=10)
    if not (isinstance(mark, dict) and mark.get("ok")):
        return mark if isinstance(mark, dict) else {"ok": False, "err": "发送按钮不可用"}
    rc = mouse_click(mark["selector"], timeout=timeout)
    return rc if isinstance(rc, dict) else {"ok": False, "err": "点击发送异常"}


def _keyboard_type(cdp, text, gap=0.02):
    """逐字符真实键盘输入（CDP Input char 事件）。

    2026-09-27 根因修复：Slate 编辑器只更新 DOM 的内容不会进入其内部模型，
    表现为 innerText 有字、发送按钮可点，但点击后 onSubmit 读到空串、消息不发。
    Input.insertText 在该客户端版本会踩这个坑；改用逐个字符派发
    Input.dispatchKeyEvent(type='char')，等价于真实键盘敲入，Slate 经
    beforeinput/insertText 同步内部模型，发送才真正生效（已实测：收到回复）。
    """
    for ch in str(text):
        cdp.call(
            "Input.dispatchKeyEvent",
            {
                "type": "char",
                "key": ch,
                "text": ch,
                "unmodifiedText": ch,
                "code": "",
                "windowsVirtualKeyCode": 0,
                "nativeVirtualKeyCode": 0,
            },
            timeout=10,
        )
        if gap:
            time.sleep(gap)


def type_text(text, do_send=False):
    """往输入框打字（真实逐字符键盘输入，进入 Slate 内部模型后发送才生效）。"""
    cdp, page = C.connect()
    try:
        ok = cdp.evaluate(
            "(function(){var e=document.querySelector('[contenteditable=true]');"
            "if(!e) return false; e.focus();"
            "window.__wbDriverTyping = true;"
            "window.__wbDriverUntil = Date.now() + 8000 + %d;"
            "var s=window.getSelection(); var r=document.createRange();"
            "r.selectNodeContents(e); r.collapse(false); s.removeAllRanges(); s.addRange(r);"
            "return true;})()" % (len(str(text)) * 40),
            timeout=20,
        )
        if not ok:
            return {"ok": False, "err": "找不到输入框"}
        _keyboard_type(cdp, text)
        time.sleep(1.0)  # React 渲染
        cdp.evaluate(
            "(function(){window.__wbDriverTyping = false;"
            " window.__wbDriverUntil = 0; return true;})()",
            timeout=15,
        )
        # 2026-09-25: 严格自检——读回输入框，确认输入框里只有我的提示词（不能有系统自动填的残留）
        _check = cdp.evaluate(
            "(function(){var e=document.querySelector('[contenteditable=true]');"
            "return e ? (e.innerText||'').trim() : '';})()",
            timeout=10,
        )
        _expected = str(text).strip()
        if text and (not _check or str(_check).strip() != _expected):
            # 输入框里不是只有我的提示词——先清空再重新输入
            time.sleep(1.0)
            clear_input()
            time.sleep(0.5)
            _keyboard_type(cdp, text)
            time.sleep(1.0)
            # 再验证一次
            _check2 = cdp.evaluate(
                "(function(){var e=document.querySelector('[contenteditable=true]');"
                "return e ? (e.innerText||'').trim() : '';})()",
                timeout=10,
            )
            if str(_check2).strip() != _expected:
                # 还是不对——再清一次再输入
                time.sleep(1.0)
                clear_input()
                time.sleep(0.5)
                _keyboard_type(cdp, text)
                time.sleep(1.0)
        if do_send:
            # 2026-09-23 修复：发送按钮同样要用真实鼠标（合成 b.click() 常被 React 吞掉，
            # 表现是"字打进去了但没发出去"，服务端自然不计分）。
            _send_btn = (
                "(function(){var b=document.querySelector('button.cr-send-button');"
                " if(b && b.getBoundingClientRect().width>0 && !b.disabled) return b;"
                " var all=Array.from(document.querySelectorAll('button,[role=\"button\"]'));"
                " return all.find(function(e){var a=e.getAttribute('aria-label')||'';"
                "  var r=e.getBoundingClientRect();"
                "  return /发送|send/i.test(a) && r.width>0 && r.height>0;}) || null;})()"
            )
            try:
                _r = mouse_click(_send_btn, timeout=15)
                sent_ok = bool(isinstance(_r, dict) and _r.get("ok"))
            except Exception:
                sent_ok = False
            if not sent_ok:
                # 兜底：合成 click + 回车
                cdp.evaluate(
                    "(function(){var b=document.querySelector('button.cr-send-button');"
                    "if(b){b.click(); return true;} return false;})()",
                    timeout=20,
                )
                time.sleep(0.5)
            time.sleep(1.0)
        state = cdp.evaluate(
            "(function(){var e=document.querySelector('[contenteditable=true]');"
            "return {text: e ? (e.innerText||'').replace(/\\s+/g,' ').trim().slice(0,120) : null,"
            "        chips: Array.from(document.querySelectorAll('.phrase-content-wrapper')).map(x=>(x.innerText||'').trim())};})()",
            timeout=20,
        )
        return {"ok": True, "state": state, "sent": do_send}
    finally:
        try:
            driver_typing(False)
        except Exception:
            pass
        cdp.close()


# --------------------------------------------------------------------------- #
# run_task 流水线（关键：中途不能插入其它操作）
# --------------------------------------------------------------------------- #
def run_task(
    template,
    prompt,
    model=None,
    max_wait=300,
    poll=True,
    delete_after=False,
    use_template=True,
    pre_js=None,
    keep_input=False,
    pre_steps=None,
    pre_gap=3.0,
    pre_timeout=180,
    pre_clear=False,
    pre_must_ok=False,
    stop=None,
    yield_to_user=True,
    team_first_reply=False,
    idle_stable_polls=4,
    min_wait=0,
    pre_retry=0,
    ask_rescue=0,
):
    """新建任务 -> 清空 -> 选模型 -> 选模板 -> 补指令 -> 发送 -> 轮询到对话结束。

    delete_after=True：对话结束后删掉自己刚建的那条会话（进度是服务端事件计数，删除不倒退）。
    use_template=False：只选模型不选模板（Model_chat_GLM5.2 用）。
    pre_steps：前置动作列表，每项一次独立 JS 调用（导航类动作必须拆步）。
    keep_input=True：不清空输入框（召唤专家/做同款会预填内容和 chip）。
    pre_must_ok=True：任何前置步骤失败立刻中止，绝不把消息发进错误会话。
    pre_retry=N：每个前置步骤做完后验结果，不 ok 就重试 N 次（抓现场+写证据），
       而不是「做完就往下走」——授权类步骤（弹层/外部窗口时序不稳）必须开。
    min_wait：发送后至少等这么多秒才允许 idle-stable 收工（专家团防提前收工）。
    ask_rescue=N：轮询命中反问（多选/单选/「请提供材料」等）时最多自救 N 轮：
       优先点「跳过/知道了/随便/忽略」按钮，没有按钮就补发一条「继续完成，不再提问」
       指令并重置轮询状态继续等（专家团真实生命周期 30+ 分钟，一次反问不能判死会话）。
    """
    out = {"template": template, "prompt": prompt, "model": model}

    def _stopped():
        try:
            if stop and stop():
                return True
        except Exception:
            pass
        return stop_requested()

    if _stopped():
        out["ok"] = False
        out["stopped"] = True
        out["err"] = "收到停止指令，未开始"
        return out

    if yield_to_user:
        busy, info = user_busy()
        out["user_busy"] = info
        if busy:
            out["ok"] = False
            out["yielded"] = True
            out["err"] = (
                "检测到用户正在使用客户端，已让路（不自作主张新建任务）：%s" % info
            )
            return out

    # 回输入页：点一次 → 等条件成立（不干等固定秒数）→ 失败重试一次 → 仍失败抓现场
    js("(async () => { await openNewTask(); return true; })()", timeout=90)
    _HOME_CHK = r"""
(() => {
  const ed = document.querySelector('[contenteditable=true]');
  const box = ed ? ed.closest('.wb-home-route__input-box, .cr-input-box') : null;
  return {
    ok: !!ed,
    isHome: !!(box && box.className.toString().indexOf('wb-home-route__input-box') !== -1),
    hasAddMenu: !!document.querySelector('.cr-add-menu__trigger'),
    convoCount: convoCount(),
    title: (document.querySelector('.cr-message-list-viewport') ? 'chat' : 'home')
  };
})()
"""
    _hok, home = wait_until(_HOME_CHK, timeout=18.0, poll=1.5, tag="task.home")
    if not _hok:
        js("(async () => { await openNewTask(); return true; })()", timeout=60)
        _hok, home = wait_until(
            _HOME_CHK, timeout=12.0, poll=1.5, tag="task.home.retry"
        )
    evidence("task.home", "输入页就绪=%s" % _hok, data=home)
    out["home"] = home
    if not _hok or not isinstance(home, dict) or not home.get("ok"):
        scene = diagnose("task.home.fail")
        out["ok"] = False
        out["scene"] = scene
        out["err"] = "拿不到新建任务输入框（现场：视图=%s 可点按钮=%s）" % (
            scene.get("view"),
            (scene.get("buttons") or [])[:8],
        )
        return out

    if pre_js:
        out["pre"] = js(pre_js, timeout=pre_timeout)

    if pre_steps:
        steps = []
        gaps = (
            list(pre_gap)
            if isinstance(pre_gap, (list, tuple))
            else [pre_gap] * len(pre_steps)
        )
        for i, s in enumerate(pre_steps):
            if _stopped():
                out["ok"] = False
                out["stopped"] = True
                out["err"] = "前置动作第 %d 步前收到停止指令，已中止" % (i + 1)
                out["pre_steps"] = steps
                return out
            try:
                r = s() if callable(s) else js(s, timeout=pre_timeout)
            except Exception as e:
                r = {"err": "%s: %s" % (type(e).__name__, str(e)[:120])}

            # 每步做完必须验结果：不 ok 就重试（pre_retry 次），不是跳过往下走。
            # 重试前抓一次现场，失败时能说清「当时页面上有什么」，而不是只有一句 err。
            for _retry in range(max(0, int(pre_retry or 0))):
                if isinstance(r, dict) and r.get("ok"):
                    break
                try:
                    scene = diagnose("pre.retry")
                except Exception:
                    scene = {}
                try:
                    evidence(
                        "pre.retry",
                        "前置第 %d 步没成功，第 %d/%d 次重试（err=%s）"
                        % (
                            i + 1,
                            _retry + 1,
                            int(pre_retry or 0),
                            (r or {}).get("err") or "ok=false",
                        ),
                        data={
                            "view": (scene or {}).get("view"),
                            "buttons": (scene.get("buttons") or [])[:8],
                        },
                    )
                except Exception:
                    pass
                time.sleep(3)
                try:
                    r = s() if callable(s) else js(s, timeout=pre_timeout)
                except Exception as e:
                    r = {"err": "%s: %s" % (type(e).__name__, str(e)[:120])}
            steps.append(r)
            if i < len(pre_steps) - 1:
                time.sleep(gaps[min(i, len(gaps) - 1)])
        out["pre_steps"] = steps
        merged = {}
        for r in steps:
            if isinstance(r, dict):
                for k, v in r.items():
                    if v not in (None, "", [], {}):
                        merged[k] = v
        out["pre"] = merged

        # 2026-09-25: 召唤专家团后，等 3 秒，让系统自动填默认的提示词，然后再清空
        # 不然清空太早，系统后面又自动填进来了
        time.sleep(3)

        if pre_must_ok:
            bad = [
                (i, (r or {}).get("err") or "ok=false")
                for i, r in enumerate(steps)
                if not (isinstance(r, dict) and r.get("ok"))
            ]
            if bad:
                scene = diagnose("pre.abort")
                out["ok"] = False
                out["scene"] = scene
                out["err"] = (
                    "前置步骤失败，已中止（未发送）：%s ｜ 现场：视图=%s 可点按钮=%s"
                    % (
                        bad[:3],
                        (scene or {}).get("view"),
                        (scene.get("buttons") or [])[:8],
                    )
                )
                return out

    # 拍「跑前」快照（必须在 openNewTask 之后，刚切号时侧边栏还是旧账号的列表）
    # 一律拍：created_title 供调用方做「生效才删」，delete_after 只控制是否当场删
    before_titles = stable_task_titles()
    if delete_after:
        out["before_titles"] = before_titles

    if keep_input:
        out["cleared"] = {"ok": True, "skipped": "keep_input"}
    else:
        out["cleared"] = clear_input()

    # 选模型：model=None → cheap_models() 依次回退（空链=沿用当前模型）；model="" → 明确不碰选择器
    cands = [model] if model else ([] if model == "" else list(cheap_models()))
    mres = {
        "ok": True,
        "skipped": "model='' 明确不选模型"
        if model == ""
        else "省量模型链为空，沿用当前模型",
    }
    for cand in cands:
        mres = js(
            "(async () => { const m = await pickModel(%s); return m; })()"
            % json.dumps(cand),
            timeout=90,
        )
        if isinstance(mres, dict) and mres.get("ok"):
            out["model_used"] = cand
            break
    out["prepare_model"] = mres
    if cands and not (isinstance(mres, dict) and mres.get("ok")):
        fb = None
        for cand in cands:
            fb = set_model_via_storage(cand)
            if isinstance(fb, dict) and fb.get("ok"):
                try:
                    js(
                        "(async () => { await openNewTask(); await sleep(1500); return true; })()",
                        timeout=60,
                    )
                except Exception:
                    pass
                mres2 = js(
                    "(async () => { const m = await pickModel(%s); return m; })()"
                    % json.dumps(cand),
                    timeout=90,
                )
                if isinstance(mres2, dict) and mres2.get("ok"):
                    mres = mres2
                    out["model_used"] = cand
                    out["model_via_storage_fallback"] = True
                    break
        if not (isinstance(mres, dict) and mres.get("ok")):
            out["ok"] = False
            out["err"] = "选模型失败（候选 %s）：%s；localStorage 兜底：%s" % (
                cands,
                (mres or {}).get("err"),
                fb,
            )
            return out
    if out.get("model_used"):
        out["model"] = out["model_used"]

    pre = {"model": mres, "steps": []}
    if cands:
        pre["steps"].append("model")
    if use_template:
        _tb = media_blocked(template)
        if _tb:
            out["ok"] = False
            out["err"] = (
                "模板「%s」命中消耗型产出名单（%s）—— 会话任务只允许文本/代码，"
                "按内容红线拒绝执行" % (template, _tb)
            )
            out["content_guard"] = {"blocked_template": _tb}
            return out
        t = js(
            "(async () => { const t = await useTemplate(%s); return t; })()"
            % json.dumps(template or ""),
            timeout=90,
        )
        pre["template_result"] = t
        if not (isinstance(t, dict) and t.get("ok")):
            out["prepare"] = pre
            out["ok"] = False
            out["err"] = "选模板失败"
            return out
        pre["steps"].append("template")
    pre["ok"] = True
    out["prepare"] = pre

    # 内容风控：生图/生视频/生音频这类消耗型产出烧的是模型积分，会话任务一律只走文本/代码。
    # 命中就把指令换成纯文本版本，并把拦截记录写进结果，绝不静默放行。
    if prompt or template:
        prompt, _hits = guard_text_only(prompt, template)
        if _hits:
            out["content_guard"] = {"blocked": _hits, "prompt": prompt}
            try:
                evidence(
                    "content.guard", "拦截消耗型产出指令 %s，已改为纯文本指令" % _hits
                )
            except Exception:
                pass

    # 防呆②：提示词末尾强制追加"不许反问"指令（源头避免 AI 进入待确认态）
    if prompt and "不要向我提问" not in str(prompt):
        prompt = str(prompt).rstrip() + NO_ASK_SUFFIX

    if prompt:
        out["typed"] = type_text(prompt)
        if not out["typed"].get("ok"):
            out["ok"] = False
            out["err"] = "输入指令失败"
            return out

    # 2026-09-26: useTemplate 已在编辑器预填完整提示词；prompt 为空时也应触发发送，
    # 否则模板任务走到这里会因无 prompt 而跳过 send()，客户端永远停在预填态。
    will_send = bool(prompt or (use_template and template))
    if not will_send:
        out["ok"] = False
        out["err"] = "既无指令也无模板，无内容可发送"
        return out

    if not poll:
        # 发射模式：发完就返回，不轮询收尾（调用方统一等待）。
        s = click_send(timeout=20)
        out["send"] = s
        out["sent"] = bool(isinstance(s, dict) and s.get("ok"))
        out["ok"] = out["sent"]
        if not out["sent"]:
            out["err"] = "发送失败：%s" % (
                (s or {}).get("err") if isinstance(s, dict) else s
            )
            return out
        # 防呆①：发完立刻看屏幕，落进待确认/等回答就如实标记（不点选项、不等它自愈）
        try:
            pc = js(PENDING_CHK, timeout=25)
            out["pending_check"] = pc
            if isinstance(pc, dict) and pc.get("pending"):
                out["pending_confirm"] = True
                evidence(
                    "task.pending",
                    "发射后落进待确认态（源头防呆未生效，如实记录）",
                    data=pc,
                )
        except Exception as e:
            out["pending_check"] = {"err": "%s: %s" % (type(e).__name__, str(e)[:80])}
        # 防呆②（2026-09-26 修复）：发射模式同样要轮询会话真实状态——
        # 自定义模型请求失败（UNKNOWN_STOP_REASON/额度/网络）也会表现为"已发送"，
        # 必须识别错误并返回失败，让上层重新发起，而不是误报发射成功。
        _t0 = time.time()
        _err_hit = None
        _started = False
        while time.time() - _t0 < 15:
            _vc = js("(async () => { return await verifyConversationStarted(); })()", timeout=10)
            if isinstance(_vc, dict):
                if _vc.get("error"):
                    _err_hit = _vc["error"]
                    break
                if _vc.get("started"):
                    _started = True
                    break
            time.sleep(0.8)
        if _err_hit:
            out["ok"] = False
            out["err"] = "发送后会话报错（%s），已中止发射等待重新发起" % _err_hit
            out["verify_error"] = _err_hit
            out["sent"] = False
            return out
        if not _started:
            out["ok"] = False
            out["err"] = "发送后 15s 内未确认会话真实发起，按不盲发原则判失败，等待重新发起"
            out["sent"] = False
            return out
        return out

    sent = click_send(timeout=20)
    out["send"] = sent
    if not (isinstance(sent, dict) and sent.get("ok")):
        out["ok"] = False
        out["err"] = "发送失败：%s" % (
            (sent or {}).get("err") if isinstance(sent, dict) else sent
        )
        return out
    out["sent"] = True

    # 分段轮询（每段约 10s，段间回 Python 检查停止信号 —— 用户点停止最多等一段就收手）
    max_iter = max(1, int(max_wait * 2))
    _POLL_CHUNK = 20
    st = None
    post = None
    stopped = False
    start_i = 1
    rescue_left = max(0, int(ask_rescue))

    def _ask_rescue_once():
        """反问自救：优先真实点击「跳过/知道了/随便/忽略」类可见按钮；没有再补发指令。

        返回 dict 描述本次自救动作，写进 evidence 便于复盘。"""
        # 1) 找可见、文本匹配的按钮并打标（打标后走真实鼠标点击，React 不吞合成事件）
        try:
            mark = js(
                """
(() => {
  const T = el => (el.innerText || '').replace(/\\s+/g, '').trim();
  const want = ['跳过', '下次', '随便', '知道了', '忽略'];
  const vis = el => { const r = el.getBoundingClientRect();
    return r.width > 20 && r.height > 20 && r.top >= 0 && r.bottom <= (window.innerHeight + 4); };
  const b = Array.from(document.querySelectorAll('button,[role="button"]'))
    .find(x => vis(x) && want.some(w => (T(x) || '').includes(w)));
  if (!b) return { ok: false };
  b.setAttribute('data-ask-rescue', '1');
  b.scrollIntoView({ block: 'center' });
  return { ok: true, hit: (T(b) || '').slice(0, 24) };
})()
""",
                timeout=25,
            )
        except Exception as _e:  # noqa: BLE001
            mark = {"ok": False, "err": str(_e)[:120]}
        if isinstance(mark, dict) and mark.get("ok"):
            try:
                clicked = mouse_click(
                    "document.querySelector('[data-ask-rescue=\"1\"]')", timeout=15
                )
            except Exception as _e:  # noqa: BLE001
                clicked = {"ok": False, "err": str(_e)[:120]}
            return {"mode": "click", "hit": mark.get("hit"), "clicked": clicked}
        # 2) 没有可点按钮 → 补发「继续完成，不再提问」指令（复用自包含精神：
        #    用示例数据当场完成；type_text 内置真实鼠标点击发送，防 React 吞事件）
        try:
            rtext = type_text(
                "请按上面的执行规则继续完成，不要再向我提问：我无法提供更多材料，"
                "请用合理示例数据直接给出完整结论与产出清单。",
                do_send=True,
            )
        except Exception as _e:  # noqa: BLE001
            rtext = {"ok": False, "err": str(_e)[:120]}
        return {"mode": "resend", "type": rtext}

    while start_i <= max_iter:
        if _stopped():
            stopped = True
            break
        r = js(
            """
(async (prev, iters, startI, maxIter, tf, idleN, minIdleIter) => {
  const out = {};
  const view = () => document.querySelector('.cr-message-list-viewport');
  const quotaTxt = () => {
    const v = view();
    const t = v ? (v.innerText || '') : '';
    const m = t.match(/(额度已用尽|购买加量包|429)/);
    return m ? m[0] : '';
  };
  const errTxt = () => {
    const t = (document.body.innerText || '');
    return (t.indexOf('发生未知错误') !== -1 || t.indexOf('超出频率限制') !== -1) ? 'model-error' : '';
  };
  function snap() {
    const v = view();
    const agents = v ? Array.from(v.querySelectorAll('.cr-agent')) : [];
    const last = agents[agents.length - 1];
    const stEl = last ? last.querySelector('.cr-agent__processing-status') : null;
    const b = document.querySelector('button.cr-send-button');
    const busyTxt = stEl ? /思考中|生成回复|等待模型|排队中|运行中|处理中|执行中|正在/.test(stEl.innerText || '') : false;
    return {
      nAg: agents.length,
      proc: v ? !!v.querySelector('.cr-agent--processing') : false,
      busyTxt: busyTxt,
      len: v ? (v.innerText || '').length : 0,
      status: stEl ? (stEl.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 22) : '',
      stop: b ? b.className.toString().indexOf('--stop') !== -1 : false,
      quota: quotaTxt(),
      err: errTxt()
    };
  }
  const log = (prev && prev.log) ? prev.log : [];
  let lastKey = (prev && prev.lastKey !== undefined) ? prev.lastKey : null;
  let stable = prev ? (prev.stable || 0) : 0;
  let stuckFor = prev ? (prev.stuckFor || 0) : 0;
  // 全程见过的最大 agent 数。团队模式下 nAg 恒为 1 = 协作根本没启动 = 永远不计分
  // （旧项目实测结论：授权流程没走 → nAg=1 → +0）。以前这个值从没被记录和校验过，
  // 于是 59 次尝试里 45 次「看起来跑完了、实际空转 8 分钟」。
  let maxAg = (prev && prev.maxAg) || 0;
  let finished = false, done = false, reason = '', quota = '';
  for (let k = 0; k < iters; k++) {
    const i = startI + k;
    if (i > maxIter) { finished = true; reason = 'timeout'; break; }
    await sleep(500);
    const s = snap();
    if (s.nAg > maxAg) maxAg = s.nAg;
    const busy = s.proc || s.busyTxt;
    const key = JSON.stringify({ nAg: s.nAg, busy: busy, len: s.len });
    if (key !== lastKey) { log.push({ t: Math.round(i * 0.5), ...s }); lastKey = key; stable = 0; }
    else stable++;
    if (s.proc) stuckFor = 0; else stuckFor++;
    if (s.quota) { finished = true; done = false; reason = 'quota-exhausted'; quota = s.quota; break; }
if (s.err) { finished = true; done = false; reason = 'model-error'; break; }
    if (i > 6 && i >= minIdleIter && !busy && s.len > 0 && stable >= idleN) {
      // 「页面安静」≠「完成」：团队反问用户时页面同样安静 → 判反问未完成
      const v2 = view();
      const ags2 = v2 ? Array.from(v2.querySelectorAll('.cr-agent')) : [];
      const lastCard = ags2[ags2.length - 1];
      const tail = lastCard ? (lastCard.innerText || '').replace(/\\s+/g, ' ').trim().slice(-200) : '';
      const askHit = /(请提供|请上传|请补充|请发送|发给我|需要你|需要您|等你|等待你|确认后我再|告诉我你|请选择|选择你)/.test(tail);
      out.askTail = tail.slice(-120);
      finished = true;
      if (askHit) { done = false; reason = 'ask-user'; }
      else { done = true; reason = 'idle-stable'; }
      break;
    }
    // 反问独立检测：不等 minIdleIter（团队模式要等数百秒才走到上一块，反问后就是傻等）。
    // 只要“回复已出现（len>0）+ 安静满一轮（stable≥idleN）+ 至少 20s”，就查反问特征；
    // 命中 → askHit=true 交给 Python 侧做「点跳过/补发指令」自救，而不是干等。
    if (i > 40 && !busy && s.len > 0 && stable >= idleN) {
      const v3 = view();
      const ags3 = v3 ? Array.from(v3.querySelectorAll('.cr-agent')) : [];
      const lastCard3 = ags3[ags3.length - 1];
      const tail3 = lastCard3 ? (lastCard3.innerText || '').replace(/\\s+/g, ' ').trim().slice(-200) : '';
      const askHit3 = /(请提供|请上传|请补充|请发送|发给我|需要你|需要您|等你|等待你|确认后我再|告诉我你|请选择|选择你)/.test(tail3);
      if (askHit3) {
        out.askTail = tail3.slice(-120);
        out.askHit = true;
        finished = true; done = false; reason = 'ask-user';
        break;
      }
    }
    if (tf && i > 10 && s.len > 120 && stable >= 3) { finished = true; done = true; reason = 'team-first-reply'; break; }
    // 团队模式（tf=false 且 minIdleIter 很大，即 min_wait 设到数百秒的那类）：
    // 足够长时间（>100s）仍只有 1 个 agent、且页面**已经安静一整轮**（!busy && stable≥idleN）
    // 才判「团队压根没成立」。以前只按 i>200 && maxAg≤1 就砍：团队组队/预热阶段 UI 可安静
    // 60s+，第一张 agent 卡出现前 maxAg=1 是正常的 → 把正在正常交付的团队误杀
    // （实测 2026-09-24 account_k：财税合规会话 32m43s 完整交付，脚本 100s 就杀队换下一个）。
    if (!tf && i > 200 && maxAg <= 1 && minIdleIter > 400 && !busy && stable >= idleN) {
      finished = true; done = false; reason = 'team-not-formed'; break;
    }
    if (i > 6 && s.proc && stable >= 160) { finished = true; done = false; reason = 'stuck-processing'; break; }
  }
  out.finished = finished; out.done = done; out.reason = reason; out.quota = quota;
  out.last = snap();
  out.state = { log: log, lastKey: lastKey, stable: stable, stuckFor: stuckFor, maxAg: maxAg };
  try {
    const side = document.querySelector('aside') || document.body;
    out.pendingSeen = (side.innerText || '').indexOf('待确认') !== -1;
  } catch (e) { out.pendingSeen = false; }
  return out;
})(%s, %d, %d, %d, %s, %d, %d)
"""
            % (
                json.dumps(st or {}),
                _POLL_CHUNK,
                start_i,
                max_iter,
                json.dumps(bool(team_first_reply)),
                int(idle_stable_polls),
                int(min_wait * 2),
            ),
            timeout=_POLL_CHUNK + 40,
        )
        if not isinstance(r, dict):
            post = {"done": False, "reason": "poll-error", "err": str(r)[:200]}
            break
        st = r.get("state") or st
        start_i += _POLL_CHUNK
        if r.get("finished"):
            if r.get("askHit") and rescue_left > 0 and not stopped and not _stopped():
                # 反问自救：不判死会话。点「跳过/知道了」或补发继续指令，
                # 重置轮询状态（st=None → stable/prev 重新累积）继续等，
                # 直到 max_wait 用尽或自救次数耗尽。
                rescue_left -= 1
                act = _ask_rescue_once()
                st = None
                evidence(
                    "ask.rescue",
                    "反问自救 %d/%d（%s）"
                    % (ask_rescue - rescue_left, ask_rescue, act.get("mode")),
                    {"act": act, "askTail": str(r.get("askTail"))[:200]},
                )
                continue
            post = {
                "done": bool(r.get("done")),
                "reason": r.get("reason"),
                "quota": r.get("quota"),
            }
            if r.get("pendingSeen"):
                post["pendingSeen"] = True
            break

    if stopped:
        out["stopped"] = True
        out["stop_click"] = js(
            """
(() => {
  const b = document.querySelector('button.cr-send-button');
  if (b && b.className.toString().indexOf('--stop') !== -1) { b.click(); return { ok: true }; }
  return { ok: false, err: '未处于生成中' };
})()
""",
            timeout=30,
        )
        if post is None:
            post = {"done": False, "reason": "user-stopped"}
        post["stopped"] = True

    if post is None:
        post = {"done": False, "reason": "timeout"}

    post["final"] = js(
        """
(() => {
  const v = document.querySelector('.cr-message-list-viewport');
  const agents = v ? Array.from(v.querySelectorAll('.cr-agent')) : [];
  const last = agents[agents.length - 1];
  const stEl = last ? last.querySelector('.cr-agent__processing-status') : null;
  const b = document.querySelector('button.cr-send-button');
  const t = v ? (v.innerText || '') : '';
  const q = t.match(/(额度已用尽|购买加量包|429)/);
  return { nAg: agents.length,
           proc: v ? !!v.querySelector('.cr-agent--processing') : false,
           len: t.length,
           status: stEl ? (stEl.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 22) : '',
           stop: b ? b.className.toString().indexOf('--stop') !== -1 : false,
           quota: q ? q[0] : '' };
})()
""",
        timeout=40,
    )
    post["log"] = (st or {}).get("log")

    out["run"] = post
    out["ok"] = bool(post.get("done"))
    if post.get("reason") == "quota-exhausted":
        out["quota_exhausted"] = True
        out["err"] = (
            "该账号额度已用尽（%s）——AI 没有响应，任务不会计分；"
            "需要等额度恢复或购买加量包。重试无意义。" % post.get("quota")
        )
    out["convoAfter"] = js("(() => convoCount())()", timeout=30)
    # 无论 delete_after 与否都识别本次新建的会话标题（供调用方验证生效后再删）
    after_titles = stable_task_titles()
    new = [t for t in (after_titles or []) if t not in set(before_titles or [])]
    out["created_title"] = new[0] if len(new) == 1 else None
    out["new_titles"] = new
    if delete_after:
        js(
            "(async () => { await openNewTask(); await sleep(600); return true; })()",
            timeout=60,
        )
        if len(new) == 1:
            out["deleted"] = delete_new_task(new[0])
        else:
            out["deleted"] = {
                "ok": False,
                "err": "本次新增会话数=%d，拒绝删除以免误删" % len(new),
            }
    return out


# --------------------------------------------------------------------------- #
# 跨域 iframe（资料库 = space-panel-iframe，OOPIF 独立调试目标）
# --------------------------------------------------------------------------- #
def iframe_cdp(url_substr="workbuddy.cn"):
    return C.connect_target(url_substr=url_substr, type_="iframe")


def eval_in_iframe(js_expr, url_substr="workbuddy.cn", timeout=90):
    try:
        cdp, _p = iframe_cdp(url_substr)
    except C.CdpError:
        return {"err": "找不到 %s 的 iframe 调试目标（面板没打开？）" % url_substr}
    try:
        r = cdp.call(
            "Runtime.evaluate",
            {
                "expression": js_expr,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            desc = (d.get("exception") or {}).get("description") or d.get("text")
            return {"err": "iframe 内 JS 抛错：%s" % str(desc)[:300]}
        return (r.get("result") or {}).get("value")
    finally:
        cdp.close()


def mouse_click_in_iframe(js_expr, url_substr="workbuddy.cn", timeout=30):
    try:
        cdp, _p = iframe_cdp(url_substr)
    except C.CdpError:
        return {"ok": False, "err": "找不到 iframe 调试目标"}
    try:
        rect = cdp.evaluate(
            "(function(){var e=%s; if(!e) return null;"
            " if (e.scrollIntoView) e.scrollIntoView({block:'center', behavior:'instant'});"
            " var r=e.getBoundingClientRect();"
            " var cx=r.left+r.width/2, cy=r.top+r.height/2;"
            " var top=document.elementFromPoint(cx,cy);"
            " return {x:cx, y:cy, w:Math.round(r.width), h:Math.round(r.height),"
            "         hit: !!(top && (top===e || e.contains(top) || top.contains(e)))};})()"
            % js_expr,
            timeout=timeout,
        )
        if not rect:
            return {"ok": False, "err": "iframe 内找不到元素"}
        if rect["w"] < 2 or rect["h"] < 2:
            return {"ok": False, "err": "iframe 内元素不可见", "rect": rect}
        x, y = rect["x"], rect["y"]
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
            timeout=15,
        )
        time.sleep(0.12)
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            timeout=15,
        )
        time.sleep(0.09)
        cdp.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            },
            timeout=15,
        )
        time.sleep(0.5)
        return {"ok": True, "rect": rect}
    finally:
        cdp.close()


def iframe_scroll(
    js_expr="document.scrollingElement",
    url_substr="workbuddy.cn",
    step=600,
    wait=0.4,
    max_steps=60,
):
    try:
        cdp, _p = iframe_cdp(url_substr)
    except C.CdpError:
        return {"err": "找不到 iframe 调试目标"}
    try:
        before = cdp.evaluate(
            "(function(){var e=%s;return e?Math.round(e.scrollTop):null;})()" % js_expr,
            timeout=20,
        )
        trace = []
        for _ in range(max_steps):
            r = cdp.evaluate(
                "(function(){var e=%s; if(!e) return null;"
                " var max = e.scrollHeight - e.clientHeight;"
                " e.scrollTop = Math.min(e.scrollTop + %d, Math.max(0,max));"
                " return {top: Math.round(e.scrollTop), max: Math.round(max),"
                "         sh: e.scrollHeight, ch: e.clientHeight};})()"
                % (js_expr, step),
                timeout=25,
            )
            if not isinstance(r, dict):
                break
            trace.append(r["top"])
            time.sleep(wait)
            if r["max"] <= 0 or r["top"] >= r["max"]:
                break
        after = cdp.evaluate(
            "(function(){var e=%s;return e?Math.round(e.scrollTop):null;})()" % js_expr,
            timeout=20,
        )
        return {
            "before": before,
            "after": after,
            "steps": len(trace),
            "trace": trace[-6:],
        }
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# 会话管理
# --------------------------------------------------------------------------- #
def conversations():
    return js("(() => convoList())()", timeout=30)


def delete_conversation(title, force=False):
    """按标题删除会话（includes 模糊匹配，自动流程勿用——只给 CLI/用户点名清理用）。
    force=True 跳过保护名单（仅限用户明确点名）；当前使用中的会话无论 force 都删不掉。"""
    return js(
        "(async () => await deleteConvo(%s, %s))()"
        % (json.dumps(title), "true" if force else "false"),
        timeout=90,
    )


def delete_new_task(expected_title):
    """删「本次刚创建、置顶在任务区第一条」的会话 —— 自动流程专用。
    标题必须与 expected_title 互相前缀匹配才动手，对不上就拒绝。"""
    if not expected_title:
        return {"ok": False, "err": "无 expected_title，拒绝删除"}
    return js(
        "(async () => await deleteNewTask(%s))()" % json.dumps(expected_title),
        timeout=90,
    )


def top_task_title():
    return js("(() => topTaskTitle())()", timeout=30)


def task_titles():
    return js("(() => taskTitles())()", timeout=30)


def stable_task_titles(gap=2.5, tries=6):
    """等侧边栏稳定后再取标题（避免读到重绘中的旧列表）。"""
    prev = None
    for _ in range(tries):
        cur = task_titles()
        if prev is not None and cur == prev:
            return cur
        prev = cur
        time.sleep(gap)
    return prev


def reset_team_summon_suppress():
    """清掉「团队召唤确认框」的抑制标记。

    根因：驱动器跑过授权流程后，客户端把
    agent-ui.expert-center.team-summon-confirm.suppress.<uid> 写成 true ——
    此后所有团队召唤跳过确认框 → 授权流程没走 → 团队协作不启动（nAg=1）→ 不计分。
    召唤团队前清掉全部 suppress 键，让确认框恢复弹出。
    """
    return js(
        """
(() => {
  const kill = [];
  for (let i = window.localStorage.length - 1; i >= 0; i--) {
    const k = window.localStorage.key(i);
    if (/^agent-ui\\.expert-center\\.(team-summon-confirm\\.suppress|finance-risk\\.suppress)/.test(k)) {
      window.localStorage.removeItem(k);
      kill.push(k);
    }
  }
  return { ok: true, removed: kill };
})()
""",
        timeout=30,
    )


def current_account_name():
    """当前客户端登录账号昵称（读左下角 .user-menu-trigger）。"""
    try:
        return js(
            "(document.querySelector('.user-menu-trigger')||{innerText:''})"
            ".innerText.trim().slice(0,40)",
            timeout=25,
        )
    except Exception:
        return None
