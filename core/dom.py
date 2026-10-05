# -*- coding: utf-8 -*-
"""基于 CDP **DOM 域**的界面结构读取与节点级点击。

黄金规则七（用户 2026-09-24 定）：进入一个界面 → 看**这一层的结构** → 选节点 → 点击 →
出现新界面再看结构，一层一层用真实数据下钻。

**精准原则**（用户 2026-09-24 定，本模块存在的理由）：
不是"页面轻所以不卡"，而是**每一步的结论都从 DOM 精确得到**——这个节点存不存在、
可不可见、可不可点、坐标在哪，全部一次问清再动手。因为精准，所以不试错、不重扫、
不误判，脚本跑起来自然不卡。旧代码遍历更多却不卡，靠的是这个；
我此前"扫一遍看看有没有、扫不到再扫一遍"，误判和卡死都是从这里来的。

与"在页面里跑 querySelectorAll + getBoundingClientRect"的根本区别：
  - `DOM.getDocument(depth=N)` 由浏览器内部分层序列化，**不在渲染进程主线程上做全量几何计算**；
  - 点击走 `DOM.getBoxModel(nodeId)` → 取 quad 中心 → `Input.dispatchMouseEvent`，
    点击对象是**具体节点**，不是选择器匹配出来的"第 N 个元素"。
  - 这正是旧代码遍历更多却不卡、新代码一跑就卡的分水岭：卡的是"页面内全量扫描 + 逐节点几何"，
    不是"遍历"这个动作本身。

用法（一轮 = 看结构 → 选节点 → 点）：
    from core import dom
    snap = dom.snapshot(depth=3)          # 看当前界面结构（每层最多 12 个，带 nodeId）
    nid  = dom.query('.某个class')         # 或按结构里的 nodeId 直接用
    dom.click(nid)                        # 节点级真实点击
"""

from __future__ import annotations

import time

from . import cdp as C

MAX_KIDS = 12          # 每层最多展开多少个节点（超出标注 more）
TEXT_LEN = 40          # 文本摘要长度
DEFAULT_DEPTH = 3


_C = None       # 复用同一连接：**nodeId 与 CDP 会话绑定，跨连接即失效**（实测踩过）


def _conn():
    global _C
    if _C is not None:
        try:
            _C.call("DOM.enable", timeout=10)
            return _C
        except Exception:
            try:
                _C.close()
            except Exception:
                pass
            _C = None
    c, _ = C.connect()
    try:
        c.call("DOM.enable", timeout=15)
    except Exception:
        pass
    _C = c
    return c


def close():
    """显式关闭复用的连接（一轮操作结束后调用，释放资源）。"""
    global _C
    if _C is not None:
        try:
            _C.close()
        except Exception:
            pass
        _C = None


def _attrs(node):
    """把 attributes 平铺数组转成 dict（class / id / role 等常用项）。"""
    out = {}
    a = node.get("attributes") or []
    for i in range(0, len(a) - 1, 2):
        out[a[i]] = a[i + 1]
    return out


def _walk(node, depth, path=""):
    """把一个 node 递归压成分层摘要（每层截断 MAX_KIDS，不做任何几何计算）。"""
    if depth <= 0:
        return None
    kids = node.get("children") or []
    # 文本节点与纯内容节点合并成一段摘要，避免逐节点展开
    texts = []
    out = []
    i = 0
    for ch in kids:
        if ch.get("nodeType") == 3:                     # text node
            v = (ch.get("nodeValue") or "").strip()
            if v:
                texts.append(v)
            continue
        if i >= MAX_KIDS:
            out.append({"more": True, "hint": "用 query() 下钻或提高 depth"})
            break
        at = _attrs(ch)
        cls = at.get("class", "")
        cls1 = cls.split()[0] if cls else ""
        item = {
            "nodeId": ch.get("nodeId"),
            "tag": ch.get("nodeName", ""),
            "cls": cls1[:40],
            "txt": (ch.get("nodeValue") or "")[:TEXT_LEN],
        }
        sub = _walk(ch, depth - 1, path)
        if sub:
            item["kids"] = sub
        out.append(item)
        i += 1
    joined = " ".join(texts)[:TEXT_LEN]
    for it in out:
        if not it.get("txt") and joined and it.get("kids") is None:
            it["txt"] = joined
    return out


def snapshot(depth=DEFAULT_DEPTH, selector=None, timeout=30):
    """读当前界面的**分层结构**。selector 给了就只读那个容器（推荐：逐层下钻）。

    返回：list（顶层节点摘要，带 nodeId）或 dict（含 err）。
    """
    c = _conn()
    try:
        doc = c.call("DOM.getDocument", {"depth": max(1, int(depth)), "pierce": False},
                     timeout=timeout)
        root = (doc or {}).get("root") or {}
        if selector:
            q = c.call("DOM.querySelector", {"nodeId": root.get("nodeId"), "selector": selector},
                       timeout=timeout)
            nid = (q or {}).get("nodeId")
            if not nid:
                return {"err": "选择器没命中：%s" % selector}
            outer = c.call("DOM.getOuterHTML", {"nodeId": nid}, timeout=timeout)
            # 结构仍按 getDocument 的树走，这里只确认命中并返回其子树摘要
            return _walk(_node_by_id(c, root, nid) or root, depth)
        return _walk(root, depth)
    except Exception as e:
        return {"err": "%s: %s" % (type(e).__name__, str(e)[:120])}
    finally:
        c.close()


