"""Local SQLite buffer for the collector (store-and-forward, Milestone 2).

One transaction per poll. The tables are the sensor's time series:

  polls                  one row per poll: collector + Kismet + datasource health
  devices                current identity and (for APs) advertised configuration
  device_config_history  previous AP configuration, appended when it changes
  observations           one row per active device per poll - RSSI, counters,
                         frequency, AP load; the core time series
  device_freq_hist       per-device frequency histogram (Kismet freq_khz_map)
  associations           one row per (AP, client MAC) pair with the first/last
                         poll at which the AP's associated-client map listed it
  probes                 SSIDs a client probed for, with first/last time
  alerts                 Kismet WIDS alerts, deduplicated by hash
  ap_load_hourly         per AP, UTC hour and advertised channel: client count,
                         BSS Load station count and channel utilisation (v7)
  meta                   schema version, resume point, server identity

Kismet's associated_client_map carries no per-client times and never drops an
entry while the AP device lives, so associations.last_seen means "the last poll
at which the AP still listed this client", not "last frame exchanged". Storing
the pair once (schema v2) instead of per poll (v1) cut the table from ~85 % of
the buffer to a negligible share.

Schema v3 adds observations.disconnects_last (unix second of the AP's last
deauth/disassoc frame, Kismet client_disconnects_last). A change of it between
two polls is an exact "deauth/disassoc activity in this interval" bit, which
the burst-size field disconnects cannot give. The v2 -> v3 migration is a
single ALTER TABLE ADD COLUMN: metadata only, no rewrite, old rows read NULL.

Schema v4 adds polls.ds_hop_n / ds_hop_visited / ds_hop_ok (hop_coverage.py):
the live hop-list length, the entries the helper really visits, and whether
the live list is the configured one. Kismet 2025-09 can lose channel coverage
without the packet counter noticing (docs/findings.md section 7); these columns
make such windows visible in the data. Same metadata-only migration as v3.

Schema v5 adds observations.rssi_floor: the adapter's floor values (-106 CCK,
-120 OFDM; dataset_rules.py) are censored readings ("at or below the floor"),
not levels. From v5 rssi is NULL for them and rssi_floor = 1 keeps the fact,
so the per-AP floor share (an eligibility criterion) stays countable. Rows
written before v5 keep the raw value in rssi; readers apply
dataset_rules.rssi_value() / sql_rssi().

Schema v6 slims the observations: a row keeps ts, key, last_time (Kismet's
last_time, still needed to tell whether a device has new packets), rssi /
rssi_floor, pk_total, pk_data, disconnects / disconnects_last and
bss_timestamp. The other columns stay in the table (no rewrite of the
million-row table on the Pi, and the v5 code can still run on the file) but
are written as NULL and age out with the retention. An AP's client count,
QBSS station count and channel utilisation are kept as the latest value on
devices (cur_n_clients, cur_qbss_stations, cur_util_pct, cur_at), written by
the devices upsert of every poll - never through merge_config or the
configuration history. A client's BSSID goes to client_bssids, one row per
(client, BSSID) pair with first/last poll, as associations does for APs.

Schema v7 records the AP load history again, compactly: ap_load_hourly holds
one row per AP, UTC hour and advertised channel (the merged
devices.adv_channel at the poll, '' if unknown; campus APs change channel
many times a day, so one hour can have two rows) with the number of polls,
max and sum of Kismet's client count, and - only for polls whose beacon
carried a BSS Load element (shape.py, dot11e_qbss) - max and sum of the
AP-reported station count and channel utilisation. Sums and counts, not
means, so the server can merge partial hours exactly. A row is written in
the same poll and under the same condition as the observation row (the
device has new packets), which is also when the v5 per-poll columns were
written. The table is created by _SCHEMA; the migration only bumps the
version (roll back by setting meta.schema_version to 6 - the v6 code ignores
the table). Pruned with the time series.

Device configuration (the AP's advertised channel, HT mode, beacon rate, ...)
is merged field by field: a NULL from Kismet means "not in this record",
never "changed to nothing". Only a known value replaced by a different known
value is a change (history row, config_changed_at). Until 2026-09-27 an
alternating empty/real beacon record wrote a history row on almost every poll
(docs/findings.md section 11); such rows in older buffers are that artefact.

freq_khz is Kismet's kismet.device.base.frequency: the frequency attributed
to the frame that last updated the device's frequency (Kismet prefers the
frame's own channel information, else the tuned channel, and updates it from
other frames than the signal). It is neither the reception channel of rssi
nor reliably the AP's operating channel - use devices.adv_channel for that.

Schema v8 (live upload, server/docs/live-upload.md 4.1 and 8.3) keeps the
data that lets the uploader place every poll on the true time line even when
the clock was wrong (the Pi has no RTC: a boot without NTP runs on a stale
date, findings.md section 13): per poll the monotonic clock (CLOCK_MONOTONIC,
read next to the wall clock), the boot id and whether chrony was synchronised;
per boot one anchor (wall and monotonic time of its first synchronised poll).
The rows a poll writes carry its id (observations, device_config_history,
alerts .poll_id), so they are placed with their poll, never by a stale ts. A
"new observation" is decided per boot (devices.boot_id / boot_last: Kismet's
last_time of the device in this boot - comparing with an earlier boot's time
on a stale clock dropped every known device); the AP load of an
unsynchronised poll waits in ap_load_pending and is folded into
ap_load_hourly with its true hour when the boot's anchor is recorded; prune()
judges such polls by their corrected time and append order, not by their
stale ts. meta.buffer_id identifies this buffer (row ids restart with a new
one). The migration only adds columns and tables; roll back by setting
meta.schema_version to 7 (the v7 code ignores them).

The "sent" columns are unused (the uploader keeps its own cursor and opens
the buffer read-only). No detection lives here - the store only writes what
shape.py produced.
"""

