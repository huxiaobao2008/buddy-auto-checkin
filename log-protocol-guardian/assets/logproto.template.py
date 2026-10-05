#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""统一结构化日志协议（单一事实来源）。

设计背景与修复记录
--------------------
本模块是「后端写日志」与「前端解析日志」之间唯一的契约层。此前两者各自
硬编码正则，导致同一批日志在不同代码路径下格式不一致，前端分流全面失效。
现统一由本模块生成行格式，前端 `web/ui.html` 的 `renderRawLog()` /
`parseAccountPipe()` 必须按 `AREA_*` 规则解析。

统一行格式（严格三段式）
----------------------
    [HH:MM:SS][<area>:<account_key>] <message>

- 时间戳**只**使用 ``%H:%M:%S``。历史上混用过 ``%Y-%m-%d %H:%M:%S``，
  导致前端 ``^\[\d{2}:\d{2}:\d{2}\]`` 锚定失败，日志整批落入兜底分支。
- ``area`` 取值受控，见 :data:`VALID_AREAS`。后端禁止再传入
  ``api:account_x`` 这类「area 与账号混写」的 stage，必须用 ``account=``。
- ``account_key`` 可为空（全局日志），此时省略冒号部分。

兼容说明
--------
历史上 :func:`format_line` 也接受 ``"api:account_1"`` 这种内嵌冒号的 area，
现已在 :func:`normalize_stage` 中拆分归一，不再推荐直接使用。
"""

import datetime
import re

# --- 受控区域取值 ---------------------------------------------------------- #
AREA_START = "start"      # 启动、断点续传、总览
AREA_CLIENT = "client"    # 客户端/CDP 生命周期
AREA_API = "api"          # 阶段一：纯 API 管线
AREA_UI = "ui"            # 阶段二：UI 管线
AREA_CLAIM = "claim"      # 阶段三：收尾领取
AREA_DONE = "done"        # 收尾汇总
AREA_ORDER = "order"      # 排序/计划
AREA_REPORT = "report"    # 报告输出

VALID_AREAS = frozenset(
    {
        AREA_START,
        AREA_CLIENT,
        AREA_API,
        AREA_UI,
        AREA_CLAIM,
        AREA_DONE,
        AREA_ORDER,
        AREA_REPORT,
    }
)

# 前端分流时「归入 API 侧」的区域
API_SIDE_AREAS = frozenset({AREA_START, AREA_CLIENT, AREA_API, AREA_CLAIM, AREA_ORDER})
# 前端分流时「归入 UI 侧」的区域
# 含 report：汇总报告在流程末尾输出，归UI 侧展示
UI_SIDE_AREAS = frozenset({AREA_UI, AREA_DONE, AREA_REPORT})

_ACCOUNT_RE = re.compile(r"^(account_[a-z0-9_]+)$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def now_hms():
    """返回前端正则唯一可识别的 HH:MM:SS 时间戳。"""
    return datetime.datetime.now().strftime("%H:%M:%S")


def normalize_area(stage):
    """把任意 stage 归一为受控 area；未知值一律降级为 ``start``。

    兼容历史写法 ``"api:account_1"``：拆出 area，账号部分由调用方另行传入。
    """
    s = (stage or "").strip().lower()
    if not s:
        return AREA_START, ""
    if ":" in s:
        head, _, tail = s.partition(":")
        head = head.strip()
        return (head if head in VALID_AREAS else AREA_START), tail.strip()
    return (s if s in VALID_AREAS else AREA_START), ""


def normalize_stage(stage, account=None):
    """返回 ``(area, account)``。

    - ``stage`` 支持 ``"ui"`` / ``"ui:account_1"`` 两种写法
    - ``account`` 显式传入时优先于内嵌值
    - 非法 area 降级为 ``start``，不再污染前端分流
    """
    area, embedded = normalize_area(stage)
    acc = (account or embedded or "").strip()
    if acc and not _ACCOUNT_RE.match(acc):
        # 非 account_xxx 形态的标识不作为账号槽，避免污染前端账号解析
        acc = ""
    return area, acc


def format_line(stage, msg, account=None, hms=None):
    """生成统一格式的一行日志（不含换行符）。"""
    area, acc = normalize_stage(stage, account)
    stamp = hms if (_TIME_RE.match(hms or "") ) else now_hms()
    tag = "%s:%s" % (area, acc) if acc else area
    return "[%s][%s] %s" % (stamp, tag, "" if msg is None else str(msg))


def side_of(area):
    """返回该area 应归属的前端区域：``"api"`` / ``"ui"`` / ``None``。"""
    if area in API_SIDE_AREAS:
        return "api"
    if area in UI_SIDE_AREAS:
        return "ui"
    return None