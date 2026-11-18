import json
import os
import yaml

ACCESS_PATH = os.environ.get("CPA_ACCESS_CONFIG", "/opt/cpa-friend-gate/access.yaml")
SECRETS_PATH = os.environ.get("CPA_SECRETS_CONFIG", "/opt/cpa-friend-gate/secrets.json")


def load_access():
    with open(ACCESS_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_secrets():
    with open(SECRETS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def principal_records():
    access = load_access()
    secrets = load_secrets()
    policies = access.get("policies") or {}
    records = []
    for name, principal in (access.get("principals") or {}).items():
        if principal.get("disabled"):
            continue
        credential = secrets.get(principal.get("credential_ref") or name) or {}
        upstream = secrets.get(principal.get("upstream_key_ref") or principal.get("credential_ref") or name) or {}
        models = []
        for policy_name in principal.get("policies") or []:
            models.extend((policies.get(policy_name) or {}).get("allow_models") or [])
        records.append({
            "name": name,
            "key": credential.get("key", ""),
            "upstream_key": upstream.get("upstream_key") or upstream.get("key", ""),
            "models": list(dict.fromkeys(models)),
            "rpm": int((principal.get("limits") or {}).get("rpm", 0)),
            "endpoints": principal.get("endpoints") or {},
        })
    return records


def endpoint_allowed(principal, endpoint):
    return bool((principal.get("endpoints") or {}).get(endpoint, False))


def keeper_mapping():
    return {item["key"]: item["upstream_key"] for item in principal_records() if item["key"] and item["upstream_key"] and endpoint_allowed(item, "usage")}


def credits_keys():
    return {item["key"] for item in principal_records() if item["key"] and endpoint_allowed(item, "credits")}
