"""In-process cache of the account's APs (aps.sql account_aps).

The view account_aps runs ap_classification() and ap_is_mobile() per row:
~90 ms for ~850 APs on the e2-micro, on every AP list request. Its rows
change only when refresh_rollups() runs (sensor_summary.refreshed_at) or
the account's configuration is written (accounts.write_version) - the same
versions the v2 ETag is made of - so they are kept per account until one of
them moves. One uvicorn worker, a lock for its thread pool. The rows are
shared: callers must not modify them.
"""

import threading

_lock = threading.Lock()
_entries = {}            # account id -> (key, {ap_id: row})


def account_aps(conn, acc, sql):
    key = (acc["refreshed_at"], acc["write_version"])
    with _lock:
        hit = _entries.get(acc["id"])
    if hit is not None and hit[0] == key:
        return hit[1]
    rows = {r["ap_id"]: r for r in conn.execute(sql, {"account_id": acc["id"]}, prepare=False).fetchall()}
    with _lock:
        _entries[acc["id"]] = (key, rows)
    return rows


def clear():
    with _lock:
        _entries.clear()
