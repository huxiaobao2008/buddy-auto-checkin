# -*- coding: utf-8 -*-
"""构建 buddy-auto-v2 的开源脱敏包。

原则（用户 2026-09-25 明确）：
  1) 真实项目保持原样 —— 手机号**不做任何加密/变形**，否则凭证失效、账号无法续期。
  2) 开源副本里把真实手机号替换成占位符（脱敏），不泄露隐私。
  3) 绝不打包：凭证(accounts/)、日志(logs/)、运行状态(data/,.dumate/)、真实配置(config.json)。

用法：
  python tools/build_opensource.py
输出：
  dist/buddy-auto-v2-opensource/   目录
  dist/buddy-auto-v2-opensource.zip
"""

import os
import re
import shutil
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
OUT = os.path.join(DIST, "buddy-auto-v2-opensource")

# 顶层随行文件
INCLUDE_FILES = [
    "auto_buddy.py",
    "launcher.py",
    "client_cdp.py",
    "check_python.py",
    "README.md",
    "Buddy工作台一键启动.cmd",
    "restart-cdp.cmd",
]
# 随行目录
INCLUDE_DIRS = [
    "core",
    "engine",
    "missions",
    "orchestrator",
    "web",
    "docs",
    "skill",
    "tests",
]
# 目录/文件级排除（含一切私密与运行时产物）
EXCLUDE_NAMES = {
    ".git",
    ".workbuddy",
    "__pycache__",
    "_trash",
    "accounts",
    "logs",
    "data",
    ".dumate",
    "dist",
    "config.json",  # 真实配置（含 19 个手机号）
    "web/ui.html.broken",  # 损坏备份
    "stop.flag",
    "nul",
}
EXCLUDE_EXTS = {".pyc", ".png", ".zip", ".bak", ".key", ".flag"}
# 需要脱敏的扩展名
TEXT_EXTS = {".py", ".md", ".json", ".html", ".cmd", ".bat", ".txt", ".example.json"}

PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
PH_PLACEHOLDER = "<手机号已脱敏>"
TK_PLACEHOLDER = "<TOKEN已移除>"


def sanitize_text(s):
    s = PHONE.sub(PH_PLACEHOLDER, s)
    s = JWT.sub(TK_PLACEHOLDER, s)
    return s


def should_skip(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_NAMES for p in parts):
        return True
    if rel.replace("\\", "/") in EXCLUDE_NAMES:
        return True
    if os.path.splitext(parts[-1])[1].lower() in EXCLUDE_EXTS:
        return True
    # 备份/临时文件（含 panels.py.bak.2026-09-24 这类「后缀是日期」的漏网文件）
    if ".bak" in parts[-1].lower() or parts[-1].lower().endswith(
        (".orig", ".tmp", ".old")
    ):
        return True
    return False


def copy_tree():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT, exist_ok=True)
    copied = 0
    missing = []

    for f in INCLUDE_FILES:
        src = os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(OUT, f))
            copied += 1
        else:
            missing.append(f)

    for d in INCLUDE_DIRS:
        src_dir = os.path.join(ROOT, d)
        if not os.path.isdir(src_dir):
            missing.append(d + "/")
            continue
        for dp, dn, fn in os.walk(src_dir):
            rel_dir = os.path.relpath(dp, ROOT)
            dn[:] = [x for x in dn if not should_skip(os.path.join(rel_dir, x))]
            for f in fn:
                rel = os.path.relpath(os.path.join(dp, f), ROOT)
                if should_skip(rel):
                    continue
                dst = os.path.join(OUT, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(dp, f), dst)
                copied += 1
    return copied, missing


def sanitize_all():
    changed = 0
    for dp, dn, fn in os.walk(OUT):
        for f in fn:
            p = os.path.join(dp, f)
            ext = os.path.splitext(f)[1].lower()
            if ext not in TEXT_EXTS and not f.endswith(".example.json"):
                continue
            try:
                s = open(p, encoding="utf-8").read()
            except Exception:
                continue
            new = sanitize_text(s)
            if new != s:
                open(p, "w", encoding="utf-8").write(new)
                changed += 1
    return changed


def verify():
    problems = []
    for dp, dn, fn in os.walk(OUT):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                s = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            ph = PHONE.findall(s)
            jw = JWT.findall(s)
            if ph:
                problems.append(("手机号", os.path.relpath(p, OUT), len(set(ph))))
            if jw:
                problems.append(("token", os.path.relpath(p, OUT), len(set(jw))))
            if "accounts" in os.path.relpath(p, OUT).lower().split(os.sep)[:1]:
                problems.append(("凭证目录", os.path.relpath(p, OUT), 1))
    return problems


def main():
    n, missing = copy_tree()
    c = sanitize_all()
    problems = verify()

    # 用脱敏版示例配置替代真实 config.json
    example = os.path.join(ROOT, "skill", "config.example.json")
    if os.path.exists(example):
        shutil.copy2(example, os.path.join(OUT, "config.example.json"))

    print("=" * 60)
    print("开源脱敏包构建完成")
    print("  输出目录: %s" % OUT)
    print("  复制文件: %d 个" % n)
    print("  脱敏处理: %d 个文件" % c)
    if missing:
        print("  告警（未随包复制）: %s" % ", ".join(sorted(missing)))
    print("-" * 60)
    if problems:
        print("  ❌ 仍有问题：")
        for kind, f, cnt in problems[:20]:
            print("     [%s] %s (%d)" % (kind, f, cnt))
    else:
        print("  ✅ 校验通过：无手机号、无 token、无凭证文件")

    zp = os.path.join(DIST, "buddy-auto-v2-opensource.zip")
    if os.path.exists(zp):
        os.remove(zp)
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dn, fn in os.walk(OUT):
            for f in fn:
                p = os.path.join(dp, f)
                z.write(p, os.path.relpath(p, DIST))
    print("  压缩包: %s (%.1f KB)" % (zp, os.path.getsize(zp) / 1024.0))
    print("=" * 60)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
