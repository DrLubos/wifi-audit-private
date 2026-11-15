"""One upload batch from the collector's buffer (server/docs/live-upload.md 4.3, 5).

The buffer is opened read-only (mode=ro); the uploader keeps its own cursor:

  {"buffer_id", "polls_id", "obs_id", "dch_id", "alerts_id", "load_ts",
   "freq_ts", "batch_seq"}

The spine is the polls after polls_id in append (= true) order. Each poll is
sent, held back (its boot's clock has no anchor yet - nothing after it is
sent either) or skipped as time-unverified (an earlier boot that never got an
anchor; a gap of the batch). Rows are taken by their poll: observations,
configuration history and alerts after their cursors, by poll_id (rows of
buffers <= v7 by their ts); AP rows only (the AP evidence of the importer -
a test keeps it equal); the AP load and frequency rows updated up to the
batch's last true ts (at most LOAD_ROWS_PER_BATCH load rows a batch - the
rest goes with the next ones); the devices the batch references, with their
time fields placed (timeline.py) against the stale window of every boot the
buffer, the uploader's state or its configuration (STALE_WINDOWS) still
knows, not only the batch's own. Polls
pruned on the sensor before the upload (the cursor is older than the oldest
poll) are a 'lost' gap.

Column lists equal the server's (server/api/app/ingest/validate.py - a test
compares them). Standard library only (tests/test_batch.py).
"""

import math
import os
import sqlite3
from urllib.parse import quote

import timeline

# = server/seed/import_snapshot.py _AP_EVIDENCE = schema.sql ap_evidence()
AP_EVIDENCE = "type = 'ap' OR ssid IS NOT NULL OR crypt IS NOT NULL OR adv_channel IS NOT NULL"

CONFIG = ("ssid", "cloaked", "crypt", "crypt_bits", "mfp_sup", "mfp_req", "adv_channel", "ht_mode",
          "beacon_rate", "country")
COLUMNS = {
    "devices": ("device_key", "mac", "type", "manuf", "first_seen", "last_seen") + CONFIG
               + ("config_changed_at", "cur_n_clients", "cur_qbss_stations", "cur_util_pct", "cur_at"),
    "polls": ("seq", "ts", "ts_offset_s", "kismet_ts", "devices_total", "devices_active", "new_obs",
              "ds_running", "ds_error", "ds_packets", "ds_hop_n", "ds_hop_visited", "ds_hop_ok",
              "duration_ms", "ok", "error", "config_version"),
    "device_config_history": ("device_key", "ts") + CONFIG,
    "observations": ("ts", "device_key", "rssi", "rssi_floor", "pk_total", "pk_data", "disconnects",
                     "disconnects_last", "bss_timestamp"),
    "ap_load_hourly": ("device_key", "hour", "channel", "polls", "clients_polls", "clients_max",
                       "clients_sum", "qbss_polls", "qbss_max", "qbss_sum", "util_max", "util_sum"),
    "device_freq_hist": ("device_key", "freq_khz", "packets", "updated_ts"),
    "alerts": ("hash", "ts", "header", "class", "severity", "source_mac", "dest_mac", "transmitter_mac",
               "other_mac", "channel", "freq_khz", "device_key", "text", "raw"),
}
ORDER = ("devices", "polls", "device_config_history", "observations", "ap_load_hourly", "device_freq_hist",
         "alerts")
# AP load rows a batch carries at most: after load_rewind() days of hours are
# due at once, and the server takes 30000 rows a batch (validate.MAX_ROWS)
LOAD_ROWS_PER_BATCH = 5000


def open_buffer(path):
    db = sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True, timeout=10,
                         isolation_level=None)
    db.execute("PRAGMA query_only = 1")
    db.row_factory = sqlite3.Row
    return db


def meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _cols(db, table):
    return {r[1] for r in db.execute("PRAGMA table_info(%s)" % table)}


def new_cursor(buffer_id):
    return {"buffer_id": buffer_id, "polls_id": 0, "obs_id": 0, "dch_id": 0, "alerts_id": 0,
            "load_ts": 0, "freq_ts": 0, "batch_seq": 0}


