"""Sensor events for upload stalls that need a person (docs/live-upload.md
6.5, amendment 1): a batch the server keeps refusing (422), a token that
keeps failing (401 with a known token id, 403 revoked), and the backlog
approaching the sensor's retention (heartbeat). One open event per type and
sensor (ts_end NULL), closed automatically when uploads resume. Network or
server outages are no events (D12).

The state lives in sensor_status (reject_*, auth_fail_*); these functions
compute the next state and whether the event opens. Standard library only
(tests/test_ingest_logic.py).
"""

REJECT_COUNT = 3              # refusals of the same batch ...
AUTH_FAIL_COUNT = 3           # ... or failed authentications ...
PERSIST_S = 600               # ... over at least 10 minutes
CLOSE_ON_ACCEPT = ("upload_rejected", "upload_unauthorized")
TYPES = ("upload_rejected", "upload_unauthorized", "upload_retention_risk")


def clear():
    return {"reject_seq": None, "reject_since": None, "reject_count": 0, "reject_reason": None,
            "auth_fail_since": None, "auth_fail_count": 0}


def _state(st):
    out = clear()
    out.update({k: v for k, v in (st or {}).items() if k in out})
    return out


def after_reject(st, batch_seq, reason, now):
    """(state, open the upload_rejected event?) after a 422 for batch_seq."""
    st = _state(st)
    if st["reject_seq"] == batch_seq and st["reject_since"] is not None:
        st["reject_count"] += 1
    else:
        st.update(reject_seq=batch_seq, reject_since=now, reject_count=1)
    st["reject_reason"] = (reason or "")[:500]
    persistent = (now - st["reject_since"]).total_seconds() >= PERSIST_S
    return st, st["reject_count"] >= REJECT_COUNT and persistent


def after_accept(st):
    st = _state(st)
    st.update(reject_seq=None, reject_since=None, reject_count=0, reject_reason=None,
              auth_fail_since=None, auth_fail_count=0)
    return st


def after_auth_fail(st, now):
    """(state, open the upload_unauthorized event?) after a wrong secret or a revoked token."""
    st = _state(st)
    if st["auth_fail_since"] is None:
        st.update(auth_fail_since=now, auth_fail_count=1)
    else:
        st["auth_fail_count"] += 1
    persistent = (now - st["auth_fail_since"]).total_seconds() >= PERSIST_S
    return st, st["auth_fail_count"] >= AUTH_FAIL_COUNT and persistent


def after_auth_ok(st):
    st = _state(st)
    st.update(auth_fail_since=None, auth_fail_count=0)
    return st


def event(kind, st, oldest=None, retention_days=None):
    """(summary, evidence) of a stall event - no token, no secret."""
    if kind == "upload_rejected":
        return ("The server refuses a batch of the sensor's upload (it needs a fix)",
                {"batch_seq": st.get("reject_seq"), "refusals": st.get("reject_count"),
                 "since": _iso(st.get("reject_since")), "reason": st.get("reject_reason")})
    if kind == "upload_unauthorized":
        return ("The sensor's ingest token is refused (wrong or revoked)",
                {"failures": st.get("auth_fail_count"), "since": _iso(st.get("auth_fail_since"))})
    if kind == "upload_retention_risk":
        return ("Upload backlog close to the sensor's retention: data will be lost unless it is uploaded",
                {"oldest_unsent": _iso(oldest), "retention_days": retention_days})
    raise ValueError(kind)


def _iso(t):
    if t is None:
        return None
    if isinstance(t, (int, float)):
        from datetime import datetime, timezone
        t = datetime.fromtimestamp(t, timezone.utc)
    return t.isoformat().replace("+00:00", "Z")
