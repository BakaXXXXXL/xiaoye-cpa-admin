#!/usr/bin/env python3
import argparse
import datetime as dt
import difflib
import json
import os
from pathlib import Path
import secrets as secretgen
import shutil
import subprocess
import sys
import time
import urllib.request
import yaml

BASE = Path("/opt/cpa-friend-gate")
ACCESS = BASE / "access.yaml"
SECRETS = BASE / "secrets.json"
CATALOG = BASE / "catalog" / "providers"
GENERATED = BASE / "model-capabilities.json"
BACKUPS = Path("/opt/cpa-admin/backups")


def load_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def save_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(temporary, path)


def save_json(path, value, mode=0o600):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def catalog_models():
    models = []
    seen = set()
    for path in sorted(CATALOG.glob("*.yaml")):
        for model in load_yaml(path).get("models", []):
            if model["id"] in seen:
                raise RuntimeError(f"duplicate model id: {model['id']}")
            seen.add(model["id"])
            models.append(model)
    return models


def validate():
    access = load_yaml(ACCESS)
    credentials = json.loads(SECRETS.read_text(encoding="utf-8"))
    models = catalog_models()
    model_ids = {model["id"] for model in models}
    for policy_name, policy in (access.get("policies") or {}).items():
        for pattern in policy.get("allow_models") or []:
            if pattern != "*" and not any(model_id == pattern for model_id in model_ids):
                raise RuntimeError(f"policy {policy_name} references unknown model: {pattern}")
    for name, principal in (access.get("principals") or {}).items():
        for ref in (principal.get("credential_ref") or name, principal.get("upstream_key_ref") or name):
            if ref not in credentials:
                raise RuntimeError(f"principal {name} has missing secret ref: {ref}")
        for policy_name in principal.get("policies") or []:
            if policy_name not in (access.get("policies") or {}):
                raise RuntimeError(f"principal {name} has unknown policy: {policy_name}")
    for model in models:
        reasoning = model.get("reasoning") or {}
        if reasoning.get("default") is not None and reasoning["default"] not in (reasoning.get("efforts") or []):
            raise RuntimeError(f"invalid default reasoning effort: {model['id']}")
    return access, credentials, models


def snapshot():
    target = BACKUPS / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    for path in (ACCESS, SECRETS, GENERATED):
        if path.exists():
            shutil.copy2(path, target / path.name)
    if CATALOG.exists():
        shutil.copytree(CATALOG, target / "providers")
    return target


def restart_and_verify():
    subprocess.run(["systemctl", "restart", "cpa-friend-gate.service", "cpa-credits.service", "cpa-keeper-friend-auth.service"], check=True)
    _, credentials, _ = validate()
    principal = next(p for p in load_yaml(ACCESS)["principals"].values() if not p.get("disabled"))
    key = credentials[principal.get("credential_ref")]["key"]
    request = urllib.request.Request("http://127.0.0.1:8321/v1/capabilities", headers={"Authorization": "Bearer " + key})
    for attempt in range(30):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
            break
        except OSError:
            if attempt == 29:
                raise
            time.sleep(0.5)
    print(f"Verified gateway capabilities: {len(result.get('models', []))} models visible to {principal.get('credential_ref')}")


def apply():
    _, _, models = validate()
    saved = snapshot()
    document = {"schema_version": 1, "config_version": dt.datetime.now(dt.timezone.utc).isoformat(), "models": models}
    save_json(GENERATED, document, 0o644)
    try:
        restart_and_verify()
    except Exception:
        for path in (ACCESS, SECRETS, GENERATED):
            source = saved / path.name
            if source.exists():
                shutil.copy2(source, path)
        if (saved / "providers").exists():
            shutil.rmtree(CATALOG)
            shutil.copytree(saved / "providers", CATALOG)
        subprocess.run(["systemctl", "restart", "cpa-friend-gate.service", "cpa-credits.service", "cpa-keeper-friend-auth.service"], check=True)
        raise
    print(f"Applied. Rollback snapshot: {saved}")


def access_command(args):
    access = load_yaml(ACCESS)
    principals = access.setdefault("principals", {})
    policies = access.setdefault("policies", {})
    if args.action == "list":
        for name, principal in principals.items():
            state = "disabled" if principal.get("disabled") else "enabled"
            print(f"{name}\t{state}\t{','.join(principal.get('policies') or [])}\trpm={(principal.get('limits') or {}).get('rpm', 0)}")
        return
    if args.action == "show":
        print(yaml.safe_dump(principals[args.name], allow_unicode=True, sort_keys=False), end="")
        return
    if args.action in ("create-key", "rotate-key"):
        credentials = json.loads(SECRETS.read_text(encoding="utf-8"))
        principal = principals.setdefault(args.name, {"credential_ref": args.name, "upstream_key_ref": args.name, "policies": [], "endpoints": {"models": True, "capabilities": True, "chat": True, "responses": True, "usage": True, "credits": False}, "limits": {"rpm": 150}})
        key = "cpa-" + secretgen.token_urlsafe(32)
        credential = credentials.setdefault(args.name, {})
        credential["key"] = key
        credential.setdefault("upstream_key", key)
        save_json(SECRETS, credentials)
        save_yaml(ACCESS, access)
        print(f"New key for {args.name} (shown once): {key}")
        print("Run cpa-admin apply to activate the change.")
        return
    principal = principals[args.name]
    if args.action in ("grant", "revoke"):
        policy = policies[args.policy or (principal.get("policies") or [""])[0]]
        models = policy.setdefault("allow_models", [])
        if args.action == "grant" and args.model not in models:
            models.append(args.model)
        if args.action == "revoke":
            policy["allow_models"] = [model for model in models if model != args.model]
    elif args.action == "set-rpm":
        principal.setdefault("limits", {})["rpm"] = int(args.value)
    elif args.action == "endpoint":
        principal.setdefault("endpoints", {})[args.endpoint] = args.value == "on"
    elif args.action in ("disable", "enable"):
        principal["disabled"] = args.action == "disable"
    save_yaml(ACCESS, access)
    print("Draft updated; run cpa-admin diff and cpa-admin apply")


