#!/usr/bin/env python3
"""Map CPA friend-gate keys to their upstream CPA keys for Keeper login.

Only the Keeper API-key login endpoint is handled. The browser keeps using the
friend key; the upstream key never leaves the server.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from policy import keeper_mapping

CONFIG_PATH = os.environ.get("FRIEND_GATE_CONFIG", "/opt/cpa-friend-gate/config.json")
UPSTREAM = os.environ.get("KEEPER_UPSTREAM", "http://127.0.0.1:8788").rstrip("/")
PORT = int(os.environ.get("PORT", "8792"))
LOGIN_PATH = "/keeper/api/v1/auth/api-key-login"
MAX_BODY = 64 * 1024


def load_mapping():
    return keeper_mapping()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Do not log request bodies or credentials.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == "/healthz":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != LOGIN_PATH:
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid content length"})
            return
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "invalid body"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "invalid json"})
            return

        supplied = payload.get("apiKey")
        mapped = False
        if isinstance(supplied, str):
            try:
                mapping = load_mapping()
            except Exception as exc:
                sys.stderr.write("friend config error: %s\n" % type(exc).__name__)
                self._json(503, {"error": "mapping unavailable"})
                return
            replacement = mapping.get(supplied)
            if replacement:
                payload["apiKey"] = replacement
                mapped = True

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {}
        for name, value in self.headers.items():
            if name.lower() in ("host", "content-length", "connection"):
                continue
            headers[name] = value
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
        headers.setdefault("X-CPA-Usage-Keeper-Request", "fetch")
        if self.client_address and "X-Forwarded-For" not in headers:
            headers["X-Forwarded-For"] = self.client_address[0]

        req = urllib.request.Request(UPSTREAM + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status = resp.status
                resp_body = resp.read()
                resp_headers = list(resp.headers.items())
        except urllib.error.HTTPError as exc:
            status = exc.code
            resp_body = exc.read()
            resp_headers = list(exc.headers.items())
        except Exception as exc:
            sys.stderr.write("keeper upstream error: %s\n" % type(exc).__name__)
            self._json(502, {"error": "keeper unavailable"})
            return

        self.send_response(status)
        for name, value in resp_headers:
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if resp_body:
            self.wfile.write(resp_body)
        sys.stderr.write("keeper friend login mapped=%s status=%s\n" % (mapped, status))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("listening on 127.0.0.1:%d" % PORT, flush=True)
    server.serve_forever()