def _node_by_id(c, root, nid):
    """在已获取的树里按 nodeId 找节点（找不到返回 None，调用方自行降级）。"""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.get("nodeId") == nid:
            return n
        for ch in (n.get("children") or []):
            stack.append(ch)
    return None


def query(selector, timeout=20):
    """按选择器取 nodeId（**只在已经看清结构的目标容器上用**，不做全页扫描）。"""
    c = _conn()
    try:
        doc = c.call("DOM.getDocument", {"depth": 1, "pierce": False}, timeout=timeout)
        root = (doc or {}).get("root") or {}
        q = c.call("DOM.querySelector", {"nodeId": root.get("nodeId"), "selector": selector},
                   timeout=timeout)
        return (q or {}).get("nodeId")
    except Exception:
        return None
    finally:
        c.close()


def find_by_text(text, limit=8, timeout=25):
    """按**可见文本**精准定位节点（CDP `DOM.performSearch`，浏览器内部实现，不在页面里跑 JS）。

    这是"精准度"的关键：要找哪个元素就用它的文本问一次，拿到确定的 nodeId，
    而不是把整页元素扫一遍再挑。返回 nodeId 列表（可能含祖先容器，取最具体的那个）。
    """
    c = _conn()
    try:
        r = c.call("DOM.performSearch", {"query": text}, timeout=timeout)
        sid = (r or {}).get("searchId")
        total = (r or {}).get("resultCount") or 0
        if not sid or not total:
            return []
        res = c.call("DOM.getSearchResults",
                     {"searchId": sid, "fromIndex": 0, "toIndex": int(min(limit, total))},
                     timeout=timeout)
        return (res or {}).get("nodeIds") or []
    except Exception:
        return []
    finally:
        c.close()


def nodes(selector, limit=10, timeout=25):
    """按选择器取 nodeId 列表（`DOM.querySelectorAll`，单次精准查询，不是全页遍历）。"""
    c = _conn()
    try:
        doc = c.call("DOM.getDocument", {"depth": 1, "pierce": False}, timeout=timeout)
        root = (doc or {}).get("root") or {}
        r = c.call("DOM.querySelectorAll",
                   {"nodeId": root.get("nodeId"), "selector": selector}, timeout=timeout)
        ids = (r or {}).get("nodeIds") or []
        return ids[:limit]
    except Exception:
        return []
    finally:
        c.close()


def text_of(node_id, timeout=20):
    """读**单个节点**的文本：resolveNode → Runtime.callFunctionOn（只作用于这一个节点）。

    与"全页 querySelectorAll + 逐节点 innerText"的区别：这里只对已定位的那一个节点求值，
    代价与页面大小无关。
    """
    c = _conn()
    try:
        r = c.call("DOM.resolveNode", {"nodeId": node_id}, timeout=timeout)
        oid = ((r or {}).get("object") or {}).get("objectId")
        if not oid:
            return ""
        out = c.call("Runtime.callFunctionOn", {
            "objectId": oid,
            "functionDeclaration": "function(){return (this.innerText||'').trim().slice(0,80);}",
            "returnByValue": True}, timeout=timeout)
        return (((out or {}).get("result") or {}).get("value") or "")
    except Exception:
        return ""
    finally:
        c.close()


def box(node_id, timeout=20):
    """取节点的中心坐标（DOM.getBoxModel → quad 中心）。返回 (x, y) 或 None。"""
    c = _conn()
    try:
        m = c.call("DOM.getBoxModel", {"nodeId": node_id}, timeout=timeout)
        quad = ((m or {}).get("model") or {}).get("quad") or []
        if len(quad) < 8:
            return None
        xs = [quad[0], quad[2], quad[4], quad[6]]
        ys = [quad[1], quad[3], quad[5], quad[7]]
        return (int(sum(xs) / 4.0), int(sum(ys) / 4.0))
    except Exception:
        return None
    finally:
        c.close()


def click(node_id, timeout=20, settle=0.15):
    """节点级真实鼠标点击：取中心 → mousemove → pressed → released。"""
    pos = box(node_id, timeout=timeout)
    if not pos:
        return {"ok": False, "err": "取不到节点坐标（可能不可见或已失效）"}
    x, y = pos
    c = _conn()
    try:
        for m in ({"type": "mouseMoved", "x": x - 5, "y": y - 5},
                  {"type": "mouseMoved", "x": x, "y": y}):
            c.call("Input.dispatchMouseEvent", dict(m, button="none"), timeout=10)
            time.sleep(settle)
        for ev in ("mousePressed", "mouseReleased"):
            c.call("Input.dispatchMouseEvent",
                   {"type": ev, "x": x, "y": y, "button": "left", "clickCount": 1}, timeout=10)
            time.sleep(0.08)
        return {"ok": True, "nodeId": node_id, "x": x, "y": y}
    except Exception as e:
        return {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:100])}
    finally:
        c.close()
