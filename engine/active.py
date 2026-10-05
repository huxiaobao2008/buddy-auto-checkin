#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成长热度点亮：今日未活跃时发起一次云端会话（纯 HTTP/SSE + ACP，不碰客户端）。

链路（5 步，照搬 WorkDaddy 已上线实现，非猜测）：
  ① POST {host}/console/as/conversations/                建会话，拿 conversationId + session
  ② GET  {host}/console/as/conversations/{id}/session    缺 link/token 时补取 ACP 凭证
  ③ GET  <link 归一化为 .../api/v1/acp>  Accept: text/event-stream → 取 acp-connection-id
  ④ POST 同端点 + acp-connection-id，JSON-RPC 2.0：initialize → session/prompt
  ⑤ DELETE 同端点 + acp-connection-id（尽力而为）

活跃校验：
  GET {host}/activity/growth/heatmap → data.today.is_active
  GET {host}/activity/growth/streak  → data.streak.days

边界：只负责「活跃/连续天数」，不推进 18 项成长任务进度（那些需要真实产品交互）。
"""
import base64
import http.client
import json
import threading
import time
from urllib.parse import urlparse

TIMEOUT = 30
DEFAULT_PROMPT = "你好"
# 模型回退链：免费额度优先；某个模型建会话失败（如额度用尽）自动换下一个。
MODEL_FALLBACK = ["deepseek-r1", "agnes 2.5"]
DEFAULT_MODEL = MODEL_FALLBACK[0]

OK = "success"
SKIP = "skip"
FAIL = "fail"


def api_host(cred):
    d = ((cred.get("auth") or {}).get("domain") or "").strip().rstrip("/")
    if not d:
        raise ValueError("凭证里没有 auth.domain，无法确定 API 主机")
    return d if d.startswith("http") else "https://" + d


def _headers(host, token, extra=None):
    h = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "x-codebuddy-request": "1",
        "x-client-platform": "web",
        "origin": host,
        "referer": host + "/",
        "authorization": "Bearer " + str(token or ""),
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    if extra:
        h.update(extra)
    return h


def _conn(url):
    u = urlparse(url)
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.netloc, timeout=TIMEOUT)
    return http.client.HTTPConnection(u.netloc, timeout=TIMEOUT)


def _path_of(url):
    u = urlparse(url)
    return (u.path or "/") + (("?" + u.query) if u.query else "")


def request(host, method, path, token, body=None, extra=None, stream=False):
    url = host + path
    conn = _conn(url)
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    headers = _headers(host, token, extra)
    try:
        conn.putrequest(method, _path_of(url), skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        if payload is not None:
            conn.putheader("Content-Length", str(len(payload)))
        conn.endheaders()
        if payload is not None:
            conn.send(payload)
        resp = conn.getresponse()
        if stream:
            return resp.status, dict(resp.getheaders()), "", conn, resp
        text = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, dict(resp.getheaders()), text, None, None
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def _json_body(text):
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def _unwrap(status, text, label):
    payload = _json_body(text)
    if status < 200 or status >= 300:
        msg = payload.get("msg") or payload.get("message") or payload.get("error") or ""
        raise RuntimeError("%s HTTP %s%s" % (label, status, ("：" + str(msg)) if msg else ""))
    code = payload.get("code")
    if code is not None and code != 0:
        raise RuntimeError("%s code=%s %s" % (label, code, payload.get("msg") or payload.get("message") or ""))
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def resolve_acp_endpoint(link):
    v = str(link or "").strip().replace("http://", "https://").rstrip("/")
    if not v:
        return ""
    try:
        u = urlparse(v)
        p = u.path.rstrip("/")
        if not p.endswith("/api/v1/acp") and not p.endswith("/acp"):
            p = p + "/api/v1/acp"
        q = ("?" + u.query) if u.query else ""
        return "%s://%s%s%s" % (u.scheme, u.netloc, p, q)
    except Exception:
        return v


class AcpConnection:
    """ACP 连接：GET 开 SSE 拿 connection-id，POST 走 JSON-RPC，DELETE 收尾。
    RPC 回包可能在任一条流上，GET 流后台线程持续读，POST 流主线程读到命中为止。"""

    def __init__(self, host, endpoint, runtime_token):
        self.host = host
        self.endpoint = endpoint
        self.token = runtime_token
        self.connection_id = ""
        self._pending = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._conn = None
        self._resp = None
        self._reader = None
        self.events = []
        self.error = ""

    def connect(self):
        url = self.endpoint
        conn = _conn(url)
        headers = _headers(self.host, self.token, {"accept": "text/event-stream"})
        conn.putrequest("GET", _path_of(url), skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        if resp.status < 200 or resp.status >= 300:
            body = resp.read().decode("utf-8", "replace")[:200]
            conn.close()
            raise RuntimeError("ACP 连接 HTTP %s%s" % (resp.status, ("：" + body) if body else ""))
        self.connection_id = (resp.getheader("acp-connection-id") or "").strip()
        if not self.connection_id:
            conn.close()
            raise RuntimeError("ACP 连接响应里没有 acp-connection-id")
        self._conn, self._resp = conn, resp
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return self.connection_id

    def _read_loop(self):
        try:
            while not self._stop.is_set():
                line = self._resp.readline()
                if not line:
                    break
                self._feed(line.decode("utf-8", "replace"))
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)

    def _feed(self, line):
        rpc = _sse_line_to_rpc(line)
        if rpc is not None:
            self._deliver(rpc)

    def _deliver(self, payload):
        if not isinstance(payload, dict):
            return
        rid = payload.get("id")
        if rid is None:
            return
        with self._lock:
            slot = self._pending.get(rid)
        if not slot:
            return
        slot["result"] = payload
        slot["event"].set()

    def rpc(self, method, params, wait=None):
        rid = self._next_id()
        slot = {"event": threading.Event(), "result": None}
        with self._lock:
            self._pending[rid] = slot
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        body = json.dumps(msg).encode("utf-8")
        url = self.endpoint
        conn = _conn(url)
        headers = _headers(self.host, self.token, {
            "accept": "application/json, text/event-stream",
            "acp-connection-id": self.connection_id,
        })
        conn.putrequest("POST", _path_of(url), skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        conn.send(body)
        resp = conn.getresponse()
        ctype = (resp.getheader("content-type") or "").lower()
        deadline = time.time() + (wait or TIMEOUT)
        if resp.status < 200 or resp.status >= 300:
            text = resp.read().decode("utf-8", "replace")[:200]
            conn.close()
            raise RuntimeError("ACP %s HTTP %s%s" % (method, resp.status, ("：" + text) if text else ""))
        try:
            if "application/json" in ctype:
                payload = _json_body(resp.read().decode("utf-8", "replace"))
                if payload.get("jsonrpc"):
                    self._deliver(payload)
            else:
                while time.time() < deadline:
                    line = resp.readline()
                    if not line:
                        break
                    self._feed(line.decode("utf-8", "replace"))
                    if slot["event"].is_set():
                        break
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not slot["event"].wait(max(0.1, deadline - time.time())):
            raise RuntimeError("ACP %s 等待响应超时" % method)
        out = slot["result"] or {}
        if out.get("error"):
            raise RuntimeError("ACP %s 失败：%s" % (method, (out.get("error") or {}).get("message")))
        return out.get("result") or {}

    def _next_id(self):
        with self._lock:
            self._seq = getattr(self, "_seq", 0) + 1
            return self._seq

    def close(self):
        self._stop.set()
        try:
            if self._reader and self._reader.is_alive():
                self._reader.join(timeout=5.0)
        except Exception:
            pass
        try:
            if self.connection_id:
                request(self.host, "DELETE", self.endpoint, self.token,
                        extra={"acp-connection-id": self.connection_id})
        except Exception:
            pass
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass


def _sse_line_to_rpc(line):
    s = line.strip()
    if not s or s.startswith(":"):
        return None
    if s.startswith("data:"):
        s = s[5:].strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) and obj.get("jsonrpc") else None


def activate(cred, prompt=None, model=None, purpose="growth", timeout=TIMEOUT,
             session_timeout=45, log=None):
    """用该账号 token 起一次独立云端会话，达到「今日活跃」。"""
    say = log or (lambda *a: None)
    host = api_host(cred)
    token = (cred.get("auth") or {}).get("accessToken")
    if not token:
        return {"ok": False, "error": "凭证里没有 accessToken"}
    prompt = (prompt or DEFAULT_PROMPT)[:2000 if purpose == "completion-report" else 100]
    model = model or DEFAULT_MODEL
    plugins = [] if purpose == "completion-report" else [
        {"name": "weixinpay", "marketplace": "codebuddy-builtin"}]

    t0 = time.time()
    out = {"ok": False, "host": host, "prompt": prompt, "model": model, "steps": []}
    acp = None
    try:
        created = None
        chain = [model] if model else list(MODEL_FALLBACK)
        _last_err = None
        for _m in chain:
            try:
                st, _, tx, _, _ = request(host, "POST", "/console/as/conversations/", token,
                                          body={"prompt": prompt, "model": _m, "plugins": plugins})
                created = _unwrap(st, tx, "建会话")
                out["model"] = _m
                break
            except Exception as e:
                _last_err = e
                out["steps"].append("create(%s) → %s" % (_m, str(e)[:80]))
        if created is None:
            raise RuntimeError("所有候选模型建会话失败（%s）：%s" % (chain, _last_err))
        out["steps"].append("create → ok")
        cid = str(created.get("id") or created.get("conversationId")
                  or ((created.get("info") or {}).get("id")) or "")
        if not cid:
            raise RuntimeError("建会话响应缺少 conversation id")
        out["conversation_id"] = cid

        sess = created.get("session") if isinstance(created.get("session"), dict) else None
        if not sess or not sess.get("link") or not sess.get("token"):
            st, _, tx, _, _ = request(host, "GET", "/console/as/conversations/%s/session" % cid, token)
            sess = _unwrap(st, tx, "取会话凭证")
        out["steps"].append("session → ok")

        endpoint = resolve_acp_endpoint(sess.get("link") or sess.get("endpoint"))
        runtime_token = str(sess.get("token") or sess.get("accessToken") or "")
        session_id = str(sess.get("sessionId") or sess.get("session_id") or "")
        if not endpoint or not runtime_token or not session_id:
            raise RuntimeError("会话响应缺少 ACP 连接凭证（link/token/sessionId）")
        out["acp_endpoint"] = endpoint
        out["session_id"] = session_id

        acp = AcpConnection(host, endpoint, runtime_token)
        acp.connect()
        out["steps"].append("acp-connect → ok")
        out["connection_id"] = acp.connection_id

        acp.rpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "buddy-auto-v2-" + purpose, "version": "1.0.0"},
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "_meta": {"codebuddy.ai": {"question": False, "terminalOutput": False}},
            },
        }, wait=timeout)
        out["steps"].append("initialize → ok")

        res = acp.rpc("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": prompt}],
        }, wait=session_timeout)
        stop = (res or {}).get("stopReason")
        out["steps"].append("session/prompt → ok（stopReason=%s）" % stop)
        out["stop_reason"] = stop
        out["ok"] = True
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        say("  激活失败：%s" % out["error"])
    finally:
        if acp:
            acp.close()
        out["seconds"] = round(time.time() - t0, 1)
    return out


def today_active(cred, log=None):
    host = api_host(cred)
    token = (cred.get("auth") or {}).get("accessToken")
    try:
        st, _, tx, _, _ = request(host, "GET", "/activity/growth/heatmap", token, extra={
            "accept": "application/json, text/plain, */*",
            "referer": host + "/profile/growth-center",
        })
        d = _unwrap(st, tx, "成长活跃查询")
        today = d.get("today") if isinstance(d, dict) else None
        if not isinstance(today, dict):
            return {"ok": False, "error": "成长活跃接口缺少 data.today"}
        return {"ok": True, "is_active": bool(today.get("is_active")),
                "date": today.get("date"), "score": today.get("score"),
                "status_text": today.get("status_text") or ""}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def streak(cred, log=None):
    host = api_host(cred)
    token = (cred.get("auth") or {}).get("accessToken")
    try:
        st, _, tx, _, _ = request(host, "GET", "/activity/growth/streak", token, extra={
            "referer": host + "/profile/growth-center",
        })
        d = _unwrap(st, tx, "连续天数查询")
        days = ((d or {}).get("streak") or {}).get("days")
        if not isinstance(days, int) or days < 0:
            return {"ok": False, "error": "成长活跃接口缺少有效连续天数"}
        return {"ok": True, "days": days}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def task_growth_active(cred, account=None, check_only=False):
    """成长热度：今日未活跃时发起一次云端会话点亮热力墙。"""
    act = today_active(cred)
    if not act.get("ok"):
        return {"status": FAIL, "code": "api_error",
                "reason": "成长热度查询失败：%s" % act.get("error")}
    st0 = streak(cred)
    days0 = st0.get("days") if st0.get("ok") else "?"
    score0 = act.get("score")

    if act.get("is_active"):
        return {"status": SKIP, "code": "already_active",
                "reason": "今日已活跃（连签 %s 天，热度分 %s），无需点亮" % (days0, score0),
                "data": {"streak": days0, "score": score0}}
    if check_only:
        return {"status": SKIP, "code": "check_only",
                "reason": "今日未活跃（连签 %s 天），仅查询模式未点亮" % days0,
                "data": {"streak": days0, "score": score0}}

    res = activate(cred)
    if not res.get("ok"):
        return {"status": FAIL, "code": "activate_failed",
                "reason": "云端会话失败：%s" % res.get("error")}

    act2, days2 = act, days0
    for _ in range(3):
        time.sleep(1.5)
        act2 = today_active(cred)
        if act2.get("is_active"):
            days2 = (streak(cred) or {}).get("days", days0)
            break
    if not act2.get("is_active"):
        return {"status": FAIL, "code": "not_flagged",
                "reason": "会话已发出（%ss）但服务端未记为活跃" % res.get("seconds")}
    return {"status": OK, "code": "activated",
            "reason": "已点亮今日活跃，连签 %s 天（热度分 %s）" % (days2, act2.get("score")),
            "data": {"streak": days2, "score": act2.get("score"),
                     "seconds": res.get("seconds"), "stop_reason": res.get("stop_reason")}}