def cursor_after(db, buffer_id, resume):
    """The cursor of a new uploader (D5): just after the server's newest poll
    (resume = {seq, ts} from the heartbeat; seq None for polls the server got
    from snapshots before it stored seq - then found by ts). None resume = the
    server has nothing: from the beginning."""
    cur = new_cursor(buffer_id)
    if not resume:
        return cur
    ts = resume["ts"]
    seq = resume.get("seq")
    if seq is None or not db.execute("SELECT 1 FROM polls WHERE id = ? AND ts = ?", (seq, ts)).fetchone():
        row = db.execute("SELECT max(id) FROM polls WHERE ts <= ? AND coalesce(clock_synced, 1) = 1"
                         if "clock_synced" in _cols(db, "polls") else
                         "SELECT max(id) FROM polls WHERE ts <= ?", (ts,)).fetchone()
        seq = row[0] or 0
    cur["polls_id"] = seq
    # the rows of later polls start after the last row at or before the resume point
    nxt = db.execute("SELECT id FROM observations WHERE ts > ? ORDER BY ts, id LIMIT 1", (ts,)).fetchone()
    cur["obs_id"] = (nxt[0] - 1) if nxt else (db.execute("SELECT coalesce(max(id), 0) FROM observations")
                                              .fetchone()[0])
    cur["dch_id"] = db.execute("SELECT coalesce(max(id), 0) FROM device_config_history WHERE ts <= ?",
                               (ts,)).fetchone()[0]
    nxt = db.execute("SELECT min(id) FROM alerts WHERE ts > ?", (ts,)).fetchone()[0]
    cur["alerts_id"] = (nxt - 1) if nxt else db.execute("SELECT coalesce(max(id), 0) FROM alerts").fetchone()[0]
    cur["load_ts"] = ts
    cur["freq_ts"] = ts
    return cur


# --- boots ---------------------------------------------------------------------------------------

def boots(db, boot_ids, server_anchors):
    """{boot_id: timeline.Boot} for the given boots: the NTP anchor of the
    buffer (else the uploader's server anchor) and the boot's stale clock."""
    out = {}
    if "clock_anchors" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}:
        return out
    for b in boot_ids:
        if not b:
            continue
        a = db.execute("SELECT wall, mono FROM clock_anchors WHERE boot_id = ?", (b,)).fetchone()
        source = "ntp" if a else None
        anchor = (a["wall"], a["mono"]) if a else None
        if anchor is None and b in (server_anchors or {}):
            sa = server_anchors[b]
            anchor, source = (sa["wall"], sa["mono"]), "server"
        first = db.execute("SELECT ts, mono FROM polls WHERE boot_id = ? AND clock_synced = 0 AND mono IS NOT NULL "
                           "ORDER BY id LIMIT 1", (b,)).fetchone()
        rng = db.execute("SELECT min(mono), max(mono) FROM polls WHERE boot_id = ? AND clock_synced = 0", (b,)).fetchone()
        out[b] = timeline.Boot(b, anchor=anchor, source=source,
                               stale_ts=first["ts"] if first else None, stale_mono=first["mono"] if first else None,
                               unsynced_mono=(rng[0], rng[1]))
    return out


def held_polls(db, current_boot):
    """Unsynchronised polls of the current boot while it has no NTP anchor."""
    if not current_boot or "clock_anchors" not in {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}:
        return 0
    if db.execute("SELECT 1 FROM clock_anchors WHERE boot_id = ?", (current_boot,)).fetchone():
        return 0
    return db.execute("SELECT count(*) FROM polls WHERE boot_id = ? AND clock_synced = 0",
                      (current_boot,)).fetchone()[0]


def stale_windows(db, server_anchors, known=None):
    """{boot_id: [from, to, delta]} - the stale window (on the stale clock)
    and the shift to true time (None: no anchor) of every boot that ran on a
    wrong clock: those with unsynchronised polls in the buffer, and `known`
    (the uploader's state) for boots whose polls the retention has removed
    since. A device row keeps a stale first_seen for good on the sensor
    (MIN over all boots), so the window must outlive the polls: sent raw, the
    server's LEAST() would move a correct first_seen back by the clock's
    error (three APs, 8 h 25 min, after the offline boot of 2026-10-20)."""
    out = {b: list(w) for b, w in (known or {}).items()}
    if "clock_synced" not in _cols(db, "polls"):
        return out
    ids = {r[0] for r in db.execute("SELECT DISTINCT boot_id FROM polls WHERE clock_synced = 0") if r[0]}
    for b, boot in boots(db, ids, server_anchors).items():
        w = boot.stale_window()
        if w is None:
            continue
        delta = boot.delta if boot.anchored else None
        old = out.get(b)
        if old is not None:
            # the union with what is known: the retention removes a boot's
            # unsynchronised polls oldest first, so the window read from the
            # buffer shrinks day by day - the devices first seen in its early
            # part keep their stale first_seen all the same
            w = (min(old[0], w[0]), max(old[1], w[1]))
            if delta is None:
                delta = old[2]                # an anchor known before stays
        out[b] = [w[0], w[1], delta]
    return out


