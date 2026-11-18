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
VISION = Path("/opt/cpa-friend-gate/vision-config.json")
BACKUPS = Path("/opt/cpa-admin/pool-backups")
APPLIED = Path("/opt/cpa-admin/pool-applied")


def load_core():
    return yaml.safe_load(CORE.read_text(encoding="utf-8"))


def load_pools():
    return yaml.safe_load(POOLS.read_text(encoding="utf-8")) or {"pools": {}}


def save_pools(document):
    temporary = POOLS.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(temporary, POOLS)


def save_vision(document):
    temporary = VISION.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, VISION)


def import_pools():
    core = load_core()
    existing = load_pools().get("pools", {}) if POOLS.exists() else {}
    pools = {}
    for provider in (core.get("api-keys") or {}).get("openai-compatibility", []):
        for index, model in enumerate(provider.get("models") or []):
            alias = model.get("alias")
            if not alias:
                continue
            pool = pools.setdefault(alias, {"strategy": "round-robin", "channels": []})
            if (existing.get(alias) or {}).get("vision"):
                pool["vision"] = existing[alias]["vision"]
            config = {key: value for key, value in model.items() if key not in ("name", "alias")}
            pool["channels"].append({"id": f"{provider['name']}-{index}", "provider": provider["name"], "name": model["name"], "enabled": True, "priority": provider.get("priority"), "config": config})
    save_pools({"schema_version": 1, "pools": pools})
    return len(pools)


def get_pool(document, pool_id):
    pool = document.get("pools", {}).get(pool_id)
    if pool is None:
        raise RuntimeError(f"unknown pool: {pool_id}")
    return pool


def find_channel(document, pool_id, channel_id):
    pool = get_pool(document, pool_id)
    channel = next((item for item in pool.get("channels") or [] if item.get("id") == channel_id), None)
    if channel is None:
        raise RuntimeError(f"unknown channel: {pool_id} / {channel_id}")
    return channel


def create_pool(pool_id):
    if not pool_id or pool_id.strip() != pool_id:
        raise RuntimeError("pool id must be non-empty and have no surrounding whitespace")
    document = load_pools()
    pools = document.setdefault("pools", {})
    if pool_id in pools:
        raise RuntimeError(f"pool already exists: {pool_id}")
    pools[pool_id] = {"strategy": "round-robin", "channels": []}
    save_pools(document)
    print(f"Pool draft created: {pool_id}; add an enabled channel before apply")


def add_channel(pool_id, channel_id, provider_name, native_name, priority=None, display_name=None):
    if not channel_id or channel_id.strip() != channel_id:
        raise RuntimeError("channel id must be non-empty and have no surrounding whitespace")
    if not native_name or native_name.strip() != native_name:
        raise RuntimeError("native model name must be non-empty and have no surrounding whitespace")
    document = load_pools()
    pool = get_pool(document, pool_id)
    if any(item.get("id") == channel_id for item in pool.get("channels") or []):
        raise RuntimeError(f"channel already exists: {pool_id} / {channel_id}")
    providers = {provider["name"]: provider for provider in (load_core().get("api-keys") or {}).get("openai-compatibility", [])}
    provider = providers.get(provider_name)
    if not provider:
        raise RuntimeError(f"unknown provider: {provider_name}")
    if not provider.get("keys"):
        raise RuntimeError(f"provider has no credentials: {provider_name}")
    existing_priority = next((item.get("priority") for item_pool in document["pools"].values() for item in item_pool.get("channels") or [] if item.get("provider") == provider_name and item.get("priority") is not None), provider.get("priority"))
    effective_priority = priority if priority is not None else existing_priority
    if priority is not None:
        for item_pool in document["pools"].values():
            for item in item_pool.get("channels") or []:
                if item.get("provider") == provider_name:
                    item["priority"] = priority
    config = {"display-name": display_name} if display_name else {}
    pool.setdefault("channels", []).append({"id": channel_id, "provider": provider_name, "name": native_name, "enabled": True, "priority": effective_priority, "config": config})
    save_pools(document)
    print("Channel draft added; run cpa-admin pool diff and cpa-admin pool apply")


def remove_channel(pool_id, channel_id):
    document = load_pools()
    pool = get_pool(document, pool_id)
    find_channel(document, pool_id, channel_id)
    pool["channels"] = [item for item in pool.get("channels") or [] if item.get("id") != channel_id]
    save_pools(document)
    print("Channel draft removed; run cpa-admin pool diff and cpa-admin pool apply")


def set_vision(pool_id, mode, provider_name=None, model=None):
    if mode not in ("inherit", "preprocess"):
        raise RuntimeError("vision mode must be inherit or preprocess")
    document = load_pools()
    pool = get_pool(document, pool_id)
    if mode == "inherit":
        pool.pop("vision", None)
    else:
        if not provider_name or not model:
            raise RuntimeError("preprocess requires --provider and --model")
        providers = {provider["name"]: provider for provider in (load_core().get("api-keys") or {}).get("openai-compatibility", [])}
        provider = providers.get(provider_name)
        if not provider:
            raise RuntimeError(f"unknown provider: {provider_name}")
        if not provider.get("keys"):
            raise RuntimeError(f"provider has no credentials: {provider_name}")
        pool["vision"] = {"mode": "preprocess", "provider": provider_name, "model": model}
    save_pools(document)
    print("Vision draft updated; run cpa-admin pool diff and cpa-admin pool apply")


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


