"""The uploader's part of configuration delivery (server/docs/config-delivery.md
3, 4.3, 5.5). The uploader never applies anything: it hands a delivered
configuration to the root applier as a file (CONFIG_DIR/desired.json, its own
state directory) and reads the applier's files back for the heartbeat
(applied.json, result.json, capabilities.json in CONFIG_STATE_DIR). Standard
library only."""

import json
import os
import re
import time

import capture_config as cc
import state as statemod

_HASH = re.compile(r"^[0-9a-f]{64}$")
MAX_BYTES = 8192
_KEYS = {"version", "sha256", "retry", "desired"}
RESULT_KEYS = ("version", "sha256", "result", "reason", "started_poll", "effective_poll", "restored_poll",
               "checks")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else None
    except (OSError, ValueError):
        return None


def accept_delivery(obj, desired_path, accepting):
    """(written, why). The response's "config" object -> desired.json, only
    with CONFIG_APPLY on, when it is exactly {version, sha256, retry,
    desired}, at most 8 KB, and desired hashes to sha256; never rewritten for
    the same configuration (the server repeats it until the sensor reports)."""
    if not accepting:
        return False, "not accepting (CONFIG_APPLY=0)"
    if not isinstance(obj, dict) or set(obj) != _KEYS:
        return False, "malformed"
    if len(json.dumps(obj)) > MAX_BYTES:
        return False, "too large"
    v, sha, desired = obj["version"], obj["sha256"], obj["desired"]
    if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0) or not isinstance(sha, str) \
            or not _HASH.match(sha) or not isinstance(obj["retry"], bool) or not isinstance(desired, dict):
        return False, "malformed"
    try:
        if cc.sha256(desired) != sha:
            return False, "hash mismatch"
    except (KeyError, TypeError, AttributeError, ValueError):
        return False, "malformed"
    old = _load(desired_path)
    if old and old.get("version") == v and old.get("sha256") == sha and old.get("retry") == obj["retry"]:
        return False, "already received"
    os.makedirs(os.path.dirname(desired_path), exist_ok=True)
    statemod.save(desired_path, {"version": v, "sha256": sha, "retry": obj["retry"], "desired": desired,
                                 "received_at": time.time()})
    return True, None


def config_state(accepting, state_dir, desired_path):
    """The heartbeat's config_state: whether this sensor accepts delivery,
    what the applier last applied, and the newest attempt - the applier's
    result for the delivered version, else "received" while it waits."""
    applied = _load(os.path.join(state_dir, "applied.json")) or {}
    result = _load(os.path.join(state_dir, "result.json"))
    desired = _load(desired_path)
    attempt = None
    if result and isinstance(result.get("version"), int) and isinstance(result.get("result"), str):
        attempt = {k: result.get(k) for k in RESULT_KEYS}
        attempt["checks"] = attempt["checks"] if isinstance(attempt["checks"], dict) else {}
    if desired and isinstance(desired.get("version"), int) and (
            attempt is None or (attempt["version"], attempt["sha256"]) != (desired["version"], desired.get("sha256"))):
        attempt = {"version": desired["version"], "sha256": desired.get("sha256"), "result": "received",
                   "reason": None, "started_poll": None, "effective_poll": None, "restored_poll": None, "checks": {}}
    v = applied.get("version")
    return {"accepting": bool(accepting), "applied_version": v if isinstance(v, int) else None,
            "applied_sha256": applied.get("sha256") if isinstance(applied.get("sha256"), str) else None,
            "attempt": attempt}


def capabilities(state_dir):
    """The applier's capabilities.json (Kismet's channel list for the source,
    the regulatory country), the two known fields, or None."""
    c = _load(os.path.join(state_dir, "capabilities.json"))
    if not c:
        return None
    return {"channels_supported": c.get("channels_supported"), "reg_country": c.get("reg_country")}