def parse_windows(text):
    """The STALE_WINDOWS setting: ({boot_id: [from, to, delta]}, [entries
    that cannot be read]). Entries 'boot_id:from:to:delta', separated by
    spaces or commas: from/to in unix seconds on the stale clock, delta the
    shift to true time in seconds ('none': the boot never got an anchor).

    For the boots the uploader cannot learn about by itself: their polls were
    pruned before it remembered stale windows (uploader < 1.3), or its state
    file was lost after the retention removed them. The device rows keep
    those boots' stale first_seen all the same. The server has what is
    needed (sensor_clock_boots; server/tests/sql/check_first_seen.sql prints
    the entries)."""
    out, bad = {}, []
    for entry in (text or "").replace(",", " ").split():
        parts = entry.split(":")
        try:
            if len(parts) != 4 or not parts[0]:
                raise ValueError(entry)
            start, end = float(parts[1]), float(parts[2])
            delta = None if parts[3].lower() == "none" else float(parts[3])
            if not (math.isfinite(start) and math.isfinite(end) and start <= end):
                raise ValueError(entry)
            if delta is not None and not math.isfinite(delta):
                raise ValueError(entry)
        except ValueError:
            bad.append(entry)
            continue
        out[parts[0]] = [start, end, delta]
    return out, bad


def merge_windows(*maps):
    """One {boot_id: [from, to, delta]} of several: per boot the union of
    the ranges and the first delta that is known (earlier maps first)."""
    out = {}
    for m in maps:
        for b, w in (m or {}).items():
            old = out.get(b)
            if old is None:
                out[b] = list(w)
            else:
                out[b] = [min(old[0], w[0]), max(old[1], w[1]), old[2] if old[2] is not None else w[2]]
    return out


