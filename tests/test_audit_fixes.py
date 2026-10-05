#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""全项目静态核验回归测试。

覆盖本轮审查发现并修复的缺陷，每条用例对应一个真实 bug，
且必须能区分「修复前失败」与「修复后通过」。

运行：
    python tests/test_audit_fixes.py
"""

import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(cond, msg):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        print("  [FAIL] %s" % msg)
        _fails.append(msg)


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8-sig") as f:
        return f.read()


def test_hotkey_defined():
    """A-1：hotkey() 曾未定义，Esc 关闭浮层静默失效。"""
    print("\n== A-1 hotkey 未定义 ==")
    from engine import driver

    check(callable(getattr(driver, "hotkey", None)), "hotkey() 已定义")
    check(len(getattr(driver, "_KEY_CODES", {})) >= 5, "按键表已内置多种按键")
    r = driver.hotkey("不存在的键")
    check(r.get("ok") is False and "不支持" in r.get("err", ""), "非法按键返回明确错误")
    # 调用点不再用 try/except 静默吞掉
    src = read("engine/driver.py")
    check("hotkey(\"escape\")" in src, "调用点存在")
    idx = src.find('hotkey("escape")')
    around = src[max(0, idx - 200): idx + 200]
    check(
        "except Exception:\n            pass" not in around.split('hotkey("escape")')[0][-120:],
        "调用点不再被 except 静默吞异常",
    )


def test_no_bom():
    """A-2：4 个文件带 UTF-8 BOM 头。"""
    print("\n== A-2 BOM 头清除 ==")
    targets = ["engine/switch.py", "engine/tasks.py", "launcher.py", "missions/panels.py"]
    for rel in targets:
        raw = open(os.path.join(ROOT, rel), "rb").read(3)
        check(raw != b"\xef\xbb\xbf", "%s 无 BOM" % rel)


def test_response_body_closed():
    """A-3：HTTP 响应体未关闭，长时间运行泄漏连接。"""
    print("\n== A-3 HTTP 响应体泄漏 ==")
    for rel, marker in [
        ("core/cdp.py", "/json/list"),
        ("client_cdp.py", "def _get"),
        ("orchestrator/adaptive.py", "/json/version"),
    ]:
        src = read(rel)
        i = src.find(marker)
        seg = src[i: i + 600] if i >= 0 else ""
        check("close()" in seg, "%s 响应体已关闭" % rel)


def test_concurrency_guard():
    """A-4：并发请求可绕过 running 检查，违反串行铁则。"""
    print("\n== A-4 并发守卫 ==")
    src = read("web/server.py")
    # action_run_account：检查与置位须在同一把锁内
    i = src.find("def action_run_account")
    seg = src[i: i + 1400]
    check(
        seg.find("with LOCK:") != -1
        and seg.find('ACCT_JOB.get("running")') > seg.find("with LOCK:"),
        "单账号：running 检查在锁内且先于置位",
    )
    # action_run_ui_code：等待旧任务退出须有超时
    j = src.find("def action_run_ui_code")
    seg2 = src[j: j + 1600]
    check("deadline" in seg2 and "time.time() < deadline" in seg2, "单任务：等待旧任务有超时上限")
    check("仍在停止中" in seg2, "单任务：超时后明确拒绝而非并发启动")


def test_no_dead_branch_confusion():
    """A-5：人工跳过分支为死代码，需显式标注。"""
    print("\n== A-5 死代码标注 ==")
    src = read("orchestrator/batch.py")
    check("死代码" in src, "batch.py 已标注 UNAUTOMATABLE_CODES 为死代码")
    i = src.find('if code in UNAUTOMATABLE_CODES')
    check(i == -1, "调度处不再以空常量作为唯一判断")
    check("_skip_codes()" in src, "实际判断走 config 动态读取")
    # 需求六三类理由齐全
    for code in ("wb_wechat_oa_subscribe_task", "Expert_Philanthropy", "black_cat"):
        check(code in src, "需求六保留 %s" % code)


def test_no_misleading_log():
    """A-6：日志文案与实现不一致。"""
    print("\n== A-6 文案一致性 ==")
    src = read("orchestrator/batch.py")
    check("跑完删掉" not in src, "删除误导文案已移除")
    i = src.find("创建 1 个自动化任务")
    check(i >= 0 and "清理闸" in src[i: i + 200], "改为说明由清理闸统一删除")


def test_delete_rule_preserved():
    """删除铁则：必须积分到账后才删，修复不得破坏该约束。"""
    print("\n== 删除铁则未被破坏 ==")
    src = read("missions/panels.py")
    check("keep=True" in src, "do_automation_1_and_clean 默认保留不删")
    b = read("orchestrator/batch.py")
    check("account_ready_for_cleanup" in b, "删除前有清理闸校验")
    check("积分已到账" in b or "积分/任务未确认完成" in b, "清理闸日志明示积分状态")


def test_all_modules_importable():
    """A-7：全项目可导入。"""
    print("\n== A-7 全项目导入 ==")
    import importlib

    mods = [
        "core.config", "core.api", "core.dom", "core.cdp", "core.logs",
        "core.logproto", "core.creds", "core.atrest", "core.notify", "core.toast",
        "engine.driver", "engine.tasks", "engine.switch", "engine.missions_api",
        "engine.active", "engine.connector_oauth",
        "missions.chat", "missions.panels", "missions.team", "missions.experts",
        "missions.scenes", "missions.skill", "missions.lighthouse_flow",
        "orchestrator.run_all", "orchestrator.batch", "orchestrator.claim",
        "orchestrator.adaptive",
    ]
    bad = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:
            bad.append("%s(%s)" % (m, type(e).__name__))
    check(not bad, "全部 %d 个模块导入成功%s" % (len(mods), ("：失败 " + ", ".join(bad)) if bad else ""))


def test_syntax_clean():
    """A-8：全项目编译无警告。"""
    print("\n== A-8 语法与警告 ==")
    import py_compile
    import tempfile
    import warnings

    tmp = tempfile.mkdtemp()
    bad = []
    n = 0
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in
                 {"__pycache__", "dist", ".workbuddy", ".ruff_cache",
                  ".dumate", ".tmp", "log-protocol-guardian", ".git", "tests"}]
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                n += 1
                with warnings.catch_warnings():
                    warnings.simplefilter("error", SyntaxWarning)
                    try:
                        py_compile.compile(p, doraise=True,
                                           cfile=os.path.join(tmp, "x.pyc"))
                    except Exception as e:
                        bad.append("%s(%s)" % (f, type(e).__name__))
    check(not bad, "编译 %d 个文件零失败零警告%s" % (n, ("：" + ", ".join(bad)) if bad else ""))


def main():
    print("=" * 62)
    print("全项目静态核验回归测试")
    print("=" * 62)
    test_hotkey_defined()
    test_no_bom()
    test_response_body_closed()
    test_concurrency_guard()
    test_no_dead_branch_confusion()
    test_no_misleading_log()
    test_delete_rule_preserved()
    test_all_modules_importable()
    test_syntax_clean()
    print("\n" + "=" * 62)
    if _fails:
        print("失败 %d 项：" % len(_fails))
        for f in _fails:
            print("  - %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())