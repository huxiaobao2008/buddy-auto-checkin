#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置与路径：项目内唯一的数据层定义（零第三方依赖）。

职责：
  - 项目路径常量（BASE_DIR / CONFIG_FILE / LOG_DIR / AUTH_DIR ...）
  - config.json 读写与默认值补全
  - 通用 JSON 读写与时间工具
  - token 脱敏

config.json 结构（与旧项目兼容）：
  accounts[]: {key, label, uid, nickname, cred_file, enabled, times[], tasks{}}
  schedule:   {trigger_mode, every_minutes, window_minutes, task_prefix}
  alert:      {desktop_notify, notify_on_success, webhook_url, alert_log}
  retry:      {attempts, backoff_seconds}
  paid_tasks: {codes[], keywords[]}   需付费任务（一票否决）
  skip_tasks: {codes[], keywords[]}   用户点名不执行的任务
  dev:        {clear_conversations_before_run: false}   开发期专用，交付版必须为 false
"""
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
RUNS_LOG = os.path.join(LOG_DIR, "runs.jsonl")
TASKS_LOG = os.path.join(LOG_DIR, "tasks.jsonl")
LATEST_JSON = os.path.join(LOG_DIR, "latest.json")
SUMMARY_TXT = os.path.join(LOG_DIR, "summary.txt")
TASK_STATE = os.path.join(LOG_DIR, "tasks_state.json")

# 跨进程「停止任务」信号文件：web 的停止按钮写它，各执行循环在检查点读它。
STOP_FILE = os.path.join(LOG_DIR, "STOP_ALL")

# WorkBuddy 客户端登录态（活文件）
AUTH_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "CodeBuddyExtension", "Data", "Public", "auth",
)
CURRENT_AUTH = os.path.join(AUTH_DIR, "workbuddy-desktop.info")
# 我们自己的切号备份也必须纳入登录态扫描（否则被切走一次的账号永远扫不到）
AUTH_BACKUP_DIR = os.path.join(DATA_DIR, "auth_backups")

CLIENT_EXE_CANDIDATES = [
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "WorkBuddy", "WorkBuddy.exe"),
    r"D:\Program Files\WorkBuddy\WorkBuddy.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WorkBuddy", "WorkBuddy.exe"),
]

CDP_PORT = int(os.environ.get("WORKBUDDY_CDP_PORT", "9222"))

# 任务开关全集（新账号/老配置缺项时按默认 True 补全）
DEFAULT_TASKS = {
    "checkin": True,               # 每日签到
    "growth_info": True,           # 成长伙伴状态
    "growth_travel_claim": True,   # 出行到达奖励
    "growth_tasks": True,          # 成长任务清单（接取/领取）
    "growth_active": True,         # 成长热度点亮
    "buddy_blindbox": True,        # 开盲盒
    "buddy_travel_dispatch": True, # 派猫猫旅行
}


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def mask(token):
    """token 脱敏：任何输出里都不允许出现完整 token。"""
    if not token:
        return "<empty>"
    return "%s...%s" % (str(token)[:6], str(token)[-4:])


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_FILE)
    if not cfg:
        print("[配置错误] 找不到或无法解析 %s" % CONFIG_FILE)
        sys.exit(1)
    cfg.setdefault("schedule", {})
    cfg.setdefault("alert", {})
    cfg.setdefault("retry", {"attempts": 2, "backoff_seconds": 2})
    cfg.setdefault("accounts", [])
    cfg.setdefault("paid_tasks", {})
    cfg.setdefault("skip_tasks", {})
    cfg.setdefault("dev", {})
    changed = False
    for a in cfg["accounts"]:
        a.setdefault("tasks", {})
        for k, v in DEFAULT_TASKS.items():
            if k not in a["tasks"]:
                a["tasks"][k] = v
                changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg):
    save_json(CONFIG_FILE, cfg)


def find_account(cfg, key):
    return next((a for a in (cfg.get("accounts") or []) if a.get("key") == key), None)


def enabled_accounts(cfg):
    return [a for a in (cfg.get("accounts") or []) if a.get("enabled", True)]


def dev_clear_conversations(cfg=None):
    """开发期专用开关：跑任务前清空各账号会话清单。

    ⚠️ 交付版必须为 False —— 它会删除使用者客户端里的已有会话。
    """
    cfg = cfg or load_config()
    return bool((cfg.get("dev") or {}).get("clear_conversations_before_run", False))


def stop_requested():
    try:
        return os.path.exists(STOP_FILE)
    except Exception:
        return False


def write_stop():
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(STOP_FILE, "w", encoding="utf-8") as f:
        f.write(now_str())


def clear_stop():
    try:
        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
    except Exception:
        pass


def auth_scan_dirs():
    """登录态扫描范围：客户端活文件目录 + 我方切号备份目录 + 客户端自身 backup。"""
    dirs = [AUTH_DIR]
    for d in (AUTH_BACKUP_DIR, os.path.join(AUTH_DIR, "backup")):
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs
