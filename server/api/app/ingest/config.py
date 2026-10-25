"""Configuration delivery, the ingest side (docs/config-delivery.md 3, 5.5,
5.6, 6): the sensor's report (config_state and capabilities in its
heartbeat), whether a response carries the desired configuration, and what a
reported attempt does. Standard library only (tests/test_ingest_config.py);
routes.py runs the SQL for each action.

The server never connects to the sensor: the desired configuration rides in
the responses to the uploader's own requests, and only to an uploader that
reports config_state with accepting = true (CONFIG_APPLY=1 on the Pi).
"""

import re

from .validate import Invalid

RESULTS = ("received", "applying", "applied", "rejected", "rolled_back", "failed")
REFUSED = ("rejected", "rolled_back", "failed")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_COUNTRY = re.compile(r"^[A-Z0-9]{2}$")
_ENTRY = re.compile(r"^[0-9A-Za-z+-]{1,16}$")
REASON_MAX = 500
CHECKS_MAX = 2000                    # characters of the checks object as text


def _int(x):
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 2 ** 63


def _opt(d, key, ok, what):
    x = d.get(key)
    if x is not None and not ok(x):
        raise Invalid("config_state: %s is invalid" % what)
    return x


def _flat(x, depth=0):
    """A small JSON value of numbers, booleans, short strings, lists / objects of them."""
    if x is None or isinstance(x, (bool, int, float)):
        return True
    if isinstance(x, str):
        return len(x) <= 200
    if depth >= 2:
        return False
    if isinstance(x, list):
        return len(x) <= 20 and all(_flat(v, depth + 1) for v in x)
    if isinstance(x, dict):
        return len(x) <= 30 and all(isinstance(k, str) and len(k) <= 40 and _flat(v, depth + 1) for k, v in x.items())
    return False


def parse_state(x):
    """heartbeat.config_state -> the known fields, or None (an uploader
    before 1.2 sends none)."""
    if x is None:
        return None
    if not isinstance(x, dict):
        raise Invalid("config_state must be an object")
    out = {"accepting": _opt(x, "accepting", lambda v: isinstance(v, bool), "accepting") is True,
           "applied_version": _opt(x, "applied_version", _int, "applied_version"),
           "applied_sha256": _opt(x, "applied_sha256", lambda v: isinstance(v, str) and _HASH.match(v),
                                  "applied_sha256"),
           "attempt": None}
    a = x.get("attempt")
    if a is not None:
        if not isinstance(a, dict):
            raise Invalid("config_state: attempt must be an object")
        if not _int(a.get("version")):
            raise Invalid("config_state: attempt.version is invalid")
        if not (isinstance(a.get("sha256"), str) and _HASH.match(a["sha256"])):
            raise Invalid("config_state: attempt.sha256 is invalid")
        if a.get("result") not in RESULTS:
            raise Invalid("config_state: attempt.result must be one of " + ", ".join(RESULTS))
        reason = _opt(a, "reason", lambda v: isinstance(v, str) and len(v) <= REASON_MAX and "\x00" not in v,
                      "attempt.reason")
        checks = a.get("checks") or {}
        if not isinstance(checks, dict) or not _flat(checks) or len(repr(checks)) > CHECKS_MAX:
            raise Invalid("config_state: attempt.checks is invalid")
        out["attempt"] = {"version": a["version"], "sha256": a["sha256"], "result": a["result"], "reason": reason,
                          "started_poll": _opt(a, "started_poll", _int, "attempt.started_poll"),
                          "effective_poll": _opt(a, "effective_poll", _int, "attempt.effective_poll"),
                          "restored_poll": _opt(a, "restored_poll", _int, "attempt.restored_poll"),
                          "checks": checks}
    return out


def parse_capabilities(x):
    """heartbeat.capabilities (refreshed hourly by the sensor) -> the known
    fields, or None."""
    if x is None:
        return None
    if not isinstance(x, dict):
        raise Invalid("capabilities must be an object")
    ch = x.get("channels_supported")
    if ch is not None and not (isinstance(ch, list) and len(ch) <= 400
                               and all(isinstance(e, str) and _ENTRY.match(e) for e in ch)):
        raise Invalid("capabilities: channels_supported is invalid")
    country = x.get("reg_country")
    if country is not None and not (isinstance(country, str) and _COUNTRY.match(country)):
        raise Invalid("capabilities: reg_country must be a two-letter code")
    return {"channels_supported": ch, "reg_country": country}


def delivery(desired, state):
    """The response's "config" object, or None. desired: the sensor's newest
    operator-made sensor_config row (with retry = an older version of the same
    hash was refused), state: parse_state() of this request's heartbeat."""
    if desired is None or state is None or not state.get("accepting"):
        return None
    if not desired.get("sha256") or desired.get("refused_at") or desired.get("confirmed_at"):
        return None
    if state.get("applied_sha256") == desired["sha256"]:
        return None
    return {"version": desired["version"], "sha256": desired["sha256"], "retry": bool(desired.get("retry")),
            "desired": {"channels": list(desired["channels"]), "dwell_s": round(float(desired["dwell_s"]), 3),
                        "poll_s": desired["poll_s"]}}


def boundary_label(row):
    plan = row.get("channel_plan")
    parts = ([plan] if plan else []) + ["%d channels" % len(row["channels"]),
                                        "dwell %g s" % round(float(row["dwell_s"]), 3), "poll %d s" % row["poll_s"]]
    return "Capture configuration v%d: %s" % (row["version"], ", ".join(parts))


def attempt_actions(a, row):
    """What a reported attempt does, as (action, params) pairs in order:
    attempt (always: the record), delivered, confirm + boundary +
    close_events (applied, once the effective poll is known), refuse,
    coverage (rolled back: the window on the rejected configuration),
    open_event. row: the sensor_config row of the attempt's version ({} or
    None when unknown); an attempt whose hash is not that row's is recorded
    only. Every action is idempotent in SQL (a heartbeat repeats its attempt)."""
    out = [("attempt", a)]
    if not row or row.get("sha256") != a["sha256"]:
        return out
    out.append(("delivered", {"version": a["version"]}))
    v, reason = a["version"], a.get("reason") or "no reason given"
    if a["result"] == "applied":
        if a.get("effective_poll") is not None:
            out.append(("confirm", {"version": v, "poll": a["effective_poll"]}))
            out.append(("boundary", {"poll": a["effective_poll"], "label": boundary_label(row)}))
            out.append(("close_events", {"types": ["config_rolled_back", "config_failed"]}))
    elif a["result"] in REFUSED:
        out.append(("refuse", {"version": v, "reason": "%s: %s" % (a["result"], reason)}))
        if a["result"] == "rolled_back":
            if a.get("started_poll") is not None and a.get("restored_poll") is not None:
                out.append(("coverage", {"from_poll": a["started_poll"], "to_poll": a["restored_poll"],
                                         "label": "Configuration v%d tried, rolled back: %s" % (v, reason)}))
            out.append(("open_event", {"type": "config_rolled_back", "severity": "medium",
                                       "summary": "Configuration v%d rolled back on the sensor" % v,
                                       "evidence": {"version": v, "reason": reason, "checks": a.get("checks")}}))
        elif a["result"] == "failed":
            out.append(("open_event", {"type": "config_failed", "severity": "high",
                                       "summary": "Configuration v%d failed on the sensor - capture state unknown" % v,
                                       "evidence": {"version": v, "reason": reason, "checks": a.get("checks")}}))
    return out
