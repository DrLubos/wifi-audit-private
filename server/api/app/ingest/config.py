"""Configuration delivery, the ingest side (docs/config-delivery.md 3, 5.5,
5.6, 6): the sensor's report (config_state and capabilities in its
heartbeat), whether a response carries the desired configuration, and what a
reported attempt does. Standard library only (tests/test_ingest_config.py);
routes.py runs the SQL for each action.

The server never connects to the sensor: the desired configuration rides in
the responses to the uploader's own requests, and only to an uploader that
reports config_state with accepting = true (CONFIG_APPLY=1 on the Pi).
"""

from .validate import RESULTS, parse_capabilities, parse_state  # noqa: F401 - the heartbeat's parts

REFUSED = ("rejected", "rolled_back", "failed")


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
