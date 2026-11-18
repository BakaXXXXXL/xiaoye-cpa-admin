#!/usr/bin/env python3
"""cpa-friend-gate — 给单个朋友的 CPA 前置网关。

作用：
  * 用独立 key 鉴权（客户端 key 不进 CPA；每个 key 绑定自己的上游 key，
    这样 CPA 侧能按人区分用量，客户端 key 又不能直接拿去打通 /cpa/*)
  * 按 key 限制可见/可调用的模型（glob 匹配）
  * 轻量限流（rpm + 并发）
  * 每个请求写一行 JSONL 审计日志（时间 / key 名 / 模型 / 状态码 / 字节数）

只监听 127.0.0.1，由 Caddy 反代暴露到公网路径。
"""

import http.client
import hmac
import json
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from fnmatch import fnmatchcase
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from policy import endpoint_allowed, principal_records

CONFIG_PATH = os.environ.get("GATE_CONFIG", "/opt/cpa-friend-gate/config.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
    CFG = json.load(fh)

UP = CFG["upstream"]
HOST, PORT = CFG.get("listen", ["127.0.0.1", 8321])
KEYS = {k["key"]: k for k in principal_records() if k["key"] and k["upstream_key"]}
LOG_PATH = CFG.get("log", "/var/log/cpa-friend-gate/access.log")
MAX_CONCURRENCY = int(CFG.get("max_concurrent", 3))
UPSTREAM_TIMEOUT = int(CFG.get("upstream_timeout", 900))
MAX_BODY_BYTES = int(CFG.get("max_body_bytes", 16 * 1024 * 1024))
# 旧模型名 -> 新「通道/模型名」的兼容映射：客户端没改配置也能继续用
MODEL_ALIASES = {str(k): str(v) for k, v in (CFG.get("model_aliases") or {}).items()}
# 可选：对外暴露的模型能力元数据。字段以客户端实际可用为准，不把上游
# 未验证的能力写成 true。放在 config.json 里方便以后随模型升级调整。
MODEL_METADATA = {str(k): dict(v) for k, v in (CFG.get("model_metadata") or {}).items()}
CAPABILITIES_PATH = os.environ.get("CAPABILITIES_CONFIG", "/opt/cpa-friend-gate/model-capabilities.json")
with open(CAPABILITIES_PATH, "r", encoding="utf-8") as fh:
    MODEL_CAPABILITIES = json.load(fh)
SUPPORTED_POST_PATHS = {"/v1/chat/completions", "/v1/responses"}

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

_LOG_LOCK = threading.Lock()
_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY)
_RATE = {}
_RATE_LOCK = threading.Lock()

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "host", "authorization", "x-api-key", "accept-encoding",
}


def write_log(rec):
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = json.dumps(rec, ensure_ascii=False)
    with _LOG_LOCK:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def model_allowed(keycfg, model):
    return any(fnmatchcase(model, pat) for pat in keycfg.get("models", []))


def canonical_model(model):
    return MODEL_ALIASES.get(model, model)


def upstream_key(keycfg):
    """每个 gate key 可以绑一把独立的上游 key；不填则回落到全局主 key。"""
    return keycfg.get("upstream_key") or UP["api_key"]


def rate_ok(keycfg):
    limit = int(keycfg.get("rpm", 0))
    if limit <= 0:
        return True
    now = time.time()
    with _RATE_LOCK:
        dq = _RATE.setdefault(keycfg["key"], deque())
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def bearer(handler):
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (handler.headers.get("x-api-key") or "").strip()


def new_trace_id():
    return uuid.uuid4().hex


def model_record(model_id, upstream=None):
    """Merge local capability metadata into an OpenAI model object."""
    upstream = upstream or {}
    meta = MODEL_METADATA.get(model_id, {})
    rec = {
        "id": model_id,
        "object": "model",
        "created": upstream.get("created", 0),
        "owned_by": upstream.get("owned_by") or meta.get("owned_by") or "xiaoye",
    }
    rec.update({k: v for k, v in meta.items() if k not in ("id", "object", "created", "owned_by")})
    if "display_name" in meta and "name" not in meta:
        rec["name"] = meta["display_name"]
    if "name" in meta and "display_name" not in meta:
        rec["display_name"] = meta["name"]
    if "context_window" in meta and "context_length" not in meta:
        rec["context_length"] = meta["context_window"]
    if "max_output_tokens" in meta and "max_tokens" not in meta:
        rec["max_tokens"] = meta["max_output_tokens"]
    if "max_output_tokens" in meta and "max_completion_tokens" not in meta:
        rec["max_completion_tokens"] = meta["max_output_tokens"]
    rec["metadata"] = dict(meta)
    return rec


