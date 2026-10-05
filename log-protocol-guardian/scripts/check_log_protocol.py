#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""日志协议一致性校验器（零依赖，仅标准库）。

用途
----
扫描目标工程，找出「绕过日志协议直接拼装日志行」以及
「解析侧正则未行首锚定」两类缺陷，输出可执行的修复清单。

用法
----
    python check_log_protocol.py <项目根目录> [--protocol <logproto.py路径>]

退出码
------
    0 = 未发现问题
    1 = 发现问题（数量见报告）
    2 = 参数错误

设计约束
--------
- 只用标准库，可在受限沙箱中运行
- 只读扫描，不修改任何文件
- 退出码可用于 CI 集成
"""

import argparse
import os
import re
import sys

# 业务代码中不应再出现的本地时间戳拼装（协议模块自身除外）
TS_PATTERNS = [
    re.compile(r'strftime\(\s*["\']%H:%M:%S["\']\s*\)'),
    re.compile(r'now_str\(\)'),
]

# 仅当时间戳出现在「日志行拼装」语境中才算缺陷。
# 判定依据：同一行内同时出现日志前缀字面量（如 "["、"[%s]"）与时间戳调用。
# 状态字段赋值（res["ts"] = now_str()）不属于日志行拼装，不报。
LOG_LINE_CTX = re.compile(r'\[\s*%s\s*\]|"\[%s\]|\[\{\}?\]|line\s*=')

# 解析侧正则：命中且不以 ^ 开头即为缺陷。
# 仅针对「协议三段式前缀」特征（[HH:MM:SS][area]），避免误报通用正则。
PARSE_HINTS = [
    re.compile(r'match\(\s*/(?!\^)[^/\n]*\\\[(?:\\d|d)'),
    re.compile(r'test\(\s*/(?!\^)[^/\n]*\\\[(?:\\d|d)'),
]

# 关键词猜测阶段：仅当整行只有 includes 拼接、无协议分支时才算缺陷。
# 保留协议分支之后的关键词兜底是允许的（用于兼容无前缀历史行）。
KEYWORD_ONLY_STAGE = re.compile(
    r'(?:^|\|\|)\s*\w+\.includes\(\s*["\'][^"\']*(?:阶段[一二三]|管线)[^"\']*["\']\s*\)'
)

SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".ruff_cache", ".dumate",
    ".tmp", ".workbuddy", "dist", "backup", ".trash",
}

EXTS = {".py", ".html", ".js"}

# 文档目录：内含缺陷示例代码片段（如报告里引用的错误写法），
# 不是可执行代码，扫描会产生误报。
DOC_DIRS = {"docs", "doc", "references", "assets", "skill-build", "skill"}


def is_doc_path(path):
    """判断是否位于文档/示例目录（其中代码片段仅用于说明，不应报警）。"""
    parts = set(os.path.normpath(path).split(os.sep))
    return bool(parts & DOC_DIRS)


def iter_source_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in EXTS:
                yield os.path.join(dirpath, fn)


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def is_protocol_module(path, protocol_path):
    if protocol_path:
        try:
            return os.path.samefile(path, protocol_path)
        except OSError:
            return False
    return os.path.basename(path) == "logproto.py"


# 独立日志文件（无 area 语义、不被前端按区域解析）——属正常用法，不报缺陷。
# 判据：文件内不出现任何 area 标记或协议模块调用。
AREA_LITERAL = re.compile(r'AREA_|format_line|LP\.|\[\{0,1\}[a-z]+(?::%s)?\]' % r'%s')


def check_timestamp_bypass(root, protocol_path):
    """查找绕过协议模块的日志行拼装。

    仅检查「面向解析侧的日志」。独立日志文件（如启动器日志、告警日志）
    无area 语义、不被前端分区解析，不属于协议管辖范围，不报缺陷。
    """
    issues = []
    for path in iter_source_files(root):
        if is_protocol_module(path, protocol_path) or is_doc_path(path):
            continue
        text = read(path)
        # 该文件是否参与区域化日志协议
        if not AREA_LITERAL.search(text):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("*"):
                continue
            if not LOG_LINE_CTX.search(line):
                continue  # 非日志行拼装语境
            for pat in TS_PATTERNS:
                if pat.search(line):
                    issues.append(
                        (path, lineno, "TS-BYPASS", stripped[:90],
                         "本地拼装日志行时间戳，应改用协议模块 format_line()")
                    )
                    break
    return issues


def check_parse_anchor(root):
    """查找未行首锚定的协议前缀正则。"""
    issues = []
    for path in iter_source_files(root):
        if os.path.basename(path) == "logproto.py" or is_doc_path(path):
            continue
        text = read(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            for pat in PARSE_HINTS:
                if pat.search(line):
                    issues.append(
                        (path, lineno, "PARSE-NOANCHOR", stripped[:90],
                         "协议前缀正则未行首锚定，应加 ^ 并显式捕获 area 与账号")
                    )
                    break
    return issues


def check_state_guess(root):
    """查找用关键词猜测运行状态的实现。"""
    issues = []
    guess = re.compile(r'if\s+["\'][^"\']*(?:停止|完成|成功)[^"\']*["\']\s+in\s+\w+')
    for path in iter_source_files(root):
        if os.path.basename(path) == "logproto.py" or is_doc_path(path):
            continue
        text = read(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            if guess.search(line):
                issues.append(
                    (path, lineno, "STATE-GUESS", line.strip()[:90],
                     "用关键词猜测运行状态，应按行首 area 判定并让完成标记优先")
                )
    return issues


def main():
    ap = argparse.ArgumentParser(description="日志协议一致性校验器")
    ap.add_argument("root", help="项目根目录")
    ap.add_argument("--protocol", default="", help="协议模块路径（默认按文件名 logproto.py 识别）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("错误：目录不存在 %s" % root, file=sys.stderr)
        return 2
    protocol = os.path.abspath(args.protocol) if args.protocol else ""

    groups = [
        ("时间戳旁路", check_timestamp_bypass(root, protocol)),
        ("解析未锚定", check_parse_anchor(root)),
        ("状态猜测", check_state_guess(root)),
    ]

    total = 0
    print("=" * 68)
    print("日志协议一致性校验：%s" % root)
    print("=" * 68)

    for title, issues in groups:
        print("\n【%s】%d 项" % (title, len(issues)))
        for path, lineno, code, snippet, advice in issues:
            total += 1
            try:
                rel = os.path.relpath(path, root)
            except ValueError:
                rel = path
            print("  %-14s %s:%d" % (code, rel, lineno))
            print("      代码：%s" % snippet)
            print("      建议：%s" % advice)

    print("\n" + "-" * 68)
    if total == 0:
        print("结论：未发现绕过协议或未锚定的解析实现。")
        return 0
    print("结论：发现 %d 处待修复，请按建议逐项处理。" % total)
    return 1


if __name__ == "__main__":
    sys.exit(main())