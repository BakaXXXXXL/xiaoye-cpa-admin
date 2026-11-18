#!/usr/bin/env python3
"""CPA credits bridge — 把 Codex/ChatGPT 的额度窗口转成 RikkaHub 可读的 /credits JSON。

监听 127.0.0.1:8791，由 Caddy 在 /cpa/v1/credits 暴露。
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from policy import credits_keys

MGMT = "http://127.0.0.1:8317"
KEY = open("/root/.cpa-mgmt-key").read().strip()
CACHE_TTL = 60
_cache = {"t": 0.0, "data": None}


def cpa_api_keys():
    return credits_keys()



def mgmt(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        MGMT + path,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def codex_auth_index():
    d = mgmt("/v8/management/credentials")
    for f in d.get("files", []):
        if f.get("provider") == "codex" and not f.get("disabled"):
            return f.get("auth_index")
    return None


def fetch_usage(idx):
    r = mgmt(
        "/v8/management/requests/api-call",
        "POST",
        {
            "auth_index": idx,
            "method": "GET",
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "header": {"Authorization": "Bearer $TOKEN$"},
        },
    )
    body = r.get("body") or "{}"
    return r.get("status_code"), json.loads(body)


def build():
    now = time.time()
    if _cache["data"] and now - _cache["t"] < CACHE_TTL:
        return _cache["data"]

    payload = {
        "object": "credits",
        "data": {
            "total_usage": 0,
            "used_percent": 0,
            "remaining_percent": 100,
            "weekly_used_percent": 0,
            "weekly_remaining_percent": 100,
            "reset_in_minutes": 0,
            "weekly_reset_in_hours": 0,
            "plan": "unknown",
            "credits_balance": 0,
            "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        },
    }
    try:
        idx = codex_auth_index()
        if not idx:
            payload["data"]["error"] = "no codex credential"
            return payload
        code, j = fetch_usage(idx)
        if code != 200:
            payload["data"]["error"] = f"upstream http {code}"
            return payload
        rl = j.get("rate_limit") or {}
        pw = rl.get("primary_window") or {}
        sw = rl.get("secondary_window") or {}
        used = float(pw.get("used_percent") or 0)
        wused = float(sw.get("used_percent") or 0)
        d = payload["data"]
        d["total_usage"] = round(used, 1)
        d["used_percent"] = round(used, 1)
        d["remaining_percent"] = round(max(0.0, 100.0 - used), 1)
        d["weekly_used_percent"] = round(wused, 1)
        d["weekly_remaining_percent"] = round(max(0.0, 100.0 - wused), 1)
        # RikkaHub 自定义额度查询只会显示成「余额」字符串，数字会被格式化成两位小数；
        # 字符串则原样显示，所以把两个窗口拼成一行给它。
        d["display"] = f"5h {used:.0f}% · 1w {wused:.0f}%"
        d["display_remaining"] = f"5h {100 - used:.0f}% · 1w {100 - wused:.0f}%"
        d["display_cn"] = f"5小时已用 {used:.0f}% · 本周已用 {wused:.0f}%"
        d["total_usage"] = d["display"]
        d["reset_in_minutes"] = int(round((pw.get("reset_after_seconds") or 0) / 60))
        d["weekly_reset_in_hours"] = round((sw.get("reset_after_seconds") or 0) / 3600, 1)
        d["plan"] = j.get("plan_type") or "unknown"
        try:
            d["credits_balance"] = float((j.get("credits") or {}).get("balance") or 0)
        except (TypeError, ValueError):
            d["credits_balance"] = 0
        d["limit_reached"] = bool(rl.get("limit_reached"))
    except Exception as e:  # 任何异常都别让 RikkaHub 那边报错
        payload["data"]["error"] = f"{type(e).__name__}: {e}"
    _cache["t"] = now
    _cache["data"] = payload
    return payload


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        allowed = cpa_api_keys()
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if allowed and token not in allowed:
            body = json.dumps({"error": "invalid api key"}, ensure_ascii=False).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(build(), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8791), Handler).serve_forever()
