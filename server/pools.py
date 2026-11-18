import datetime as dt
import difflib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import yaml

CORE = Path("/opt/cli-proxy-api/config.yaml")
POOLS = Path("/opt/cpa-friend-gate/pools.yaml")
BACKUPS = Path("/opt/cpa-admin/pool-backups")


def load_core():
    return yaml.safe_load(CORE.read_text(encoding="utf-8"))


def load_pools():
    return yaml.safe_load(POOLS.read_text(encoding="utf-8")) or {"pools": {}}


def save_pools(document):
    temporary = POOLS.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(temporary, POOLS)


def import_pools():
    core = load_core()
    pools = {}
    for provider in (core.get("api-keys") or {}).get("openai-compatibility", []):
        for index, model in enumerate(provider.get("models") or []):
            alias = model.get("alias")
            if not alias:
                continue
            pool = pools.setdefault(alias, {"strategy": "round-robin", "channels": []})
            config = {key: value for key, value in model.items() if key not in ("name", "alias")}
            pool["channels"].append({"id": f"{provider['name']}-{index}", "provider": provider["name"], "name": model["name"], "enabled": True, "priority": provider.get("priority"), "config": config})
    save_pools({"schema_version": 1, "pools": pools})
    return len(pools)


def find_channel(document, pool_id, channel_id):
    pool = document["pools"][pool_id]
    return next(channel for channel in pool["channels"] if channel["id"] == channel_id)


def list_pools():
    for pool_id, pool in load_pools().get("pools", {}).items():
        channels = pool.get("channels") or []
        enabled = sum(1 for channel in channels if channel.get("enabled", True))
        print(f"{pool_id}\t{enabled}/{len(channels)} channels\t{pool.get('strategy', 'round-robin')}")


def show_pool(pool_id):
    print(yaml.safe_dump(load_pools()["pools"][pool_id], allow_unicode=True, sort_keys=False), end="")


def edit_pool(pool_id, channel_id, action, value=None):
    document = load_pools()
    channel = find_channel(document, pool_id, channel_id)
    if action in ("enable", "disable"):
        channel["enabled"] = action == "enable"
    elif action == "set-priority":
        priority = int(value)
        for pool in document["pools"].values():
            for item in pool.get("channels") or []:
                if item["provider"] == channel["provider"]:
                    item["priority"] = priority
    save_pools(document)
    print("Pool draft updated; run cpa-admin pool diff and cpa-admin pool apply")


def validate_pools():
    core = load_core()
    providers = {provider["name"]: provider for provider in (core.get("api-keys") or {}).get("openai-compatibility", [])}
    pools = load_pools().get("pools") or {}
    for pool_id, pool in pools.items():
        channels = pool.get("channels") or []
        if not any(channel.get("enabled", True) for channel in channels):
            raise RuntimeError(f"pool {pool_id} has no enabled channel")
        for channel in channels:
            provider = providers.get(channel.get("provider"))
            if not provider:
                raise RuntimeError(f"unknown provider: {channel.get('provider')}")
            if not provider.get("keys"):
                raise RuntimeError(f"provider has no credentials: {channel['provider']}")
    return pools


def snapshot():
    target = BACKUPS / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CORE, target / "config.yaml")
    shutil.copy2(POOLS, target / "pools.yaml")
    return target


def render_core():
    core = load_core()
    pools = validate_pools()
    providers = {provider["name"]: provider for provider in (core.get("api-keys") or {}).get("openai-compatibility", [])}
    for provider in providers.values():
        provider["models"] = [model for model in (provider.get("models") or []) if model.get("alias") not in pools]
    for pool_id, pool in pools.items():
        for channel in pool.get("channels") or []:
            provider = providers[channel["provider"]]
            if channel.get("priority") is not None:
                provider["priority"] = channel["priority"]
            if not channel.get("enabled", True):
                continue
            model = {"name": channel["name"], "alias": pool_id}
            model.update(channel.get("config") or {})
            provider.setdefault("models", []).append(model)
    return core


def wait_for_core():
    import sys
    sys.path.insert(0, "/opt/cpa-friend-gate")
    from policy import principal_records
    key = next(item["key"] for item in principal_records() if "*" in item["models"])
    request = urllib.request.Request("http://127.0.0.1:8317/v1/models", headers={"Authorization": "Bearer " + key})
    for attempt in range(40):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
            return len(result.get("data", []))
        except OSError:
            if attempt == 39:
                raise
            time.sleep(0.5)


def apply_pools():
    validate_pools()
    saved = snapshot()
    rendered = render_core()
    temporary = CORE.with_suffix(".cpa-admin-tmp")
    temporary.write_text(yaml.safe_dump(rendered, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, CORE)
    try:
        subprocess.run(["systemctl", "restart", "cli-proxy-api.service"], check=True)
        count = wait_for_core()
    except Exception:
        shutil.copy2(saved / "config.yaml", CORE)
        shutil.copy2(saved / "pools.yaml", POOLS)
        subprocess.run(["systemctl", "restart", "cli-proxy-api.service"], check=True)
        wait_for_core()
        raise
    print(f"Pool configuration applied; CPA reports {count} models. Snapshot: {saved}")


def diff_pools():
    snapshots = sorted(BACKUPS.glob("*"))
    if not snapshots:
        print("No pool snapshot; run cpa-admin pool apply")
        return
    previous = snapshots[-1]
    for path in (POOLS,):
        old_path = previous / path.name
        old = old_path.read_text(encoding="utf-8").splitlines() if old_path.exists() else []
        new = path.read_text(encoding="utf-8").splitlines()
        print("\n".join(difflib.unified_diff(old, new, str(old_path), str(path), lineterm="")))


def rollback_pools():
    snapshots = sorted(BACKUPS.glob("*"))
    if not snapshots:
        raise RuntimeError("no pool snapshots")
    saved = snapshots[-1]
    shutil.copy2(saved / "config.yaml", CORE)
    shutil.copy2(saved / "pools.yaml", POOLS)
    subprocess.run(["systemctl", "restart", "cli-proxy-api.service"], check=True)
    wait_for_core()
    print(f"Pool configuration rolled back to {saved}")


def test_channel(pool_id, channel_id):
    core = load_core()
    channel = find_channel(load_pools(), pool_id, channel_id)
    provider = next(item for item in (core.get("api-keys") or {}).get("openai-compatibility", []) if item["name"] == channel["provider"])
    credential = provider["keys"][0]
    if isinstance(credential, dict):
        credential = credential.get("api-key") or credential.get("key")
    payload = json.dumps({"model": channel["name"], "messages": [{"role": "user", "content": "Reply with ok"}], "max_tokens": 1, "stream": False}).encode()
    request = urllib.request.Request(provider["base-url"].rstrip("/") + "/chat/completions", data=payload, headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        response.read()
        print(f"Channel test succeeded: {pool_id} / {channel_id} (HTTP {response.status})")