import logging
import os
import sqlite3
import uuid

log = logging.getLogger("collector.store")

SCHEMA_VERSION = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS polls (
  id             INTEGER PRIMARY KEY,
  ts             INTEGER NOT NULL,   -- collector time (unix s)
  kismet_ts      INTEGER,            -- kismet.system.timestamp.sec
  devices_total  INTEGER,            -- kismet.system.devices.count
  devices_active INTEGER,            -- devices returned by the last-time query
  new_obs        INTEGER,            -- observation rows written
  ds_running     INTEGER,            -- 1 when every datasource is running
  ds_error       TEXT,               -- first datasource error reason, if any
  ds_packets     INTEGER,            -- sum of datasource num_packets
  ds_hop_n       INTEGER,            -- live hop-list length (v4)
  ds_hop_visited INTEGER,            -- entries the helper really visits (v4)
  ds_hop_ok      INTEGER,            -- 1 = configured list, fully visited; 0 = degraded;
                                     -- NULL = no explicit list configured (v4)
  duration_ms    INTEGER,
  ok             INTEGER NOT NULL DEFAULT 1,
  error          TEXT,
  mono           REAL,               -- CLOCK_MONOTONIC at ts (v8)
  boot_id        TEXT,               -- /proc/sys/kernel/random/boot_id (v8)
  clock_synced   INTEGER,            -- 1 = chrony synchronised, |offset| < 0.5 s (v8)
  chrony_offset  REAL);              -- chrony's system time offset, s (v8)
CREATE INDEX IF NOT EXISTS polls_ts ON polls(ts);

CREATE TABLE IF NOT EXISTS devices (
  key        TEXT PRIMARY KEY,
  mac        TEXT NOT NULL,
  type       TEXT NOT NULL,          -- ap | client | bridged | ...
  manuf      TEXT,
  first_seen INTEGER NOT NULL,       -- kismet first_time
  last_seen  INTEGER NOT NULL,       -- kismet last_time of the newest observation
  -- advertised AP configuration, last known values (NULL for non-APs)
  ssid        TEXT,
  cloaked     INTEGER,
  crypt       TEXT,
  crypt_bits  INTEGER,
  mfp_sup     INTEGER,
  mfp_req     INTEGER,
  adv_channel TEXT,
  ht_mode     TEXT,
  beacon_rate INTEGER,
  country     TEXT,
  config_changed_at INTEGER,         -- poll ts of the last configuration change
  -- AP load, latest value, written every poll the AP is seen (v6)
  cur_n_clients     INTEGER,         -- Kismet num_associated_clients
  cur_qbss_stations INTEGER,         -- AP-reported station count (QBSS IE)
  cur_util_pct      REAL,            -- AP-reported channel utilisation (QBSS IE)
  cur_at            INTEGER,         -- poll ts of these values
  boot_id    TEXT,                   -- the boot of boot_last (v8)
  boot_last  INTEGER);               -- Kismet last_time of the newest observation in that boot (v8)

CREATE TABLE IF NOT EXISTS device_config_history (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL,      -- poll ts at which the new config was seen
  key         TEXT NOT NULL,
  -- the configuration that was replaced
  ssid        TEXT,
  cloaked     INTEGER,
  crypt       TEXT,
  crypt_bits  INTEGER,
  mfp_sup     INTEGER,
  mfp_req     INTEGER,
  adv_channel TEXT,
  ht_mode     TEXT,
  beacon_rate INTEGER,
  country     TEXT,
  poll_id     INTEGER);              -- the poll that wrote the row (v8)
CREATE INDEX IF NOT EXISTS device_config_history_key ON device_config_history(key, ts);

