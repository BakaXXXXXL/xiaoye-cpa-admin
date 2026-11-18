#!/usr/bin/env python3
import datetime as dt
import json
import subprocess
import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path("/opt/cpa-friend-gate")
SERVICES = [
    "cli-proxy-api.service",
    "cpa-friend-gate.service",
    "cpa-heartbeat.service",
    "cpa-credits.service",
    "cpa-keeper-friend-auth.service",
    "cpa-usage-keeper.service",
    "longcat-thinking-proxy.service",
    "cpa-admin-readonly.service",
    "caddy.service",
]


def read_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def service_states():
    result = []
    for name in SERVICES:
        process = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        result.append({"name": name, "status": process.stdout.strip() or "unknown"})
    return result


def overview():
    access = read_yaml(BASE / "access.yaml")
    pools = read_yaml(BASE / "pools.yaml").get("pools") or {}
    models = []
    for path in sorted((BASE / "catalog" / "providers").glob("*.yaml")):
        models.extend(read_yaml(path).get("models") or [])
    principals = []
    for name, principal in (access.get("principals") or {}).items():
        principals.append({
            "name": name,
            "disabled": bool(principal.get("disabled")),
            "policies": principal.get("policies") or [],
            "endpoints": principal.get("endpoints") or {},
            "limits": principal.get("limits") or {},
        })
    pool_items = []
    for pool_id, pool in pools.items():
        channels = []
        for channel in pool.get("channels") or []:
            channels.append({
                "id": channel.get("id"),
                "provider": channel.get("provider"),
                "name": channel.get("name"),
                "enabled": channel.get("enabled", True),
                "priority": channel.get("priority"),
            })
        pool_items.append({"id": pool_id, "strategy": pool.get("strategy", "round-robin"), "channels": channels})
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "summary": {
            "models": len(models),
            "enabled_models": sum(1 for model in models if model.get("enabled", True)),
            "principals": len(principals),
            "pools": len(pool_items),
            "channels": sum(len(pool["channels"]) for pool in pool_items),
        },
        "models": models,
        "principals": principals,
        "policies": access.get("policies") or {},
        "pools": pool_items,
        "services": service_states(),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/overview":
            try:
                self.send_json(200, overview())
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.send_json(500, {"error": {"message": type(exc).__name__}})
            return
        if self.path == "/health":
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": {"message": "not found"}})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8326), Handler)
    server.daemon_threads = True
    server.serve_forever()
