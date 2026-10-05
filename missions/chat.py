#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话型任务（严格串行发射，发射即走，统一轮询服务端计分，积分到账后再清理）：
  template_5          使用 5 个不同模板并发起对话（每个不同模板 +1）
  Model_chat_GLM5.2   新建对话并用 GLM-5.2 成功对话一次
  black_cat           夜间 23:00-08:00 用 GLM-5.2 对话（非开放时段自动标记跳过）
"""

import datetime
import time

from engine import driver as U

CHAT_PROMPT = "用一句话回答：你好。"


def in_night(dt=None):
    h = (dt or datetime.datetime.now()).hour
    return h >= 23 or h < 8


def do_template_once(account_key, template_info=None, stop=None):
    """使用指定/自动选择的未用过模板发起一次对话，发射成功即返回，不等AI回复。
    完整流程（动态遍历分类、全局模板账本、参考文件挂载校验、真实点击）均在driver中实现。
    """
    return U.fire_template_once(account_key, template_info=template_info)


def do_model_chat(stop=None, poll=False, do_delete=False):
    """Model_chat_GLM5.2：新建对话切到 GLM-5.2，发送一句话，验证会话发起即返回。

    按 WORKFLOW：必须先切到其它免费模型、确认切换，再切回 GLM-5.2，直接停在
    GLM 发送不计分。长下拉用原生鼠标 nearest 滚动定位。
    poll/do_delete 为调度层统一入参（发射即走模式下不使用），保留以对齐调用签名。
    """
    # 打开新建任务页
    U.js("(async () => { await openNewTask(); return true; })()", timeout=60)
    U.wait_until(
        r"""
(() => {
  const ed = document.querySelector('[contenteditable=true]');
  return {ok: !!ed};
})()
""",
        timeout=20,
    )

    # 选一个非 GLM 的便宜模型先切过去
    other = None
    try:
        for _m in U.cheap_models():
            if "glm" not in str(_m).lower():
                other = str(_m)
                break
    except Exception:
        other = None
    if other:
        rpre = U.native_pick_model(other)
        if not rpre.get("ok"):
            return {"ok": False, "err": "预切换免费模型(%s)失败" % other, "detail": rpre}
        time.sleep(0.8)

    # 切回 GLM-5.2
    pick_res = U.native_pick_model("GLM-5.2")
    if not pick_res.get("ok"):
        return {"ok": False, "err": "切换GLM-5.2失败", "detail": pick_res}

    # 输入提示词（真实键盘逐字符，进入 Slate 模型）
    type_res = U.type_text(CHAT_PROMPT)
    if not type_res.get("ok"):
        return {"ok": False, "err": "输入提示词失败"}

    # 真实点击发送
    mark_send = U.js("(function() { return markSendBtn(); })()", timeout=10)
    if not (isinstance(mark_send, dict) and mark_send.get("ok")):
        return {"ok": False, "err": "发送按钮不可用"}
    send_rc = U.mouse_click(mark_send["selector"], timeout=10)
    if not send_rc.get("ok"):
        return {"ok": False, "err": "点击发送失败"}

    # 验证会话发起（识别错误态，出现模型/额度/网络错误立即判失败）
    started = False
    t0 = time.time()
    start_check = None
    while time.time() - t0 < 20:
        start_check = U.js(
            "(async () => { return await verifyConversationStarted(); })()", timeout=10
        )
        if isinstance(start_check, dict):
            if start_check.get("error"):
                return {
                    "ok": False,
                    "err": "会话发起失败（%s）" % start_check["error"],
                    "model": "GLM-5.2",
                    "started": False,
                    "detail": start_check,
                }
            if start_check.get("started"):
                started = True
                break
        time.sleep(1)
    return {
        "ok": started,
        "model": "GLM-5.2",
        "started": started,
        "detail": start_check,
    }


def do_black_cat(stop=None):
    """black_cat：夜间窗口内同GLM对话任务，非开放时段返回跳过。"""
    if not in_night():
        return {"ok": False, "skipped": "不在夜间窗口（23:00-08:00），需人工/开放时段执行"}
    return do_model_chat(stop=stop)