def validate_pools(for_apply=False):
    core = load_core()
    providers = {provider["name"]: provider for provider in (core.get("api-keys") or {}).get("openai-compatibility", [])}
    pools = load_pools().get("pools") or {}
    for pool_id, pool in pools.items():
        vision = pool.get("vision") or {}
        mode = vision.get("mode", "inherit")
        if mode not in ("inherit", "preprocess"):
            raise RuntimeError(f"pool {pool_id} has invalid vision mode: {mode}")
        if mode == "preprocess":
            if not vision.get("model"):
                raise RuntimeError(f"pool {pool_id} has no vision preprocessing model")
            provider = providers.get(vision.get("provider"))
            if not provider:
                raise RuntimeError(f"unknown vision provider: {vision.get('provider')}")
            if not provider.get("keys"):
                raise RuntimeError(f"vision provider has no credentials: {vision['provider']}")
        channels = pool.get("channels") or []
        if for_apply and not any(channel.get("enabled", True) for channel in channels):
            raise RuntimeError(f"pool {pool_id} has no enabled channel")
        channel_ids = set()
        for channel in channels:
            channel_id = channel.get("id")
            if not channel_id:
                raise RuntimeError(f"pool {pool_id} has a channel without id")
            if channel_id in channel_ids:
                raise RuntimeError(f"duplicate channel id: {pool_id} / {channel_id}")
            channel_ids.add(channel_id)
            if not channel.get("name"):
                raise RuntimeError(f"channel has no native model name: {pool_id} / {channel_id}")
            provider = providers.get(channel.get("provider"))
            if not provider:
                raise RuntimeError(f"unknown provider: {channel.get('provider')}")
            if not provider.get("keys"):
                raise RuntimeError(f"provider has no credentials: {channel['provider']}")
    return pools


def copy_state(target, core=None, pools_path=None, vision=None):
    core = core or CORE
    pools_path = pools_path or POOLS
    vision = vision or VISION
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    shutil.copy2(core, target / "config.yaml")
    shutil.copy2(pools_path, target / "pools.yaml")
    if vision.exists():
        shutil.copy2(vision, target / VISION.name)
    else:
        (target / VISION.name).unlink(missing_ok=True)


def ensure_applied_state():
    if (APPLIED / "config.yaml").exists() and (APPLIED / "pools.yaml").exists():
        return
    snapshots = sorted(BACKUPS.glob("*")) if BACKUPS.exists() else []
    previous_pools = snapshots[-1] / "pools.yaml" if snapshots and (snapshots[-1] / "pools.yaml").exists() else POOLS
    copy_state(APPLIED, pools_path=previous_pools)


def record_applied():
    copy_state(APPLIED)


def snapshot():
    ensure_applied_state()
    target = BACKUPS / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    copy_state(target, APPLIED / "config.yaml", APPLIED / "pools.yaml", APPLIED / VISION.name)
    return target


def render_core():
    core = load_core()
    pools = validate_pools(for_apply=True)
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


def render_vision():
    pools = validate_pools(for_apply=True)
    return {"schema_version": 1, "pools": {pool_id: pool["vision"] for pool_id, pool in pools.items() if (pool.get("vision") or {}).get("mode") == "preprocess"}}


def restart_services():
    subprocess.run(["systemctl", "restart", "cli-proxy-api.service", "cpa-heartbeat.service"], check=True)


def restore_vision(saved):
    source = saved / VISION.name
    if source.exists():
        shutil.copy2(source, VISION)
    else:
        VISION.unlink(missing_ok=True)


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


def wait_for_heartbeat():
    request = urllib.request.Request("http://127.0.0.1:8322/healthz")
    for attempt in range(40):
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
            if result.get("ok"):
                return result.get("preprocess_pools", 0)
        except (OSError, ValueError):
            if attempt == 39:
                raise
            time.sleep(0.5)
    raise RuntimeError("heartbeat proxy did not become ready")


def apply_pools():
    validate_pools(for_apply=True)
    saved = snapshot()
    rendered = render_core()
    rendered_vision = render_vision()
    temporary = CORE.with_suffix(".cpa-admin-tmp")
    temporary.write_text(yaml.safe_dump(rendered, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, CORE)
    try:
        save_vision(rendered_vision)
        restart_services()
        count = wait_for_core()
        wait_for_heartbeat()
        record_applied()
    except Exception:
        shutil.copy2(saved / "config.yaml", CORE)
        shutil.copy2(saved / "pools.yaml", POOLS)
        restore_vision(saved)
        restart_services()
        wait_for_core()
        wait_for_heartbeat()
        raise
    print(f"Pool configuration applied; CPA reports {count} models. Snapshot: {saved}")


def diff_pools():
    ensure_applied_state()
    previous = APPLIED
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
    restore_vision(saved)
    restart_services()
    wait_for_core()
    wait_for_heartbeat()
    record_applied()
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
