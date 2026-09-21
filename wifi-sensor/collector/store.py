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

"sent" columns are for the upload step (later); the prototype only prunes by
age. No detection lives here - the store only writes what shape.py produced.
"""

import logging
import os
import sqlite3

log = logging.getLogger("collector.store")

SCHEMA_VERSION = 3

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
  duration_ms    INTEGER,
  ok             INTEGER NOT NULL DEFAULT 1,
  error          TEXT);
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
  config_changed_at INTEGER);        -- poll ts of the last configuration change

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
  country     TEXT);
CREATE INDEX IF NOT EXISTS device_config_history_key ON device_config_history(key, ts);

CREATE TABLE IF NOT EXISTS observations (
  id        INTEGER PRIMARY KEY,
  ts        INTEGER NOT NULL,        -- poll ts
  key       TEXT NOT NULL,
  last_time INTEGER NOT NULL,        -- kismet last_time
  freq_khz  INTEGER,
  channel   TEXT,
  rssi      INTEGER,                 -- NULL when Kismet has no reading, never 0
  rssi_min  INTEGER,
  rssi_max  INTEGER,
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
  sent      INTEGER NOT NULL DEFAULT 0);
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
  sent            INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
"""

_CONFIG_COLS = ("ssid", "cloaked", "crypt", "crypt_bits", "mfp_sup", "mfp_req",
                "adv_channel", "ht_mode", "beacon_rate", "country")


class Store:
    def __init__(self, path):
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
        if v is not None and int(v) != SCHEMA_VERSION:
            raise RuntimeError("database schema version %s, collector expects %d"
                               % (v, SCHEMA_VERSION))
        self.db.executescript(_SCHEMA)
        if v is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

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

    def close(self):
        self.db.close()

    # --- meta -------------------------------------------------------------

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, str(value)))

    # --- polls ------------------------------------------------------------

    def write_poll(self, ts, health, records, alerts, duration_ms):
        """Store the result of one successful poll in a single transaction.

        health:  dict with kismet_ts, devices_total, ds_running, ds_error,
                 ds_packets (see collector.py)
        records: list of shape.shape_device() dicts
        alerts:  list of shape.shape_alert() dicts
        Returns (new_observations, new_alerts).
        """
        new_obs = 0
        new_alerts = 0
        self.db.execute("BEGIN")
        try:
            for rec in records:
                if self._write_device(rec):
                    new_obs += 1
            for a in alerts:
                new_alerts += self._write_alert(a)
            self.db.execute(
                "INSERT INTO polls(ts, kismet_ts, devices_total, devices_active, new_obs, "
                "ds_running, ds_error, ds_packets, duration_ms, ok) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (ts, health.get("kismet_ts"), health.get("devices_total"), len(records),
                 new_obs, health.get("ds_running"), health.get("ds_error"),
                 health.get("ds_packets"), duration_ms))
            if health.get("kismet_ts") is not None:
                self.set_meta("last_kismet_ts", health["kismet_ts"])
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return new_obs, new_alerts

    def write_failed_poll(self, ts, error, duration_ms):
        """Record a poll that raised before anything could be shaped (explicit
        transaction, same as write_poll)."""
        self.db.execute("BEGIN")
        try:
            self.db.execute(
                "INSERT INTO polls(ts, duration_ms, ok, error) VALUES (?, ?, 0, ?)",
                (ts, duration_ms, error[:500]))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # --- devices ----------------------------------------------------------

    def _write_device(self, rec):
        """Upsert the device row and, when the device has new packets since
        its last stored observation, append the time-series rows.
        Returns True when an observation was written."""
        key = rec["key"]
        ap = rec.get("ap")
        row = self.db.execute(
            "SELECT last_seen, first_seen, " + ", ".join(_CONFIG_COLS) +
            " FROM devices WHERE key = ?", (key,)).fetchone()

        new_config = None
        if ap is not None:
            new_config = (ap["ssid"], ap["cloaked"], ap["crypt"], ap["crypt_bits"],
                          ap["mfp"][0], ap["mfp"][1], ap["adv_ch"], ap["ht"],
                          ap["beacon_rate"], ap["country"])

        first_seen = rec["first"] or rec["last"]
        if row is None:
            self.db.execute(
                "INSERT INTO devices(key, mac, type, manuf, first_seen, last_seen, " +
                ", ".join(_CONFIG_COLS) + ", config_changed_at) VALUES (" +
                ", ".join("?" * (6 + len(_CONFIG_COLS) + 1)) + ")",
                (key, rec["mac"], rec["type"], rec["manuf"], first_seen, rec["last"])
                + (new_config or (None,) * len(_CONFIG_COLS))
                + (rec["ts"] if new_config else None,))
            is_new_obs = True
        else:
            last_seen = row[0]
            old_config = tuple(row[2:])
            is_new_obs = rec["last"] > last_seen
            sets = ["mac = ?", "type = ?", "manuf = COALESCE(?, manuf)",
                    "first_seen = MIN(first_seen, ?)", "last_seen = MAX(last_seen, ?)"]
            args = [rec["mac"], rec["type"], rec["manuf"], first_seen, rec["last"]]
            if new_config is not None and new_config != old_config:
                if any(v is not None for v in old_config):
                    self.db.execute(
                        "INSERT INTO device_config_history(ts, key, " +
                        ", ".join(_CONFIG_COLS) + ") VALUES (" +
                        ", ".join("?" * (2 + len(_CONFIG_COLS))) + ")",
                        (rec["ts"], key) + old_config)
                sets += ["%s = ?" % c for c in _CONFIG_COLS] + ["config_changed_at = ?"]
                args += list(new_config) + [rec["ts"]]
            args.append(key)
            self.db.execute("UPDATE devices SET " + ", ".join(sets) + " WHERE key = ?", args)

        if not is_new_obs:
            return False

        cl = rec.get("cl") or {}
        self.db.execute(
            "INSERT INTO observations(ts, key, last_time, freq_khz, channel, "
            "rssi, rssi_min, rssi_max, pk_total, pk_tx, pk_rx, pk_data, bytes, "
            "n_clients, disconnects, disconnects_last, qbss_stations, util_pct, bss_timestamp, "
            "ie_checksum, beacon_fp, bssid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["ts"], key, rec["last"], rec["freq"], rec["ch"],
             rec["rssi"], rec["rssi_min"], rec["rssi_max"],
             rec["pk"], rec["tx"], rec["rx"], rec["data"], rec["bytes"],
             ap["n_clients"] if ap else None,
             ap["disconnects"] if ap else None,
             ap["disconnects_last"] if ap else None,
             ap["qbss_stations"] if ap else None,
             ap["util_pct"] if ap else None,
             ap["bss_ts"] if ap else None,
             ap["ie_sum"] if ap else None,
             ap["beacon_fp"] if ap else None,
             cl.get("bssid")))

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

    # --- alerts -----------------------------------------------------------

    def _write_alert(self, a):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO alerts(hash, ts, header, class, severity, source_mac, "
            "dest_mac, transmitter_mac, other_mac, channel, freq_khz, device_key, text, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (a["hash"], a["ts"], a["header"], a["class"], a["severity"], a["source_mac"],
             a["dest_mac"], a["transmitter_mac"], a["other_mac"], a["channel"], a["freq"],
             a["device_key"], a["text"], a["raw"]))
        return cur.rowcount

    # --- housekeeping -----------------------------------------------------

    def prune(self, before_ts):
        """Delete time-series rows older than before_ts. Device identity,
        configuration history, associations, probes and alerts are kept.

        TODO(retention): the deduplicated tables (associations, probes, devices)
        are never pruned, so distinct pairs accumulate for the life of the sensor.
        Fine for now; a long-running sensor should eventually prune them by
        last_seen / last_time as well."""
        counts = {}
        self.db.execute("BEGIN")
        try:
            for table in ("observations", "polls"):
                cur = self.db.execute("DELETE FROM %s WHERE ts < ?" % table, (before_ts,))
                counts[table] = cur.rowcount
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return counts

    def counts(self):
        out = {}
        for table in ("devices", "observations", "associations", "probes", "alerts", "polls"):
            out[table] = self.db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        return out