def load_rewind(db, boot_id):
    """Where the AP load cursor must go back to for a boot the uploader
    anchored on the server's time: (done, ts).

    The collector folds the boot's pending AP load into ap_load_hourly, on
    the true hours, in the transaction of its first synchronised poll. An
    uploader that had a server anchor by then has sent those polls already,
    its load cursor is past their time, and the folded rows (last_ts in the
    past) would never go (17 hours of 2026-11-02/03 did not). Once the
    buffer shows the boot's NTP anchor: ts = just before the true time of
    its first unsynchronised poll - the rows from there are sent again (the
    server keeps the row with more polls, so a second copy changes nothing).
    done without a ts: nothing to send again (the polls are pruned, or the
    boot ended without an NTP anchor - its pending load is never folded)."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "clock_anchors" not in tables or not boot_id:
        return True, None
    a = db.execute("SELECT wall, mono FROM clock_anchors WHERE boot_id = ?", (boot_id,)).fetchone()
    if a is None:
        return False, None
    first = db.execute("SELECT min(mono) FROM polls WHERE boot_id = ? AND clock_synced = 0", (boot_id,)).fetchone()[0]
    if first is None:
        return True, None
    return True, int(a["wall"] - a["mono"] + first) - 1


def poll_identity(db, poll_id):
    """What tells one poll from another that got the same row id: its raw ts
    and its boot. None when the buffer has no such poll."""
    row = db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "ts": row["ts"], "boot_id": row["boot_id"] if "boot_id" in row.keys() else None}


def repair_cursor(db, cursor, last):
    """(cursor, polls stepped back) - the cursor moved back when the buffer
    is no longer the one it was written against.

    The buffer can lose its newest transactions in a power cut (they were in
    the WAL, not yet synced), after the uploader has sent them and saved its
    cursor. The next boot's collector then reuses those row ids: for the
    uploader the new polls, observations, history rows and alerts lie
    *before* its cursor and would never be sent. `last` is poll_identity()
    of the newest poll the server acknowledged; when the buffer's poll with
    that id is another one (or gone), the poll cursor goes back to the
    newest poll that is still the acknowledged boot's, and the row cursors
    to just before the first row of a later poll. Rows sent twice by this
    are merged away by the server; nothing is skipped."""
    if not last or last.get("id") != cursor["polls_id"]:
        return cursor, 0
    c, boot = cursor["polls_id"], last.get("boot_id")
    now = poll_identity(db, c)
    if now is not None and now["ts"] == last.get("ts") and now["boot_id"] == boot:
        return cursor, 0
    newest = db.execute("SELECT max(id) FROM polls").fetchone()[0]
    if newest is None:
        keep = 0                              # an emptied buffer numbers its polls from 1 again
    elif now is None and c <= newest:
        return cursor, 0                      # pruned by the retention: the 'lost' gap's business, not a reuse
    else:
        top = min(c, newest)
        keep = db.execute("SELECT max(id) FROM polls WHERE id <= ? AND boot_id IS ?", (top, boot)).fetchone()[0]
        if keep is None:                      # nothing of the acknowledged boot is left: everything of the
            b = poll_identity(db, top)["boot_id"]             # boot that owns the ids now goes (again)
            keep = db.execute("SELECT min(id) FROM polls WHERE id <= ? AND boot_id IS ?", (top, b)).fetchone()[0] - 1
    if keep >= c:
        return cursor, 0
    new = dict(cursor, polls_id=keep)
    # the first row a later poll wrote: observations by their poll's ts (indexed), the small tables by poll_id
    first_obs = None
    for p in db.execute("SELECT id, ts FROM polls WHERE id > ? ORDER BY id LIMIT 2000", (keep,)).fetchall():
        first_obs = db.execute("SELECT min(id) FROM observations WHERE ts = ? AND poll_id = ?",
                               (p["ts"], p["id"])).fetchone()[0]
        if first_obs is not None:
            break
    for key, table, first in (("obs_id", "observations", first_obs),
                              ("dch_id", "device_config_history", None), ("alerts_id", "alerts", None)):
        if table != "observations":
            first = db.execute("SELECT min(id) FROM %s WHERE poll_id > ?" % table, (keep,)).fetchone()[0]
        if first is not None:
            new[key] = min(cursor[key], first - 1)
        else:
            new[key] = min(cursor[key], db.execute("SELECT coalesce(max(id), 0) FROM %s" % table).fetchone()[0])
    return new, c - keep


# --- the batch -----------------------------------------------------------------------------------

def _ranges(ids):
    out = []
    for i in sorted(ids):
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


def build(db, cursor, current_boot, server_anchors, max_polls, windows=None, extra_windows=None):
    """(batch, new cursor, info) or (None, cursor, info) when nothing is due.

    batch: {"tables": {name: {"cols", "rows"}}, "gaps": [...], "boots": [...],
    "polls": n}; the envelope (v, batch_seq, buffer_id, ...) is added by the
    caller. windows: stale windows the uploader's state remembers
    (stale_windows()); extra_windows: those of the configuration
    (parse_windows()) - used like the remembered ones, never added to them.
    info: {"held": n, "unverified": {boot: polls},
    "corrected": {boot: (n, [offsets])}, "windows": {boot: [from, to, delta]}}.
    """
    has_v8 = "clock_synced" in _cols(db, "polls")
    info = {"held": 0, "unverified": {}, "corrected": {}, "windows": dict(windows or {})}
    gaps = []
    oldest = db.execute("SELECT min(id) FROM polls").fetchone()[0]
    if oldest is not None and cursor["polls_id"] < oldest - 1 and cursor["polls_id"] > 0:
        gaps.append({"seq_from": cursor["polls_id"] + 1, "seq_to": oldest - 1, "reason": "lost"})

    polls = [dict(r) for r in db.execute(
        "SELECT * FROM polls WHERE id > ? ORDER BY id LIMIT ?", (cursor["polls_id"], max_polls))]
    bmap = boots(db, {p.get("boot_id") for p in polls}, server_anchors) if has_v8 else {}
    sent, skipped, last_id = [], [], cursor["polls_id"]
    for p in polls:
        fate = timeline.poll_fate(p, bmap.get(p.get("boot_id")), current_boot) if has_v8 else timeline.SEND
        if fate == timeline.HOLD:
            info["held"] = held_polls(db, current_boot)
            break
        last_id = p["id"]
        if fate == timeline.UNVERIFIED:
            skipped.append(p["id"])
            info["unverified"][p["boot_id"]] = info["unverified"].get(p["boot_id"], 0) + 1
            continue
        boot = bmap.get(p.get("boot_id"))
        p["true_ts"], p["offset"] = timeline.place_poll(p, boot)
        p["boot"] = boot
        if p["offset"] is not None:
            n, offs = info["corrected"].get(p["boot_id"], (0, []))
            info["corrected"][p["boot_id"]] = (n + 1, offs + [p["offset"]])
        sent.append(p)
    for a, b in _ranges(skipped):
        raw = db.execute("SELECT min(ts), max(ts) FROM polls WHERE id BETWEEN ? AND ?", (a, b)).fetchone()
        gaps.append({"seq_from": a, "seq_to": b, "reason": "unverified", "raw_from": raw[0], "raw_to": raw[1]})
    if not sent and not gaps:
        return None, cursor, info

    by_id = {p["id"]: p for p in sent}
    by_raw_ts = {p["ts"]: p for p in sent if p.get("boot_id") is None}      # rows of buffers <= v7
    skipped_set = set(skipped)
    true_to = max([p["true_ts"] for p in sent] + [cursor["load_ts"]])
    raw_to = max([p["ts"] for p in sent], default=0)
    new = dict(cursor, polls_id=last_id)
    ap_known = {}

    def is_ap(key):
        """The AP evidence of one device, looked up once a batch. (All AP keys
        at once was a scan of the whole devices table for every batch: 0.6 s
        each 30 s with 480,000 devices in the buffer, growing with it.)"""
        known = ap_known.get(key)
        if known is None:
            known = ap_known[key] = db.execute(
                "SELECT 1 FROM devices WHERE key = ? AND (" + AP_EVIDENCE + ")", (key,)).fetchone() is not None
        return known

    def owner(row):
        """(poll, stop): the sent poll a row belongs to; stop = it is beyond the batch."""
        pid = row["poll_id"] if "poll_id" in row.keys() else None
        if pid is not None:
            if pid > last_id:
                return None, True
            return by_id.get(pid), False
        if row["ts"] is not None and row["ts"] > raw_to and not skipped_set:
            return None, True
        return by_raw_ts.get(row["ts"]), False

    # observations
    obs_rows, last_time = [], {}
    for r in db.execute("SELECT * FROM observations WHERE id > ? ORDER BY id", (cursor["obs_id"],)):
        p, stop = owner(r)
        if stop:
            break
        new["obs_id"] = r["id"]
        if p is None or not is_ap(r["key"]):
            continue
        lt = timeline.place_kismet(r["last_time"], p["boot"], p["offset"])
        last_time.setdefault(r["key"], []).append(lt)
        obs_rows.append([p["true_ts"], r["key"], r["rssi"], r["rssi_floor"], r["pk_total"], r["pk_data"],
                         r["disconnects"], timeline.place_kismet(r["disconnects_last"], p["boot"], p["offset"]),
                         r["bss_timestamp"]])
    # configuration history
    hist_rows = []
    for r in db.execute("SELECT * FROM device_config_history WHERE id > ? ORDER BY id", (cursor["dch_id"],)):
        p, stop = owner(r)
        if stop:
            break
        new["dch_id"] = r["id"]
        if p is None or not is_ap(r["key"]):
            continue
        hist_rows.append([r["key"], p["true_ts"]] + [r[c] for c in CONFIG])
    # alerts (Kismet's own time, placed by the boot of the poll that fetched it)
    alert_rows = []
    for r in db.execute("SELECT * FROM alerts WHERE id > ? ORDER BY id", (cursor["alerts_id"],)):
        pid = r["poll_id"] if "poll_id" in r.keys() else None
        if pid is not None and pid > last_id:
            break
        new["alerts_id"] = r["id"]
        if pid is not None and pid not in by_id:
            continue
        p = by_id.get(pid)
        ts = timeline.place_kismet(r["ts"], p["boot"], p["offset"]) if p else r["ts"]
        alert_rows.append([r["hash"], ts, r["header"], r["class"], r["severity"], r["source_mac"], r["dest_mac"],
                           r["transmitter_mac"], r["other_mac"], r["channel"], r["freq_khz"], r["device_key"],
                           r["text"], r["raw"]])
    # AP load per hour, updated after the cursor up to the batch's true end;
    # oldest first and at most LOAD_ROWS_PER_BATCH, cut between two last_ts
    # values so that the cursor never passes a row that was not sent
    load_rows, load_to = [], true_to
    if "ap_load_hourly" in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}:
        due = db.execute("SELECT * FROM ap_load_hourly WHERE last_ts > ? AND last_ts <= ? ORDER BY last_ts LIMIT ?",
                         (cursor["load_ts"], true_to, LOAD_ROWS_PER_BATCH + 1)).fetchall()
        if len(due) > LOAD_ROWS_PER_BATCH:
            cut = due[LOAD_ROWS_PER_BATCH]["last_ts"]
            if due[0]["last_ts"] < cut:
                due, load_to = [r for r in due if r["last_ts"] < cut], cut - 1
            else:                             # more rows on one second than a batch takes: that second whole
                due, load_to = db.execute("SELECT * FROM ap_load_hourly WHERE last_ts = ?", (cut,)).fetchall(), cut
        for r in due:
            if is_ap(r["key"]):
                load_rows.append([r["key"], r["hour"], r["channel"], r["polls"], r["clients_polls"],
                                  r["clients_max"], r["clients_sum"], r["qbss_polls"], r["qbss_max"],
                                  r["qbss_sum"], r["util_max"], r["util_sum"]])
    new["load_ts"] = load_to
    # frequency histograms (cumulative counters; only the band fallback reads
    # them): every poll updates every AP's ~16 rows, so they go at most once per
    # UTC hour - with the first batch of an hour, the APs of that batch, every
    # row updated since the last send
    freq_rows = []
    keys = sorted({o[1] for o in obs_rows})
    if true_to // 3600 > cursor["freq_ts"] // 3600:
        for k in keys:
            for r in db.execute("SELECT key, freq_khz, packets, updated_ts FROM device_freq_hist "
                                "WHERE key = ? AND updated_ts > ? AND updated_ts <= ?",
                                (k, cursor["freq_ts"], true_to)):
                freq_rows.append([r["key"], r["freq_khz"], r["packets"], r["updated_ts"]])
        new["freq_ts"] = true_to
    # the devices every row refers to, their time fields placed: against the
    # stale window of every boot known to have run on a wrong clock
    info["windows"] = stale_windows(db, server_anchors, windows) if has_v8 else dict(windows or {})
    # (also for the batch's own boots: the remembered window is the union, the
    # one read from the buffer has lost what the retention removed)
    known = merge_windows(info["windows"], extra_windows)
    all_boots = list(bmap.values()) + [timeline.Window(*w) for b, w in sorted(known.items())]
    dev_rows = []
    for k in sorted(set(keys) | {h[0] for h in hist_rows} | {lr[0] for lr in load_rows}):
        d = db.execute("SELECT * FROM devices WHERE key = ?", (k,)).fetchone()
        if d is None:
            continue
        seen = [t for t in last_time.get(k, []) if t is not None]
        last_seen = max(seen) if seen else timeline.placeable(d["last_seen"], all_boots)
        first_seen = timeline.placeable(d["first_seen"], all_boots)
        if first_seen is None:
            first_seen = min(seen) if seen else last_seen
        if last_seen is None:
            continue                      # nothing of it can be placed yet
        first_seen = min(first_seen, last_seen)
        cur_at = timeline.placeable(d["cur_at"], all_boots)
        cur = [d["cur_n_clients"], d["cur_qbss_stations"], d["cur_util_pct"], cur_at] if cur_at else [None] * 4
        dev_rows.append([k, d["mac"], d["type"], d["manuf"], first_seen, last_seen] + [d[c] for c in CONFIG]
                        + [timeline.placeable(d["config_changed_at"], all_boots)] + cur)
    poll_rows = []
    for p in sent:
        kts = p["kismet_ts"]
        if kts is not None and p["offset"] is not None:
            kts = int(round(kts + p["offset"]))
        poll_rows.append([p["id"], p["true_ts"], p["offset"], kts, p["devices_total"], p["devices_active"],
                          p["new_obs"], p["ds_running"], p["ds_error"], p["ds_packets"], p.get("ds_hop_n"),
                          p.get("ds_hop_visited"), p.get("ds_hop_ok"), p["duration_ms"], p["ok"], p["error"],
                          p.get("config_version")])
    tables = {}
    for name, rows in (("devices", dev_rows), ("polls", poll_rows), ("device_config_history", hist_rows),
                       ("observations", obs_rows), ("ap_load_hourly", load_rows), ("device_freq_hist", freq_rows),
                       ("alerts", alert_rows)):
        if rows:
            tables[name] = {"cols": list(COLUMNS[name]), "rows": rows}
    boot_payload = []
    for b, (n, offs) in info["corrected"].items():
        if bmap.get(b) is not None:
            boot_payload.append(bmap[b].to_payload(n, offs))
    batch = {"tables": tables, "gaps": gaps, "boots": boot_payload, "polls": len(sent),
             "polls_from": min((p["true_ts"] for p in sent), default=None), "polls_to": max(
                 (p["true_ts"] for p in sent), default=None)}
    return batch, new, info
