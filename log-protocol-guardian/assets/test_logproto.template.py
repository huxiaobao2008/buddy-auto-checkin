#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志协议一致性回归测试。

验证后端 :mod:`core.logproto` 生成的行，能被前端 ``web/ui.html`` 中
``renderRawLog`` / ``parseAccountPipe`` 使用的正则正确解析。

运行：
    python tests/test_logproto.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import logproto as LP

# 与 web/ui.html 中保持一致的三个正则（唯一契约的镜像）
RE_HTML_LINE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\[([a-z]+)(?::([^\]]+))?\]")
RE_HAS_API = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\[(?:api|claim)\b", re.M)
RE_HAS_UI = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\[ui[:\]]", re.M)

API_AREAS = {"start", "client", "api", "claim", "order"}
UI_AREAS = {"ui", "done", "report"}

_fails = []


def check(cond, msg):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        print("  [FAIL] %s" % msg)
        _fails.append(msg)


def test_timestamp_anchors():
    """时间戳必须是 HH:MM:SS，否则前端正则整体失配。"""
    print("\n== 时间戳格式 ==")
    line = LP.format_line(LP.AREA_API, "签到成功")
    check(RE_HTML_LINE.match(line) is not None, "format_line 可被前端正则解析: %s" % line)
    stamp = line[1:9]
    check(len(stamp) == 8 and stamp[2] == ":" and stamp[5] == ":", "时间戳为 HH:MM:SS: %s" % stamp)


def test_legacy_stage_normalized():
    """历史写法 'ui:account_1' 必须归一化，不能产生非法 area。"""
    print("\n== 遗留 stage 归一化 ==")
    area, acc = LP.normalize_stage("ui:account_1")
    check(area == "ui", "'ui:account_1' -> area=ui")
    check(acc == "account_1", "'ui:account_1' -> account=account_1")

    area2, acc2 = LP.normalize_stage("api", account="account_7")
    check(area2 == "api" and acc2 == "account_7", "显式 account 覆盖: %s/%s" % (area2, acc2))

    area3, _ = LP.normalize_stage("weird_unknown")
    check(area3 in LP.VALID_AREAS, "未知 area 降级为受控值: %s" % area3)

    line = LP.format_line("ui:account_1", "切号 ok=True | 完成")
    m = RE_HTML_LINE.match(line)
    check(m is not None and m.group(1) == "ui" and m.group(2) == "account_1",
          "遗留写法产出合法行: %s" % line)


def test_non_account_slot_rejected():
    """非 account_xxx 形态不得占用账号槽。"""
    print("\n== 账号槽校验 ==")
    _, acc = LP.normalize_stage("ui", account="random_text")
    check(acc == "", "非法账号标识被拒绝")
    line = LP.format_line("ui", "x", account="random_text")
    m = RE_HTML_LINE.match(line)
    check(m is not None and m.group(2) is None, "非法账号不出现在行内: %s" % line)


def test_side_routing():
    """各area 应路由到正确的前端区域。"""
    print("\n== 区域路由 ==")
    for a in ("api", "claim", "start", "client", "order"):
        check(LP.side_of(a) == "api", "%s -> API 区" % a)
    for a in ("ui", "done", "report"):
        check(LP.side_of(a) == "ui", "%s -> UI 区" % a)
    check(LP.side_of("nonexistent") is None, "未知 area 不路由")


def test_realistic_split():
    """用真实阶段序列验证 API/UI 分流可被前端正则识别。"""
    print("\n== 真实序列分流 ==")
    lines = [
        LP.format_line("start", "启用账号 2 个：account_1, account_2"),
        LP.format_line("client", "客户端已就绪（CDP 端口 9222）"),
        LP.format_line("api", "===== 阶段一：API 管线 ====="),
        LP.format_line("api:account_1", "签到 ok"),
        LP.format_line("ui", "===== 阶段二：UI 管线 ====="),
        LP.format_line("ui:account_1", "切号 ok=True | 完成"),
        LP.format_line("claim:account_2", "收尾领取 5 项 | 到账 20 积分"),
        LP.format_line("done", "已写进度：今天全部完成"),
    ]
    text = "\n".join(lines)

    check(RE_HAS_API.search(text) is not None, "前端可识别已进入 API 阶段")
    check(RE_HAS_UI.search(text) is not None, "前端可识别已进入 UI 阶段")

    api_bucket, ui_bucket = [], []
    for ln in lines:
        m = RE_HTML_LINE.match(ln)
        area = m.group(1) if m else None
        if area in API_AREAS:
            api_bucket.append(ln)
        elif area in UI_AREAS:
            ui_bucket.append(ln)
    check(len(api_bucket) == 5, "API 桶收5 行，实际 %d" % len(api_bucket))
    check(len(ui_bucket) == 3, "UI 桶收 3 行，实际 %d" % len(ui_bucket))
    check(not any("[ui" in x for x in api_bucket), "API 桶无 ui 行")
    check(not any("[api" in x for x in ui_bucket), "UI 桶无 api 行")

    # 账号轨道：api/account_1、ui/account_1、claim/account_2
    accs = re.findall(r"^\[\d{2}:\d{2}:\d{2}\]\[[a-z]+:(account_[a-z0-9_]+)\]", text, re.M)
    check(accs == ["account_1", "account_1", "account_2"],
          "账号轨道解析顺序正确: %s" % accs)


def test_stop_detection_not_false_positive():
    """停止判定：正常完成不得被误判为已停止。"""
    print("\n== 停止判定 ==")

    def detect(lines):
        stopped = False
        ls = [x for x in "\n".join(lines).strip().split("\n") if x.strip()]
        for ln in reversed(ls):
            m = RE_HTML_LINE.match(ln)
            area = m.group(1) if m else None
            if area == "done" or "全部完成" in ln:
                return False
            if area in ("ui", "claim", "api") and ("收到停止指令" in ln or "中止" in ln):
                return True
        return stopped

    completed = [
        LP.format_line("api", "===== 阶段一 ====="),
        LP.format_line("ui", "===== 阶段二 ====="),
        LP.format_line("done", "已写进度：今天全部完成，下次再跑会直接跳过"),
    ]
    check(detect(completed) is False, "正常完成 -> stopped=False")

    stopped_run = [
        LP.format_line("api", "===== 阶段一 ====="),
        LP.format_line("ui", "[停止] 收到停止指令，中止账号循环"),
    ]
    check(detect(stopped_run) is True, "真实停止 -> stopped=True")

    # 旧逻辑的 bug：正文出现「停止」二字即误判
    legacy_false_positive = [
        LP.format_line("ui", "本轮不停止，正常执行"),
        LP.format_line("done", "已写进度：今天全部完成"),
    ]
    naive = any("停止" in x for x in legacy_false_positive)
    check(naive is True and detect(legacy_false_positive) is False,
          "旧逻辑会误判，新逻辑正确")


def main():
    print("=" * 60)
    print("日志协议一致性回归测试")
    print("=" * 60)
    test_timestamp_anchors()
    test_legacy_stage_normalized()
    test_non_account_slot_rejected()
    test_side_routing()
    test_realistic_split()
    test_stop_detection_not_false_positive()
    print("\n" + "=" * 60)
    if _fails:
        print("失败 %d 项：" % len(_fails))
        for f in _fails:
            print("  - %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())