-- From v6 only ts, key, last_time, rssi, rssi_floor, pk_total, pk_data,
-- disconnects, disconnects_last and bss_timestamp are written; the other
-- columns are NULL in new rows (kept for the v5 code and the older rows).
CREATE TABLE IF NOT EXISTS observations (
  id        INTEGER PRIMARY KEY,
  ts        INTEGER NOT NULL,        -- poll ts
  key       TEXT NOT NULL,
  last_time INTEGER NOT NULL,        -- kismet last_time
  freq_khz  INTEGER,                 -- Kismet device frequency, NOT the rssi frame's channel
  channel   TEXT,
  rssi      INTEGER,                 -- NULL when no reading or a floor value (v5), never 0
  rssi_min  INTEGER,
  rssi_max  INTEGER,
  rssi_floor INTEGER,                -- 1 = sig_last was an adapter floor value (v5)
  pk_total  INTEGER,                 -- cumulative counters
  pk_tx     INTEGER,
  pk_rx     INTEGER,
  pk_data   INTEGER,
  bytes     INTEGER,
  -- AP only (NULL otherwise)
  n_clients     INTEGER,
  disconnects   INTEGER,             -- client_disconnects: size of the current deauth/disassoc burst
  disconnects_last INTEGER,          -- unix s of the last deauth/disassoc frame (NULL = none yet; v3)
  qbss_stations INTEGER,             -- AP-reported station count
  util_pct      REAL,                -- AP-reported channel utilisation
  bss_timestamp INTEGER,             -- AP uptime (us); a drop means a restart
  ie_checksum   INTEGER,             -- beacon IE checksum (varies per beacon)
  beacon_fp     INTEGER,             -- beacon fingerprint
  -- client only
  bssid     TEXT,
  sent      INTEGER NOT NULL DEFAULT 0,
  poll_id   INTEGER);                -- the poll that wrote the row (v8)
CREATE INDEX IF NOT EXISTS observations_key_ts ON observations(key, ts);
CREATE INDEX IF NOT EXISTS observations_ts ON observations(ts);
CREATE INDEX IF NOT EXISTS observations_unsent ON observations(sent) WHERE sent = 0;

