#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接口原语：WorkBuddy 官方 API 调用（零第三方依赖，标准库 urllib）。

接口清单（已从客户端 app.asar + 官网 usercenter 前端产物双向核实，非猜测）：
  POST /v2/billing/meter/checkin-activity-status   签到状态（total_credits / streak）
  POST /v2/billing/meter/daily-checkin             领取签到（已签返回 code=10001）
  GET  /v2/activity/growth/tasks                   成长任务列表
  POST /activity/growth/tasks/accept               接取任务 {task_codes:[...]}
  POST /activity/growth/tasks/{code}/claim         领取任务奖励
  GET  /v2/activity/growth/profile                 成长档案
  GET  /v2/activity/growth/buddy/info              成长伙伴
  POST /v2/activity/growth/buddy/travel/claim      出行到达奖励
  GET  /billing/meter/get-user-resource-summary    账户剩余额度（注意：不带 /v2）

成长中心（盲盒/能量/猫猫旅行）接口主机特殊：
  必须用 www.workbuddy.cn 且不带 /v2 前缀；codebuddy.cn 亦可；auth.domain 兜底。
"""
import json
import time
import urllib.error
import urllib.request

from . import config as C

PATH_CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"
PATH_CHECKIN_CLAIM = "/v2/billing/meter/daily-checkin"
PATH_GROWTH_TASKS = "/v2/activity/growth/tasks"
PATH_GROWTH_ACCEPT = "/activity/growth/tasks/accept"
PATH_GROWTH_CLAIM = "/activity/growth/tasks/{code}/claim"
PATH_GROWTH_PROFILE = "/v2/activity/growth/profile"
PATH_GROWTH_INFO = "/v2/activity/growth/buddy/info"
PATH_GROWTH_TRAVEL_CLAIM = "/v2/activity/growth/buddy/travel/claim"
PATH_RESOURCE_SUMMARY = "/billing/meter/get-user-resource-summary"

GROWTH_HOSTS = ["https://www.workbuddy.cn", "https://www.codebuddy.cn"]


def api(cred, path, method="POST", body=None, timeout=20):
    """主接口调用（auth.domain 主机）。返回 (http_code, json_obj)；网络异常返回 (-1, {error})。"""
    auth = cred["auth"]
    acct = cred.get("account") or {}
    url = "https://%s%s" % (auth["domain"], path)
    payload = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(url, method=method, data=payload)
    req.add_header("Authorization", "Bearer %s" % auth["accessToken"])
    if auth.get("refreshToken"):
        req.add_header("X-Refresh-Token", auth["refreshToken"])
    if acct.get("uid"):
        req.add_header("X-User-Id", acct["uid"])
    if auth.get("domain"):
        req.add_header("X-Domain", auth["domain"])
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": str(e)}


def auth_error(http_code, data):
    return http_code in (401, 403) or (isinstance(data, dict) and data.get("code") in (401, 403))


def growth_api(cred, path, method="GET", body=None, timeout=25):
    """成长中心接口（盲盒/能量/猫猫旅行）：多主机兜底，无 /v2 前缀。"""
    auth = cred.get("auth") or {}
    token = auth.get("accessToken")
    if not token:
        raise ValueError("凭证里没有 accessToken")
    hosts = list(GROWTH_HOSTS) + ["https://" + str(auth.get("domain") or "www.workbuddy.cn")]
    last = ""
    for host in hosts:
        url = host + path
        payload = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        try:
            req = urllib.request.Request(url, method=method, data=payload)
            req.add_header("Authorization", "Bearer " + token)
            req.add_header("Content-Type", "application/json")
            req.add_header("x-codebuddy-request", "1")
            req.add_header("x-client-platform", "web")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            last = "%s(%s)" % (type(e).__name__, str(e)[:80])
    raise RuntimeError("成长接口全部主机失败：%s" % last)


def retry(fn, attempts=2, backoff=2):
    """网络异常(-1)与 5xx 才重试；业务错误（4xx 带 msg）不重试，避免重复提交/扣费。"""
    last = (-1, {"error": "未执行"})
    for i in range(max(1, attempts)):
        code, res = fn()
        retryable = (code == -1) or (isinstance(code, int) and code >= 500)
        if not retryable:
            return code, res
        last = (code, res)
        if i < attempts - 1:
            time.sleep(backoff * (i + 1))
    return last


def retry_params(cfg):
    r = (cfg or {}).get("retry") or {}
    return int(r.get("attempts", 2)), int(r.get("backoff_seconds", 2))