def usage_summary(keycfg):
    """Request counts from the gate's own JSONL log.

    This is deliberately not a billing endpoint: token/credit balances live in
    the upstream providers and are not available per friend key. It does give
    the client a way to see whether requests are reaching the gate and which
    models are being used.
    """
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    out = {
        "object": "usage",
        "key": keycfg.get("name"),
        "date": today,
        "requests": 0,
        "success": 0,
        "errors": 0,
        "bytes_out": 0,
        "by_model": {},
        "last_request": None,
        "limits": {
            "rpm": int(keycfg.get("rpm", 0)),
            "max_concurrent": MAX_CONCURRENCY,
        },
        "note": "请求计数来自朋友网关访问日志；token 与额度余额需看上游账单。",
    }
    try:
        size = os.path.getsize(LOG_PATH)
        with open(LOG_PATH, "rb") as fh:
            if size > 5 * 1024 * 1024:
                fh.seek(-5 * 1024 * 1024, os.SEEK_END)
                fh.readline()  # discard partial line
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if rec.get("key") != keycfg.get("name"):
                    continue
                ts = str(rec.get("ts") or "")
                if not ts.startswith(today):
                    continue
                out["requests"] += 1
                status = int(rec.get("status") or 0)
                if 200 <= status < 400:
                    out["success"] += 1
                else:
                    out["errors"] += 1
                out["bytes_out"] += int(rec.get("bytes") or 0)
                model = rec.get("model") or "-"
                out["by_model"][model] = out["by_model"].get(model, 0) + 1
                out["last_request"] = {
                    "ts": ts,
                    "model": model,
                    "status": status,
                    "ms": int(rec.get("ms") or 0),
                }
    except OSError:
        pass
    return out


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-API-Key, X-Cpa-Trace-Id",
    "Access-Control-Expose-Headers": "X-Cpa-Trace-Id, X-Request-Id",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cpa-friend-gate/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---------- 响应工具 ----------
    def trace_id(self):
        if not getattr(self, "_trace_id", ""):
            self._trace_id = new_trace_id()
        return self._trace_id

    def send_json(self, code, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in CORS_HEADERS.items():
            self.send_header(name, value)
        self.send_header("X-Cpa-Trace-Id", self.trace_id())
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, code, message, etype="invalid_request_error", ecode=None, param=None):
        err = {"message": message, "type": etype}
        if ecode:
            err["code"] = ecode
        if param:
            err["param"] = param
        self.send_json(code, {"error": err})

    # ---------- 鉴权 ----------
    def authenticate(self):
        key = bearer(self)
        cfg = None
        for candidate, keycfg in KEYS.items():
            if key and hmac.compare_digest(key, candidate):
                cfg = keycfg
                break
        if cfg is None:
            write_log({"key": "-", "auth": "fail", "prefix": key[:12] if key else "",
                       "status": 401, "path": self.path.split("?", 1)[0],
                       "ip": self.client_address[0],
                       "ua": (self.headers.get("User-Agent") or "")[:70]})
            self.send_error_json(401, "invalid api key", "authentication_error", "invalid_api_key")
            return None
        return cfg

    # ---------- GET ----------
    def do_OPTIONS(self):
        # Browser preflight never carries the API key; answer it before auth.
        self.send_response(204)
        for name, value in CORS_HEADERS.items():
            self.send_header(name, value)
        self.send_header("X-Cpa-Trace-Id", self.trace_id())
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        keycfg = self.authenticate()
        if keycfg is None:
            return
        path = unquote(self.path.split("?", 1)[0])

        if path in ("/healthz", "/health"):
            return self.send_json(200, {"ok": True})

        if path in ("/v1/models", "/models"):
            if not endpoint_allowed(keycfg, "models"):
                return self.send_error_json(403, "models endpoint is not allowed for this key", "permission_error", "endpoint_forbidden")
            return self.list_models(keycfg)

        if path == "/v1/capabilities":
            if not endpoint_allowed(keycfg, "capabilities"):
                return self.send_error_json(403, "capabilities endpoint is not allowed for this key", "permission_error", "endpoint_forbidden")
            return self.list_capabilities(keycfg)

        if path == "/v1/key/info":
            return self.key_info(keycfg)
        if path == "/v1/usage":
            if not endpoint_allowed(keycfg, "usage"):
                return self.send_error_json(403, "usage endpoint is not allowed for this key", "permission_error", "endpoint_forbidden")
            return self.send_json(200, usage_summary(keycfg))

        if path.startswith("/v1/models/"):
            model = canonical_model(path[len("/v1/models/"):])
            if not model_allowed(keycfg, model):
                return self.send_error_json(
                    404,
                    f"model '{model}' is not available for this key",
                    "invalid_request_error", "model_not_found", param="model")
            return self.send_json(200, model_record(model))

        return self.send_error_json(404, "not found", "invalid_request_error", "not_found")

    # ---------- 模型列表 ----------
    def list_models(self, keycfg):
        try:
            conn = http.client.HTTPConnection(UP["host"], UP["port"], timeout=20)
            conn.request("GET", "/v1/models", headers={"Authorization": "Bearer " + upstream_key(keycfg)})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            return self.send_error_json(502, f"upstream error: {exc}", "api_error", "upstream_error")

        upstream_by_id = {str(m.get("id", "")): m for m in data.get("data", [])}
        items = []
        candidates = set(upstream_by_id) | set(keycfg.get("models", []))
        for model in sorted(candidates):
            if "*" not in model and model_allowed(keycfg, model):
                items.append(model_record(model, upstream_by_id.get(model)))
        self.send_json(200, {
            "object": "list",
            "data": items,
            "metadata": {
                "gate": "cpa-friend-gate",
                "model_count": len(items),
            },
        })

    def list_capabilities(self, keycfg):
        models = [
            model for model in MODEL_CAPABILITIES.get("models", [])
            if model.get("enabled", True) and model_allowed(keycfg, model.get("id", ""))
        ]
        response = dict(MODEL_CAPABILITIES)
        response["models"] = models
        self.send_json(200, response)

    def key_info(self, keycfg, include_usage=False):
        obj = {
            "object": "key.info",
            "key": keycfg.get("name"),
            "models": [model_record(m) for m in keycfg.get("models", [])],
            "limits": {
                "rpm": int(keycfg.get("rpm", 0)),
                "max_concurrent": MAX_CONCURRENCY,
            },
        }
        if include_usage:
            obj["usage"] = usage_summary(keycfg)
        self.send_json(200, obj)

    # ---------- POST ----------
    def do_POST(self):
        keycfg = self.authenticate()
        if keycfg is None:
            return

        path = unquote(self.path.split("?", 1)[0])
        if path not in SUPPORTED_POST_PATHS:
            # Don't let unknown endpoints turn into "missing model" or an
            # empty upstream 404. Image generation is intentionally not
            # enabled for friend keys.
            if path == "/v1/images/generations":
                return self.send_error_json(
                    404, "image generation is not enabled for this key",
                    "invalid_request_error", "not_found")
            return self.send_error_json(404, "not found", "invalid_request_error", "not_found")

        endpoint = "chat" if path == "/v1/chat/completions" else "responses"
        if not endpoint_allowed(keycfg, endpoint):
            return self.send_error_json(403, f"{endpoint} endpoint is not allowed for this key", "permission_error", "endpoint_forbidden")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return self.send_error_json(413, "request body too large",
                                        "invalid_request_error", "payload_too_large")
        body = self.rfile.read(length) if length > 0 else b""

        if self.headers.get("Content-Encoding"):
            return self.send_error_json(400, "compressed request bodies are not supported by this gateway",
                                        "invalid_request_error", "unsupported_encoding")

        try:
            payload = json.loads(body) if body else {}
            model = payload.get("model")
        except (json.JSONDecodeError, AttributeError):
            return self.send_error_json(400, "request body is not valid JSON",
                                        "invalid_request_error", "invalid_json")

        if not isinstance(model, str) or not model:
            return self.send_error_json(400, "request has no 'model' field",
                                        "invalid_request_error", "missing_model", param="model")

        canonical = canonical_model(model)
        if canonical != model:
            payload["model"] = canonical
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            model = canonical

        if not model_allowed(keycfg, model):
            write_log({"key": keycfg.get("name"), "model": model, "status": 404,
                       "path": path})
            return self.send_error_json(
                404,
                f"model '{model}' is not available for this key",
                "invalid_request_error", "model_not_found", param="model")

        # Validate output-token limits at the gate so all channels fail the
        # same way. The Codex upstream currently ignores max_tokens; we can't
        # make it obey without changing CPA, but we can at least reject values
        # above the documented ceiling instead of silently accepting them.
        meta = MODEL_METADATA.get(model, {})
        max_out = meta.get("max_output_tokens")
        if isinstance(max_out, int) and max_out > 0:
            for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                if field not in payload:
                    continue
                val = payload[field]
                if val is None:
                    continue
                if isinstance(val, bool) or not isinstance(val, int) or val < 1 or val > max_out:
                    return self.send_error_json(
                        400,
                        f"{field} must be an integer between 1 and {max_out} for {model}",
                        "invalid_request_error", "invalid_value", param=field)

        if not rate_ok(keycfg):
            write_log({"key": keycfg.get("name"), "model": model, "status": 429,
                       "path": path})
            return self.send_error_json(429, "rate limit exceeded, slow down a bit",
                                        "rate_limit_error", "rate_limited")

        return self.proxy("POST", body, keycfg, model=model)

    # ---------- 转发 ----------
    def proxy(self, method, body, keycfg, model=None):
        if not _SEM.acquire(timeout=30):
            return self.send_error_json(429, "gateway busy, retry shortly",
                                        "rate_limit_error", "concurrency_limited")
        started = time.time()
        status = 0
        sent = 0
        try:
            headers = {}
            for name, value in self.headers.items():
                if name.lower() in HOP_HEADERS:
                    continue
                headers[name] = value
            headers["Authorization"] = "Bearer " + upstream_key(keycfg)
            headers["Accept-Encoding"] = "identity"
            headers["X-Cpa-Trace-Id"] = self.trace_id()
            if method == "POST":
                headers["Content-Length"] = str(len(body))

            upstream_port = CFG.get("heartbeat_port", 8322) if self.path.split("?", 1)[0] == "/v1/chat/completions" else UP["port"]
            conn = http.client.HTTPConnection(UP["host"], upstream_port, timeout=UPSTREAM_TIMEOUT)
            conn.request(method, self.path, body=body if method != "GET" else None, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            content_type = (resp.getheader("Content-Type") or "").lower()

            # CPA sometimes returns an HTML 5xx page (notably /v1/responses).
            # Never leak that to a JSON client; normalize it before streaming.
            if status >= 500 and content_type.startswith("text/html"):
                try:
                    raw = resp.read(32768)
                except Exception:  # noqa: BLE001
                    raw = b""
                conn.close()
                upstream_status = status
                status = 502
                return self.send_error_json(
                    502,
                    f"upstream returned HTTP {upstream_status} HTML instead of JSON",
                    "api_error", "upstream_error")

            trace = resp.getheader("X-Cpa-Trace-Id") or self.trace_id()
            self.send_response(resp.status, resp.reason)
            for name, value in resp.getheaders():
                lname = name.lower()
                if lname in {"transfer-encoding", "connection", "content-length",
                             "keep-alive", "content-encoding",
                             "access-control-allow-origin", "access-control-allow-methods",
                             "access-control-allow-headers", "access-control-expose-headers",
                             "x-cpa-trace-id"}:
                    continue
                self.send_header(name, value)
            for name, value in CORS_HEADERS.items():
                self.send_header(name, value)
            self.send_header("X-Cpa-Trace-Id", trace)

            no_body = resp.status in (204, 304) or method == "HEAD"
            if no_body:
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read1(8192)
                    if not chunk:
                        break
                    sent += len(chunk)
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            conn.close()
        except (BrokenPipeError, ConnectionResetError):
            status = status or 499
        except Exception as exc:  # noqa: BLE001
            status = status or 599
            try:
                self.send_error_json(502, f"upstream error: {exc}", "api_error", "upstream_error")
            except Exception:  # noqa: BLE001
                pass
        finally:
            _SEM.release()
            write_log({
                "key": keycfg.get("name"), "model": model, "status": status,
                "path": self.path.split("?", 1)[0], "bytes": sent,
                "ms": int((time.time() - started) * 1000),
                "trace": self.trace_id(),
            })


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"cpa-friend-gate listening on {HOST}:{PORT} -> {UP['host']}:{UP['port']}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
