#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构化日志：runs.jsonl（账号级）/ tasks.jsonl（任务级）/ 状态回写。"""
import json
import os

from . import config as C


def append_run(record):
    os.makedirs(C.LOG_DIR, exist_ok=True)
    with open(C.RUNS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_step(account_key, nickname, task_code, task_title, step, status, reason, extra=None):
    rec = {
        "ts": C.now_str(), "date": C.today_str(),
        "account": account_key, "nickname": nickname,
        "task": task_code, "task_title": task_title, "step": step,
        "status": status, "reason": reason,
    }
    if extra:
        rec["extra"] = extra
    os.makedirs(C.LOG_DIR, exist_ok=True)
    with open(C.TASKS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def save_task_state(state):
    os.makedirs(C.LOG_DIR, exist_ok=True)
    C.save_json(C.TASK_STATE, {"ts": C.now_str(), "accounts": state})