def models_command(args):
    if args.action == "list":
        for model in catalog_models():
            print(f"{model['id']}\t{','.join((model.get('reasoning') or {}).get('efforts') or [])}")
        return
    if args.action == "show":
        model = next(model for model in catalog_models() if model["id"] == args.id)
        print(yaml.safe_dump(model, allow_unicode=True, sort_keys=False), end="")
        return
    provider = args.id.split("/", 1)[0]
    path = CATALOG / (provider.replace("_", "-") + ".yaml")
    document = load_yaml(path) if path.exists() else {"models": []}
    models = document.setdefault("models", [])
    existing = next((model for model in models if model["id"] == args.id), None)
    if args.action == "disable":
        existing["enabled"] = False
    elif args.action == "add":
        if existing:
            raise RuntimeError("model already exists")
        efforts = args.reasoning.split(",") if args.reasoning else []
        models.append({"id": args.id, "display_name": args.name or args.id, "enabled": True, "capabilities": {"tools": args.tools, "reasoning": bool(efforts), "attachments": "image" in args.input or "video" in args.input, "streaming": True}, "modalities": {"input": args.input.split(","), "output": ["text"]}, "limits": {"context": args.context, "max_output": args.output}, "reasoning": {"supported": bool(efforts), "efforts": efforts, "default": args.default, "supports_disable": args.supports_disable}})
    save_yaml(path, document)
    print("Draft updated; run cpa-admin diff and cpa-admin apply")


def diff():
    snapshots = sorted(BACKUPS.glob("*")) if BACKUPS.exists() else []
    if not snapshots:
        print("No applied snapshot; run cpa-admin apply")
        return
    previous = snapshots[-1]
    for path in (ACCESS, GENERATED):
        old_path = previous / path.name
        if path == GENERATED:
            old_document = json.loads(old_path.read_text(encoding="utf-8")) if old_path.exists() else {"models": []}
            new_document = {"schema_version": 1, "models": catalog_models()}
            old_document = {"schema_version": 1, "models": old_document.get("models", [])}
            for document in (old_document, new_document):
                document["models"] = sorted(document["models"], key=lambda model: model["id"])
            old = (json.dumps(old_document, ensure_ascii=False, indent=2) + "\n").splitlines()
            new = (json.dumps(new_document, ensure_ascii=False, indent=2) + "\n").splitlines()
        else:
            old = old_path.read_text(encoding="utf-8").splitlines() if old_path.exists() else []
            new = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        print("\n".join(difflib.unified_diff(old, new, str(old_path), str(path), lineterm="")))


def rollback():
    snapshots = sorted(BACKUPS.glob("*"))
    if not snapshots:
        raise RuntimeError("no snapshots")
    saved = snapshots[-1]
    for path in (ACCESS, SECRETS, GENERATED):
        source = saved / path.name
        if source.exists():
            shutil.copy2(source, path)
    if (saved / "providers").exists():
        shutil.rmtree(CATALOG, ignore_errors=True)
        shutil.copytree(saved / "providers", CATALOG)
    restart_and_verify()
    print(f"Rolled back to {saved}")


def main():
    parser = argparse.ArgumentParser(prog="cpa-admin")
    sub = parser.add_subparsers(dest="group", required=True)
    access = sub.add_parser("access")
    access.add_argument("action", choices=["list", "show", "grant", "revoke", "set-rpm", "endpoint", "create-key", "rotate-key", "disable", "enable"])
    access.add_argument("name", nargs="?")
    access.add_argument("value", nargs="?")
    access.add_argument("extra", nargs="?")
    access.add_argument("--policy")
    access.add_argument("--model")
    access.add_argument("--endpoint")
    models = sub.add_parser("models")
    models.add_argument("action", choices=["list", "show", "add", "disable"])
    models.add_argument("id", nargs="?")
    models.add_argument("--name")
    models.add_argument("--input", default="text")
    models.add_argument("--context", type=int)
    models.add_argument("--output", type=int)
    models.add_argument("--tools", action="store_true")
    models.add_argument("--reasoning")
    models.add_argument("--default")
    models.add_argument("--supports-disable", action="store_true")
    sub.add_parser("validate")
    sub.add_parser("apply")
    sub.add_parser("diff")
    sub.add_parser("history")
    sub.add_parser("rollback")
    args = parser.parse_args()
    if args.group == "access":
        if args.action in ("grant", "revoke"):
            args.model = args.value
        if args.action == "endpoint":
            args.endpoint = args.value
            args.value = args.extra or "on"
        access_command(args)
    elif args.group == "models":
        if args.action == "add" and not all((args.id, args.context, args.output)):
            raise RuntimeError("models add requires id, --context and --output")
        models_command(args)
    elif args.group == "validate":
        _, _, models = validate()
        print(f"Valid: {len(models)} models")
    elif args.group == "apply":
        apply()
    elif args.group == "diff":
        diff()
    elif args.group == "history":
        for path in sorted(BACKUPS.glob("*")):
            print(path)
    elif args.group == "rollback":
        rollback()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"cpa-admin: {exc}", file=sys.stderr)
        raise SystemExit(1)