CREATE TABLE IF NOT EXISTS device_freq_hist (
  key        TEXT NOT NULL,
  freq_khz   INTEGER NOT NULL,
  packets    INTEGER NOT NULL,       -- cumulative, as reported by Kismet
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (key, freq_khz)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS associations (
  ap_key     TEXT NOT NULL,
  client_mac TEXT NOT NULL,
  first_seen INTEGER NOT NULL,       -- poll ts at which the pair was first listed
  last_seen  INTEGER NOT NULL,       -- poll ts at which the pair was last listed
  sent       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ap_key, client_mac)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS client_bssids (
  client_key TEXT NOT NULL,          -- device key of the client
  bssid      TEXT NOT NULL,          -- Kismet last_bssid of the client
  first_seen INTEGER NOT NULL,       -- poll ts at which the pair was first seen
  last_seen  INTEGER NOT NULL,       -- poll ts at which the pair was last seen
  sent       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (client_key, bssid)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS probes (
  key        TEXT NOT NULL,          -- client device key
  ssid       TEXT NOT NULL,          -- "" is a wildcard probe
  first_time INTEGER NOT NULL,
  last_time  INTEGER NOT NULL,
  sent       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key, ssid)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS alerts (
  id              INTEGER PRIMARY KEY,
  hash            TEXT NOT NULL UNIQUE,
  ts              REAL,
  header          TEXT NOT NULL,     -- e.g. DEAUTHFLOOD, CHANCHANGE
  class           TEXT,
  severity        INTEGER,
  source_mac      TEXT,
  dest_mac        TEXT,
  transmitter_mac TEXT,
  other_mac       TEXT,
  channel         TEXT,
  freq_khz        INTEGER,
  device_key      TEXT,
  text            TEXT,
  raw             TEXT NOT NULL,     -- complete Kismet JSON
  sent            INTEGER NOT NULL DEFAULT 0,
  poll_id         INTEGER);          -- the poll that wrote the row (v8)
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);

-- v7: AP load per UTC hour and advertised channel. Sums + counts (means are
-- sum / polls), so partial hours merge exactly on the server.
CREATE TABLE IF NOT EXISTS ap_load_hourly (
  key           TEXT NOT NULL,       -- AP device key
  hour          INTEGER NOT NULL,    -- unix s of the UTC hour start
  channel       TEXT NOT NULL,       -- merged devices.adv_channel at the poll, '' = unknown
  polls         INTEGER NOT NULL,    -- polls with a new observation of the AP on this channel
  clients_polls INTEGER NOT NULL DEFAULT 0,  -- polls with a Kismet client count
  clients_max   INTEGER,             -- Kismet num_associated_clients
  clients_sum   INTEGER,
  qbss_polls    INTEGER NOT NULL DEFAULT 0,  -- polls whose beacon carried a BSS Load element
  qbss_max      INTEGER,             -- AP-reported station count
  qbss_sum      INTEGER,
  util_max      REAL,                -- AP-reported channel utilisation, %
  util_sum      REAL,
  first_ts      INTEGER NOT NULL,    -- first / last poll ts in the row
  last_ts       INTEGER NOT NULL,
  sent          INTEGER NOT NULL DEFAULT 0,  -- unused (the uploader keeps its own cursor)
  PRIMARY KEY (key, hour, channel)) WITHOUT ROWID;

-- v8: one clock anchor per boot - the wall and monotonic time of the boot's
-- first poll with a synchronised clock; true time of any poll of the boot =
-- wall - mono + polls.mono.
CREATE TABLE IF NOT EXISTS clock_anchors (
  boot_id  TEXT PRIMARY KEY,
  mono     REAL NOT NULL,
  wall     REAL NOT NULL,
  poll_id  INTEGER NOT NULL,
  source   TEXT NOT NULL DEFAULT 'ntp');

-- v8: the AP load of polls whose clock was not synchronised, until the boot's
-- anchor gives their true hour (then folded into ap_load_hourly, deleted).
CREATE TABLE IF NOT EXISTS ap_load_pending (
  poll_id       INTEGER NOT NULL,
  key           TEXT NOT NULL,
  channel       TEXT NOT NULL,
  n_clients     INTEGER,
  qbss          INTEGER,
  qbss_stations INTEGER,
  util_pct      REAL,
  PRIMARY KEY (poll_id, key)) WITHOUT ROWID;
"""

_V8_COLUMNS = (("polls", "mono", "REAL"), ("polls", "boot_id", "TEXT"), ("polls", "clock_synced", "INTEGER"),
               ("polls", "chrony_offset", "REAL"), ("observations", "poll_id", "INTEGER"),
               ("device_config_history", "poll_id", "INTEGER"), ("alerts", "poll_id", "INTEGER"),
               ("devices", "boot_id", "TEXT"), ("devices", "boot_last", "INTEGER"))

# One poll added to an ap_load_hourly row. SQLite's scalar max(a, b) is NULL
# when either argument is: max(coalesce(a, b), coalesce(b, a)) keeps the
# known one. A sum stays NULL until the first known value.
_LOAD_UPSERT = (
    "INSERT INTO ap_load_hourly(key, hour, channel, polls, clients_polls, clients_max, "
    "clients_sum, qbss_polls, qbss_max, qbss_sum, util_max, util_sum, first_ts, last_ts) "
    "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(key, hour, channel) DO UPDATE SET "
    "polls = polls + 1, "
    "clients_polls = clients_polls + excluded.clients_polls, "
    "clients_max = max(coalesce(clients_max, excluded.clients_max), "
    "coalesce(excluded.clients_max, clients_max)), "
    "clients_sum = CASE WHEN excluded.clients_sum IS NULL THEN clients_sum "
    "ELSE coalesce(clients_sum, 0) + excluded.clients_sum END, "
    "qbss_polls = qbss_polls + excluded.qbss_polls, "
    "qbss_max = max(coalesce(qbss_max, excluded.qbss_max), coalesce(excluded.qbss_max, qbss_max)), "
    "qbss_sum = CASE WHEN excluded.qbss_sum IS NULL THEN qbss_sum "
    "ELSE coalesce(qbss_sum, 0) + excluded.qbss_sum END, "
    "util_max = max(coalesce(util_max, excluded.util_max), coalesce(excluded.util_max, util_max)), "
    "util_sum = CASE WHEN excluded.util_sum IS NULL THEN util_sum "
    "ELSE coalesce(util_sum, 0) + excluded.util_sum END, "
    "first_ts = min(first_ts, excluded.first_ts), "
    "last_ts = max(last_ts, excluded.last_ts), "
    "sent = 0")

_CONFIG_COLS = ("ssid", "cloaked", "crypt", "crypt_bits", "mfp_sup", "mfp_req",
                "adv_channel", "ht_mode", "beacon_rate", "country")
# Latest AP load on devices (v6): overwritten every poll, never merged or historised.
_CUR_COLS = ("cur_n_clients", "cur_qbss_stations", "cur_util_pct", "cur_at")


def merge_config(old, new):
    """Field-wise merge of an AP's advertised configuration. A None in NEW means
    "not in this beacon record" and keeps the OLD value. Returns (merged,
    changed): changed is True only when a known value was replaced by a
    different known value - an unknown value becoming known is not a change."""
    merged = tuple(o if n is None else n for o, n in zip(old, new))
    changed = any(o is not None and n is not None and n != o for o, n in zip(old, new))
    return merged, changed


class Store:
    def __init__(self, path, boot_id=None):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        # Explicit transactions (isolation_level=None = autocommit outside them).
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # meta first: the version decides whether a migration must run before
        # the CREATE TABLE IF NOT EXISTS statements are allowed to see old tables.
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        v = self.get_meta("schema_version")
        if v == "1":
            self._migrate_v1_to_v2()
            v = "2"
        if v == "2":
            self._migrate_v2_to_v3()
            v = "3"
        if v == "3":
            self._migrate_v3_to_v4()
            v = "4"
        if v == "4":
            self._migrate_v4_to_v5()
            v = "5"
        if v == "5":
            self._migrate_v5_to_v6()
            v = "6"
        if v == "6":
            self._migrate_v6_to_v7()
            v = "7"
        if v == "7":
            self._migrate_v7_to_v8(boot_id)
            v = "8"
        if v is not None and int(v) != SCHEMA_VERSION:
            raise RuntimeError("database schema version %s, collector expects %d"
                               % (v, SCHEMA_VERSION))
        self.db.executescript(_SCHEMA)
        if v is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        if self.get_meta("buffer_id") is None:
            self.set_meta("buffer_id", str(uuid.uuid4()))

    def _migrate_v1_to_v2(self):
        """v1 stored associations per poll (PK ts, ap_key, client_mac) - about
        85 % of the buffer. v2 stores one row per pair. The old rows are dropped,
        not condensed: a GROUP BY over millions of rows would stall the collector
        for minutes at start on the Pi, and the pair history is not worth the
        collection gap. DROP only moves pages to the freelist; the file shrinks
        only with a manual VACUUM (see README)."""
        log.info("migrating buffer schema v1 -> v2: dropping per-poll associations")
        self.db.execute("BEGIN")
        try:
            self.db.execute("DROP TABLE IF EXISTS associations")
            self.set_meta("schema_version", "2")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_v2_to_v3(self):
        """v3 adds observations.disconnects_last. ALTER TABLE ADD COLUMN only
        touches the schema (no rewrite of the ~million-row table), so the
        collector resumes polling immediately; existing rows read NULL. A
        buffer without the table yet (fresh, or a v1 file that held only
        associations) gets the column from the CREATE TABLE in _SCHEMA."""
        log.info("migrating buffer schema v2 -> v3: adding observations.disconnects_last")
        self.db.execute("BEGIN")
        try:
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(observations)")]
            if cols and "disconnects_last" not in cols:
                self.db.execute("ALTER TABLE observations ADD COLUMN disconnects_last INTEGER")
            self.set_meta("schema_version", "3")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_v3_to_v4(self):
        """v4 adds polls.ds_hop_n / ds_hop_visited / ds_hop_ok (hop-list
        coverage). ALTER TABLE ADD COLUMN only, like v2 -> v3; older polls read
        NULL. A buffer without the table yet gets the columns from _SCHEMA."""
        log.info("migrating buffer schema v3 -> v4: adding polls.ds_hop_*")
        self.db.execute("BEGIN")
        try:
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(polls)")]
            for c in ("ds_hop_n", "ds_hop_visited", "ds_hop_ok"):
                if cols and c not in cols:
                    self.db.execute("ALTER TABLE polls ADD COLUMN %s INTEGER" % c)
            self.set_meta("schema_version", "4")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_v4_to_v5(self):
        """v5 adds observations.rssi_floor (censored floor readings). ALTER TABLE
        ADD COLUMN only; older rows read NULL and keep their raw rssi."""
        log.info("migrating buffer schema v4 -> v5: adding observations.rssi_floor")
        self.db.execute("BEGIN")
        try:
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(observations)")]
            if cols and "rssi_floor" not in cols:
                self.db.execute("ALTER TABLE observations ADD COLUMN rssi_floor INTEGER")
            self.set_meta("schema_version", "5")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_v5_to_v6(self):
        """v6 adds devices.cur_* (latest AP load) and the client_bssids table
        (from _SCHEMA). ALTER TABLE ADD COLUMN only: the observations columns
        that v6 no longer writes stay, so nothing is rewritten. To roll the
        code back, set meta.schema_version to 5; the extra columns and table
        are harmless to the v5 code."""
        log.info("migrating buffer schema v5 -> v6: adding devices.cur_*, client_bssids")
        self.db.execute("BEGIN")
        try:
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(devices)")]
            for c, t in (("cur_n_clients", "INTEGER"), ("cur_qbss_stations", "INTEGER"),
                         ("cur_util_pct", "REAL"), ("cur_at", "INTEGER")):
                if cols and c not in cols:
                    self.db.execute("ALTER TABLE devices ADD COLUMN %s %s" % (c, t))
            self.set_meta("schema_version", "6")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_v6_to_v7(self):
        """v7 adds the ap_load_hourly table, created by _SCHEMA right after
        this; nothing existing changes. To roll the code back, set
        meta.schema_version to 6; the v6 code ignores the table."""
        log.info("migrating buffer schema v6 -> v7: adding ap_load_hourly")
        self.db.execute("BEGIN")
        try:
            self.set_meta("schema_version", "7")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_v7_to_v8(self, boot_id):
        """v8 adds the clock columns of polls, poll_id on the rows a poll
        writes, devices.boot_id / boot_last, clock_anchors and
        ap_load_pending (from _SCHEMA). ALTER TABLE ADD COLUMN only. The
        devices seen so far count as seen in the boot the migration runs in
        (it runs at a collector restart, within that boot), so the first v8
        poll writes no extra observation; that boot is also the one the
        resume point belongs to. Roll back by setting meta.schema_version to 7."""
        log.info("migrating buffer schema v7 -> v8: clock data, poll ids, per-boot observations")
        self.db.execute("BEGIN")
        try:
            for table, col, typ in _V8_COLUMNS:
                cols = [r[1] for r in self.db.execute("PRAGMA table_info(%s)" % table)]
                if cols and col not in cols:
                    self.db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
            if boot_id:
                self.db.execute("UPDATE devices SET boot_id = ?, boot_last = last_seen", (boot_id,))
                self.set_meta("last_boot_id", boot_id)
            self.set_meta("schema_version", "8")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def boot_started(self, boot_id):
        """True when this boot is new to the buffer: the resume point
        (last_kismet_ts) belongs to an earlier boot and is dropped - Kismet
        restarted with fresh state, and on a stale clock a resume point of the
        true time would ask Kismet for devices weeks in its future."""
        if not boot_id or self.get_meta("last_boot_id") == boot_id:
            return False
        self.db.execute("BEGIN")
        try:
            self.set_meta("last_boot_id", boot_id)
            self.db.execute("DELETE FROM meta WHERE key = 'last_kismet_ts'")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return True

    # --- meta -------------------------------------------------------------

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, str(value)))

    # --- polls ------------------------------------------------------------

    def write_poll(self, ts, health, records, alerts, duration_ms, clock=None):
        """Store the result of one successful poll in a single transaction.

        health:  dict with kismet_ts, devices_total, ds_running, ds_error,
                 ds_packets and optionally ds_hop_n, ds_hop_visited, ds_hop_ok
                 (see collector.py)
        records: list of shape.shape_device() dicts
        alerts:  list of shape.shape_alert() dicts
        clock:   dict with boot_id, mono, synced, chrony_offset, wall (v8,
                 collector.py / clock.py); None = no clock data (tests, tools):
                 treated as synchronised, the v7 behaviour
        The poll row is written first, so every row of the poll carries its id.
        Returns (new_observations, new_alerts).
        """
        new_obs = 0
        new_alerts = 0
        clk = clock or {}
        self.db.execute("BEGIN")
        try:
            poll_id = self.db.execute(
                "INSERT INTO polls(ts, kismet_ts, devices_total, devices_active, new_obs, "
                "ds_running, ds_error, ds_packets, ds_hop_n, ds_hop_visited, ds_hop_ok, "
                "duration_ms, ok, mono, boot_id, clock_synced, chrony_offset) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (ts, health.get("kismet_ts"), health.get("devices_total"), len(records),
                 health.get("ds_running"), health.get("ds_error"),
                 health.get("ds_packets"), health.get("ds_hop_n"), health.get("ds_hop_visited"),
                 health.get("ds_hop_ok"), duration_ms) + self._clock_cols(clock)).lastrowid
            for rec in records:
                if self._write_device(rec, poll_id, clock):
                    new_obs += 1
            for a in alerts:
                new_alerts += self._write_alert(a, poll_id)
            self.db.execute("UPDATE polls SET new_obs = ? WHERE id = ?", (new_obs, poll_id))
            if clock and clk.get("synced") and clk.get("boot_id"):
                self._anchor(clk, poll_id)
            if health.get("kismet_ts") is not None:
                self.set_meta("last_kismet_ts", health["kismet_ts"])
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return new_obs, new_alerts

    @staticmethod
    def _clock_cols(clock):
        if not clock:
            return (None, None, None, None)
        return (clock.get("mono"), clock.get("boot_id"), 1 if clock.get("synced") else 0,
                clock.get("chrony_offset"))

    def _anchor(self, clk, poll_id):
        """The boot's anchor at its first synchronised poll (once per boot);
        then the AP load its unsynchronised polls left pending goes into
        ap_load_hourly on the true hour (wall - mono + the poll's mono)."""
        boot = clk["boot_id"]
        if self.db.execute("SELECT 1 FROM clock_anchors WHERE boot_id = ?", (boot,)).fetchone():
            return
        wall = clk.get("wall")
        if wall is None or clk.get("mono") is None:
            return
        self.db.execute("INSERT INTO clock_anchors(boot_id, mono, wall, poll_id, source) VALUES (?, ?, ?, ?, 'ntp')",
                        (boot, clk["mono"], wall, poll_id))
        epoch = wall - clk["mono"]
        pending = self.db.execute(
            "SELECT p.poll_id, p.key, p.channel, p.n_clients, p.qbss, p.qbss_stations, p.util_pct, q.mono "
            "FROM ap_load_pending p JOIN polls q ON q.id = p.poll_id WHERE q.boot_id = ?", (boot,)).fetchall()
        for _poll, key, channel, n_clients, qbss, stations, util, mono in pending:
            if mono is None:
                continue
            self._write_load(key, int(round(epoch + mono)), channel,
                             {"n_clients": n_clients, "qbss": qbss, "qbss_stations": stations, "util_pct": util})
        if pending:
            self.db.execute("DELETE FROM ap_load_pending WHERE poll_id IN "
                            "(SELECT id FROM polls WHERE boot_id = ?)", (boot,))
            log.info("clock anchor for boot %s: %d pending AP load rows on their true hours", boot, len(pending))

    def write_failed_poll(self, ts, error, duration_ms, clock=None):
        """Record a poll that raised before anything could be shaped (explicit
        transaction, same as write_poll)."""
        self.db.execute("BEGIN")
        try:
            self.db.execute(
                "INSERT INTO polls(ts, duration_ms, ok, error, mono, boot_id, clock_synced, chrony_offset) "
                "VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
                (ts, duration_ms, error[:500]) + self._clock_cols(clock))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # --- devices ----------------------------------------------------------

    def _write_device(self, rec, poll_id=None, clock=None):
        """Upsert the device row and, when the device has new packets since
        its last stored observation (in this boot, v8), append the time-series
        rows, each with the poll's id. Returns True when an observation was
        written."""
        key = rec["key"]
        ap = rec.get("ap")
        boot = (clock or {}).get("boot_id")
        synced = clock is None or bool(clock.get("synced"))
        row = self.db.execute(
            "SELECT last_seen, first_seen, " + ", ".join(_CONFIG_COLS) +
            ", boot_id, boot_last FROM devices WHERE key = ?", (key,)).fetchone()

        new_config = None
        if ap is not None:
            new_config = (ap["ssid"], ap["cloaked"], ap["crypt"], ap["crypt_bits"],
                          ap["mfp"][0], ap["mfp"][1], ap["adv_ch"], ap["ht"],
                          ap["beacon_rate"], ap["country"])

        # AP load: the latest value, not configuration (no history, no merge)
        cur = ((ap["n_clients"], ap["qbss_stations"], ap["util_pct"], rec["ts"])
               if ap is not None else None)

        first_seen = rec["first"] or rec["last"]
        channel = new_config[6] if new_config else None   # adv_channel after the merge
        if row is None:
            self.db.execute(
                "INSERT INTO devices(key, mac, type, manuf, first_seen, last_seen, " +
                ", ".join(_CONFIG_COLS) + ", config_changed_at, " + ", ".join(_CUR_COLS) +
                ") VALUES (" + ", ".join("?" * (6 + len(_CONFIG_COLS) + 1 + len(_CUR_COLS))) + ")",
                (key, rec["mac"], rec["type"], rec["manuf"], first_seen, rec["last"])
                + (new_config or (None,) * len(_CONFIG_COLS))
                + (rec["ts"] if new_config else None,)
                + (cur or (None,) * len(_CUR_COLS)))
            if boot:
                self.db.execute("UPDATE devices SET boot_id = ?, boot_last = ? WHERE key = ?",
                                (boot, rec["last"], key))
            is_new_obs = True
        else:
            last_seen = row[0]
            old_config = tuple(row[2:2 + len(_CONFIG_COLS)])
            row_boot, row_boot_last = row[-2], row[-1]
            if not boot:
                is_new_obs = rec["last"] > last_seen
            elif row_boot == boot and row_boot_last is not None:
                is_new_obs = rec["last"] > row_boot_last
            else:
                is_new_obs = True        # first sighting in this boot (Kismet restarts with each boot)
            sets = ["mac = ?", "type = ?", "manuf = COALESCE(?, manuf)",
                    "first_seen = MIN(first_seen, ?)", "last_seen = MAX(last_seen, ?)"]
            args = [rec["mac"], rec["type"], rec["manuf"], first_seen, rec["last"]]
            if new_config is not None:
                merged, changed = merge_config(old_config, new_config)
                if changed:
                    self.db.execute(
                        "INSERT INTO device_config_history(ts, key, " +
                        ", ".join(_CONFIG_COLS) + ", poll_id) VALUES (" +
                        ", ".join("?" * (3 + len(_CONFIG_COLS))) + ")",
                        (rec["ts"], key) + old_config + (poll_id,))
                    sets += ["config_changed_at = ?"]
                    args += [rec["ts"]]
                if merged != old_config:
                    sets += ["%s = ?" % c for c in _CONFIG_COLS]
                    args += list(merged)
                channel = merged[6]
            if cur is not None:
                sets += ["%s = ?" % c for c in _CUR_COLS]
                args += list(cur)
            if boot:
                sets += ["boot_last = CASE WHEN boot_id = ? THEN MAX(coalesce(boot_last, ?), ?) ELSE ? END",
                         "boot_id = ?"]
                args += [boot, rec["last"], rec["last"], rec["last"], boot]
            args.append(key)
            self.db.execute("UPDATE devices SET " + ", ".join(sets) + " WHERE key = ?", args)

        if not is_new_obs:
            return False

        cl = rec.get("cl") or {}
        # v6: the slim row; the table's other columns stay NULL
        self.db.execute(
            "INSERT INTO observations(ts, key, last_time, rssi, rssi_floor, pk_total, pk_data, "
            "disconnects, disconnects_last, bss_timestamp, poll_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["ts"], key, rec["last"], rec["rssi"], rec.get("rssi_floor"),
             rec["pk"], rec["data"],
             ap["disconnects"] if ap else None,
             ap["disconnects_last"] if ap else None,
             ap["bss_ts"] if ap else None, poll_id))
        if ap is not None:
            if synced or poll_id is None:
                self._write_load(key, rec["ts"], channel, ap)
            else:
                # v8: the hour of a stale ts is wrong - wait for the boot's anchor
                self.db.execute(
                    "INSERT OR REPLACE INTO ap_load_pending(poll_id, key, channel, n_clients, qbss, "
                    "qbss_stations, util_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (poll_id, key, channel or "", ap["n_clients"], 1 if ap.get("qbss") else 0,
                     ap["qbss_stations"], ap["util_pct"]))
        if cl.get("bssid"):
            self.db.execute(
                "INSERT INTO client_bssids(client_key, bssid, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(client_key, bssid) DO UPDATE SET "
                "first_seen = MIN(first_seen, excluded.first_seen), "
                "last_seen = MAX(last_seen, excluded.last_seen), "
                "sent = CASE WHEN excluded.last_seen > last_seen THEN 0 ELSE sent END",
                (key, cl["bssid"], rec["ts"], rec["ts"]))

        if rec["freqs"]:
            self.db.executemany(
                "INSERT INTO device_freq_hist(key, freq_khz, packets, updated_ts) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(key, freq_khz) DO UPDATE SET "
                "packets = excluded.packets, updated_ts = excluded.updated_ts",
                [(key, f, n, rec["ts"]) for f, n in rec["freqs"].items()])
        if ap and ap["clients"]:
            # TODO(upload): sent resets to 0 whenever last_seen advances, which for
            # a currently associated pair is every poll - settle this before the
            # upload step or associations will be re-uploaded on every poll.
            self.db.executemany(
                "INSERT INTO associations(ap_key, client_mac, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(ap_key, client_mac) DO UPDATE SET "
                "first_seen = MIN(first_seen, excluded.first_seen), "
                "last_seen = MAX(last_seen, excluded.last_seen), "
                "sent = CASE WHEN excluded.last_seen > last_seen THEN 0 ELSE sent END",
                [(key, m, rec["ts"], rec["ts"]) for m in ap["clients"]])
        if cl.get("probes"):
            self.db.executemany(
                "INSERT INTO probes(key, ssid, first_time, last_time) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key, ssid) DO UPDATE SET "
                "first_time = MIN(first_time, excluded.first_time), "
                "last_time = MAX(last_time, excluded.last_time), "
                "sent = CASE WHEN excluded.last_time > last_time THEN 0 ELSE sent END",
                [(key, s, f, l) for s, f, l in cl["probes"]])
        return True

    def _write_load(self, key, ts, channel, ap):
        """Add this poll's AP load to its ap_load_hourly row (v7). The BSS Load
        values count only when the beacon carried the element (ap["qbss"])."""
        clients = ap["n_clients"]
        stations, util = ap["qbss_stations"], ap["util_pct"]
        qbss = 1 if ap.get("qbss") and (stations is not None or util is not None) else 0
        if not qbss:
            stations = util = None
        self.db.execute(_LOAD_UPSERT, (
            key, ts - ts % 3600, channel or "",
            1 if clients is not None else 0, clients, clients,
            qbss, stations, stations, util, util, ts, ts))

    # --- alerts -----------------------------------------------------------

    def _write_alert(self, a, poll_id=None):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO alerts(hash, ts, header, class, severity, source_mac, "
            "dest_mac, transmitter_mac, other_mac, channel, freq_khz, device_key, text, raw, poll_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (a["hash"], a["ts"], a["header"], a["class"], a["severity"], a["source_mac"],
             a["dest_mac"], a["transmitter_mac"], a["other_mac"], a["channel"], a["freq"],
             a["device_key"], a["text"], a["raw"], poll_id))
        return cur.rowcount

    # --- housekeeping -----------------------------------------------------

    def prune(self, before_ts):
        """Delete time-series rows older than before_ts: observations, polls
        and the ap_load_hourly hours that ended by then. Device identity,
        configuration history, associations, client_bssids, probes and alerts
        are kept.

        v8: a poll whose clock was not synchronised has a stale ts (weeks
        back on a boot without NTP) and is judged by its true time instead -
        wall - mono of its boot's anchor + its mono - or, for a boot that never
        got an anchor, by append order: it goes once the first synchronised
        poll after it is older than before_ts. A stale stretch therefore stays
        until the uploader could place it, and no longer than real data.

        TODO(retention): the deduplicated tables (associations, probes, devices)
        are never pruned, so distinct pairs accumulate for the life of the sensor.
        Fine for now; a long-running sensor should eventually prune them by
        last_seen / last_time as well."""
        counts = {}
        self.db.execute("BEGIN")
        try:
            # unsynchronised polls that are old by their true time / append order
            stale = [r[0] for r in self.db.execute(
                "SELECT p.id FROM polls p JOIN clock_anchors a ON a.boot_id = p.boot_id "
                "WHERE p.clock_synced = 0 AND a.wall - a.mono + p.mono < ? "
                "UNION "
                "SELECT p.id FROM polls p "
                "WHERE p.clock_synced = 0 "
                "  AND NOT EXISTS (SELECT 1 FROM clock_anchors a WHERE a.boot_id = p.boot_id) "
                "  AND (SELECT q.ts FROM polls q WHERE q.id > p.id AND coalesce(q.clock_synced, 1) = 1 "
                "       ORDER BY q.id LIMIT 1) < ?", (before_ts, before_ts))]
            unsynced = "SELECT id FROM polls WHERE clock_synced = 0"
            cur = self.db.execute(
                "DELETE FROM observations WHERE ts < ? AND (poll_id IS NULL OR poll_id NOT IN (%s))" % unsynced,
                (before_ts,))
            counts["observations"] = cur.rowcount
            cur = self.db.execute(
                "DELETE FROM polls WHERE ts < ? AND coalesce(clock_synced, 1) = 1", (before_ts,))
            counts["polls"] = cur.rowcount
            if stale:
                marks = ",".join("?" * len(stale))
                counts["observations"] += self.db.execute(
                    "DELETE FROM observations WHERE poll_id IN (%s)" % marks, stale).rowcount
                self.db.execute("DELETE FROM ap_load_pending WHERE poll_id IN (%s)" % marks, stale)
                counts["polls"] += self.db.execute(
                    "DELETE FROM polls WHERE id IN (%s)" % marks, stale).rowcount
            cur = self.db.execute("DELETE FROM ap_load_hourly WHERE hour + 3600 <= ?", (before_ts,))
            counts["ap_load_hourly"] = cur.rowcount
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return counts

    def counts(self):
        out = {}
        for table in ("devices", "observations", "associations", "probes", "alerts", "polls",
                      "ap_load_hourly"):
            out[table] = self.db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        return out
