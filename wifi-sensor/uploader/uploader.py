#!/usr/bin/env python3
"""wifi-sensor uploader: each poll from the collector's buffer to the server
(live upload; server/docs/live-upload.md 4, 5).

A separate process from the collector (capture never waits on the network):
every WATCH_S it checks whether the buffer changed (PRAGMA data_version, a
shared-memory read) and sends every new poll at once - one small batch per
poll in steady state, catch-up batches of CATCHUP_POLLS polls with
CATCHUP_PAUSE_S between them after an outage. The buffer is opened read-only;
the cursor of what the server acknowledged lives in STATE_PATH and advances
only on a 2xx, so an outage, a crash or a refused batch never loses data:
the same polls are sent again (the server merges idempotently). Polls whose
clock was wrong are placed on the true time line first (timeline.py); polls
of a boot without a clock anchor wait for one. A heartbeat rides along with
every batch; a standalone one goes when no batch went for HEARTBEAT_S, and a
refused one is retried after 5, 10, 20, 40 s, then about every HEARTBEAT_S.

Configuration - environment, else KEY=value lines in UPLOADER_CONF
(default /etc/wifi-sensor/uploader.conf), DB_PATH / RETENTION_DAYS also from
the collector's COLLECTOR_CONF:

  INGEST_URL         https://<server>/api/ingest/v1 (https only)
  INGEST_TOKEN_FILE  file with the sensor's token     Default: /etc/wifi-sensor/ingest.token
                     (or INGEST_TOKEN itself; never logged)
  DB_PATH            the collector's buffer           Default: /var/lib/wifi-sensor/buffer.db
  STATE_PATH         the cursor file                  Default: /var/lib/wifi-sensor-uploader/state.json
  CAPTURE_IFACE      the capture interface (adapter identity)  Default: wlan1
  WATCH_S 2, HEARTBEAT_S 60, CATCHUP_POLLS 120, CATCHUP_PAUSE_S 2,
  MAX_BATCH_BYTES 1048576, BACKOFF_MAX_S 600, SERVER_ANCHOR_AFTER_S 120,
  RETENTION_DAYS (14), HTTP_TIMEOUT 30, LOG_LEVEL INFO

Run:  uploader.py            the daemon (systemd: wifi-sensor-uploader.service)
      uploader.py --check    one real heartbeat + the first batch built, not sent;
                             prints what would go (no cursor change)
      uploader.py --once     until caught up once, then exit (tests)
"""

import logging
import os
import random
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "collector"))   # clock.py, installed next to it

import batch as batchmod  # noqa: E402
import clock  # noqa: E402
import sensorinfo  # noqa: E402
import state as statemod  # noqa: E402
from client import Client, Refused, describe_token  # noqa: E402

VERSION = "1.1"
log = logging.getLogger("uploader")

DEFAULTS = {
    "UPLOADER_CONF": "/etc/wifi-sensor/uploader.conf",
    "COLLECTOR_CONF": "/etc/wifi-sensor/collector.conf",
    "INGEST_URL": "",
    "INGEST_TOKEN": "",
    "INGEST_TOKEN_FILE": "/etc/wifi-sensor/ingest.token",
    "DB_PATH": "/var/lib/wifi-sensor/buffer.db",
    "STATE_PATH": "/var/lib/wifi-sensor-uploader/state.json",
    "CAPTURE_IFACE": "wlan1",
    "WATCH_S": "2", "HEARTBEAT_S": "60", "CATCHUP_POLLS": "120", "CATCHUP_PAUSE_S": "2",
    "MAX_BATCH_BYTES": "1048576", "BACKOFF_MAX_S": "600", "SERVER_ANCHOR_AFTER_S": "120",
    "RETENTION_DAYS": "14", "HTTP_TIMEOUT": "30", "LOG_LEVEL": "INFO",
}
NUMERIC = ("WATCH_S", "HEARTBEAT_S", "CATCHUP_POLLS", "CATCHUP_PAUSE_S", "MAX_BATCH_BYTES", "BACKOFF_MAX_S",
           "SERVER_ANCHOR_AFTER_S", "RETENTION_DAYS", "HTTP_TIMEOUT")


def read_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except OSError:
        pass
    return out


def load_config(env=None):
    env = os.environ if env is None else env
    cfg = dict(DEFAULTS)
    coll = read_kv(env.get("COLLECTOR_CONF", cfg["COLLECTOR_CONF"]))
    for k in ("DB_PATH", "RETENTION_DAYS"):
        if k in coll:
            cfg[k] = coll[k]
    cfg.update(read_kv(env.get("UPLOADER_CONF", cfg["UPLOADER_CONF"])))
    cfg.update({k: v for k, v in env.items() if k in DEFAULTS})
    for k in NUMERIC:
        cfg[k] = float(cfg[k])
    cfg["TOKEN_ERROR"] = None
    if not cfg["INGEST_TOKEN"]:
        try:
            with open(cfg["INGEST_TOKEN_FILE"]) as f:
                cfg["INGEST_TOKEN"] = f.read().strip()
        except OSError as e:
            cfg["TOKEN_ERROR"] = "%s: %s" % (cfg["INGEST_TOKEN_FILE"], e.strerror)
    return cfg


class Backoff:
    def __init__(self, cap):
        self.cap, self.n, self.until = cap, 0, 0.0

    def fail(self, now, at_least=0.0, at_most=None):
        self.n += 1
        delay = min(at_most or self.cap, 5.0 * 2 ** (self.n - 1))
        delay = max(at_least, delay * random.uniform(0.8, 1.2))
        self.until = now + delay
        return delay

    def ok(self):
        self.n, self.until = 0, 0.0

    def waiting(self, now):
        return now < self.until


class Uploader:
    def __init__(self, cfg, client, current_boot=None):
        self.cfg = cfg
        self.client = client
        self.boot = current_boot if current_boot is not None else clock.boot_id()
        self.state = statemod.load(cfg["STATE_PATH"])
        self.db = None
        self.data_version = None
        self.backoff = Backoff(cfg["BACKOFF_MAX_S"])
        # a refused heartbeat is retried after 5, 10, 20, 40 s, then about every
        # HEARTBEAT_S - not on every tick (the 2026-10-20 outage: one every 2 s)
        self.hb_backoff = Backoff(cfg["HEARTBEAT_S"])
        self.max_polls = int(cfg["CATCHUP_POLLS"])
        self.last_contact = 0.0          # monotonic
        self.contact_since = None        # monotonic, first contact of this process
        self.rtt_ms = None
        self.last_result = "start"
        self.attempts = {}

    # --- buffer ---------------------------------------------------------------------------------

    def open(self):
        if self.db is None:
            self.db = batchmod.open_buffer(self.cfg["DB_PATH"])
        return self.db

    def buffer_ready(self):
        db = self.open()
        v = batchmod.meta(db, "schema_version")
        if v is None or int(v) < 8 or batchmod.meta(db, "buffer_id") is None:
            log.warning("buffer schema %s: the uploader needs buffer v8 (collector of live-upload step 3)", v)
            return False
        return True

    def changed(self):
        v = self.open().execute("PRAGMA data_version").fetchone()[0]
        changed, self.data_version = v != self.data_version, v
        return changed

    # --- heartbeat ------------------------------------------------------------------------------------

    def heartbeat(self):
        db = self.open()
        cur = (self.state or {}).get("cursor") or {}
        synced, offset = clock.chrony_state()
        last = db.execute("SELECT id, ts, ok FROM polls ORDER BY id DESC LIMIT 1").fetchone()
        nxt = db.execute("SELECT count(*), min(ts) FROM polls WHERE id > ?", (cur.get("polls_id", 0),)).fetchone()
        oldest = db.execute("SELECT min(ts) FROM polls WHERE coalesce(clock_synced, 1) = 1").fetchone()[0]
        unverified = sum(((self.state or {}).get("unverified") or {}).values())
        wall, mono = clock.now()
        return {
            "sensor_ts": wall, "mono": mono, "boot_id": self.boot, "uptime_s": sensorinfo.uptime_s(),
            "rtt_ms": self.rtt_ms, "chrony_synced": synced, "chrony_offset_s": offset,
            "last_poll_id": last["id"] if last else None, "last_poll_ts": last["ts"] if last else None,
            "last_poll_ok": bool(last["ok"]) if last else None,
            "buffer_bytes": sensorinfo.buffer_bytes(self.cfg["DB_PATH"]),
            "buffer_version": batchmod.meta(db, "schema_version"), "buffer_id": batchmod.meta(db, "buffer_id"),
            "retention_days": int(self.cfg["RETENTION_DAYS"]), "buffer_oldest_ts": oldest,
            "backlog_polls": nxt[0], "backlog_oldest_ts": nxt[1],
            "held_polls": batchmod.held_polls(db, self.boot), "unverified_polls": unverified,
            "disk_free_bytes": sensorinfo.disk_free(), "uploader_version": VERSION,
            "last_result": self.last_result, "adapter": sensorinfo.adapter(self.cfg["CAPTURE_IFACE"]),
        }

    def send_heartbeat(self):
        status, body, rtt, mid = self.client.post("/heartbeat", {"v": 1, "heartbeat": self.heartbeat()})
        self.rtt_ms = rtt
        if status == 200:
            self.hb_backoff.ok()
            self.contact(mid, body)
            return body
        self.hb_backoff.fail(time.monotonic())
        self.last_result = "heartbeat %s" % (status or (body or {}).get("detail"))
        log.warning("heartbeat refused: %s %s", status, (body or {}).get("detail"))
        return None

    def contact(self, mono_mid, body):
        now = time.monotonic()
        self.last_contact = now
        if self.contact_since is None:
            self.contact_since = now
        # server anchor (8.3): only when this boot's polls are held for one and
        # contact has lasted SERVER_ANCHOR_AFTER_S (NTP gets the first chance)
        if body and body.get("server_ts") and self.state is not None:
            anchors = self.state.setdefault("server_anchors", {})
            if (self.boot and self.boot not in anchors
                    and now - self.contact_since >= self.cfg["SERVER_ANCHOR_AFTER_S"]
                    and batchmod.held_polls(self.open(), self.boot) > 0):
                anchors[self.boot] = {"wall": float(body["server_ts"]), "mono": mono_mid}
                statemod.save(self.cfg["STATE_PATH"], self.state)
                log.warning("boot %s: no NTP - anchored on the server's time (rtt %s ms)", self.boot, self.rtt_ms)

    # --- cursor ----------------------------------------------------------------------------------------

    def ensure_cursor(self):
        """The cursor for this buffer: the saved one, or - first start, a new
        buffer - just after the server's newest poll (D5)."""
        db = self.open()
        bid = batchmod.meta(db, "buffer_id")
        if self.state and (self.state.get("cursor") or {}).get("buffer_id") == bid:
            return True
        if self.hb_backoff.waiting(time.monotonic()):
            return False
        body = self.send_heartbeat()
        if body is None:
            return False
        resume = body.get("resume")
        cur = batchmod.cursor_after(db, bid, resume)
        self.state = {"cursor": cur, "server_anchors": (self.state or {}).get("server_anchors", {}),
                      "unverified": {}, "corrected": {}}
        statemod.save(self.cfg["STATE_PATH"], self.state)
        log.info("new cursor for buffer %s: after poll %s (server's newest %s)", bid, cur["polls_id"], resume)
        return True

    # --- batches -----------------------------------------------------------------------------------------

    def build(self):
        db = self.open()
        cur = self.state["cursor"]
        db.execute("BEGIN")             # one read snapshot for the whole batch
        try:
            b, new, info = batchmod.build(db, cur, self.boot, self.state.get("server_anchors"), self.max_polls)
        finally:
            db.execute("COMMIT")
        if b is None:
            return None, new, info
        corrected = self.state.setdefault("corrected", {})
        for boot, (n, offs) in info["corrected"].items():
            c = corrected.get(boot, {"polls": 0})
            for bp in b["boots"]:
                if bp["boot_id"] == boot:
                    bp["polls_corrected"] = c["polls"] + n
        payload = dict({"v": 1, "batch_seq": cur["batch_seq"] + 1, "buffer_id": cur["buffer_id"],
                        "buffer_version": batchmod.meta(db, "schema_version"), "sent_at": time.time(),
                        "attempt": self.attempts.get(cur["batch_seq"] + 1, 0) + 1,
                        "heartbeat": self.heartbeat()},
                       tables=b["tables"], gaps=b["gaps"], boots=b["boots"])
        return payload, new, info

    def send_batch(self):
        """True when a batch went through and more may be waiting."""
        payload, new, info = self.build()
        if payload is None:
            return False
        while Client.size(payload) > self.cfg["MAX_BATCH_BYTES"] and self.max_polls > 1:
            self.max_polls = max(1, self.max_polls // 2)
            payload, new, info = self.build()
        seq = payload["batch_seq"]
        self.attempts[seq] = payload["attempt"]
        # stamp the clock right before sending: the server compares it with its
        # receive time (offset + one-way latency), building a batch takes time
        payload["heartbeat"]["sensor_ts"], payload["heartbeat"]["mono"] = clock.now()
        status, body, rtt, mid = self.client.post("/batch", payload)
        self.rtt_ms = rtt
        now = time.monotonic()
        polls = len(payload["tables"].get("polls", {}).get("rows", []))
        if status == 200:
            self.contact(mid, body)
            new["batch_seq"] = seq
            self.state["cursor"] = new
            un = self.state.setdefault("unverified", {})
            for boot, n in info["unverified"].items():
                un[boot] = un.get(boot, 0) + n
            for boot, (n, offs) in info["corrected"].items():
                c = self.state.setdefault("corrected", {}).setdefault(boot, {"polls": 0})
                c["polls"] += n
            statemod.save(self.cfg["STATE_PATH"], self.state)
            self.backoff.ok()
            self.attempts.pop(seq, None)
            self.max_polls = int(self.cfg["CATCHUP_POLLS"])
            self.last_result = "ok"
            log.info("batch %d: %d polls, %d gaps, %s ms, inserted %s%s", seq, polls, len(payload["gaps"]), rtt,
                     (body or {}).get("inserted"), " (replay)" if (body or {}).get("replay") else "")
            return True
        detail = (body or {}).get("detail")
        self.last_result = "batch %s: %s" % (status, detail)
        if status == 413:
            self.max_polls = max(1, self.max_polls // 2)
            log.warning("batch %d too large (%d polls): halving", seq, polls)
            return True
        if status in (401, 403):
            delay = self.backoff.fail(now, at_least=self.cfg["BACKOFF_MAX_S"])
            log.error("batch %d: %s %s (%s) - next try in %.0f s", seq, status, detail,
                      describe_token(self.client.token), delay)
        elif status == 409:
            self.backoff.until = now + self.cfg["WATCH_S"]
            log.info("batch %d: 409 %s - next tick", seq, detail)
        elif status == 429:
            delay = self.backoff.fail(now, at_least=5.0)
            log.warning("batch %d: 429 - next try in %.0f s", seq, delay)
        else:     # 0 (network/TLS), 5xx, 422, 400
            delay = self.backoff.fail(now)
            log.warning("batch %d (%d polls): %s %s - next try in %.0f s", seq, polls, status, detail, delay)
        return False

    def backlog(self):
        cur = self.state["cursor"]
        return self.open().execute("SELECT EXISTS (SELECT 1 FROM polls WHERE id > ?)",
                                   (cur["polls_id"],)).fetchone()[0] == 1

    # --- the loop ----------------------------------------------------------------------------------------------

    def tick(self):
        """One round: batches while there is something to send (paced), a
        heartbeat when nothing went for HEARTBEAT_S. Returns True when caught up."""
        now = time.monotonic()
        if self.state is None or not (self.state.get("cursor") or {}).get("buffer_id"):
            if not self.ensure_cursor():
                return False
        caught_up = True
        if not self.backoff.waiting(now) and (self.changed() or self.backlog()):
            while self.send_batch():
                if not self.backlog():
                    break
                caught_up = False
                if _stop:
                    break
                time.sleep(self.cfg["CATCHUP_PAUSE_S"])
            caught_up = not self.backlog() or self.backoff.waiting(time.monotonic())
        now = time.monotonic()
        if now - self.last_contact >= self.cfg["HEARTBEAT_S"] and not self.hb_backoff.waiting(now):
            self.send_heartbeat()
        return caught_up and not self.backlog()


_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True


def check(up):
    """--check: one real heartbeat, the first batch built but not sent."""
    print("uploader %s: buffer %s, state %s, url %s, %s" % (
        VERSION, up.cfg["DB_PATH"], up.cfg["STATE_PATH"], up.client.base, describe_token(up.client.token)))
    if not up.buffer_ready():
        return 1
    body = up.send_heartbeat()
    if body is None:
        print("heartbeat: refused (%s)" % up.last_result)
        return 1
    print("heartbeat: ok, rtt %s ms, server resume point %s" % (up.rtt_ms, body.get("resume")))
    if up.state is None or (up.state.get("cursor") or {}).get("buffer_id") != batchmod.meta(up.open(), "buffer_id"):
        cur = batchmod.cursor_after(up.open(), batchmod.meta(up.open(), "buffer_id"), body.get("resume"))
        up.state = {"cursor": cur, "server_anchors": {}, "unverified": {}, "corrected": {}}
        print("cursor (not saved): after poll %s" % cur["polls_id"])
    payload, new, info = up.build()
    if payload is None:
        print("first batch: nothing to send (held back: %s)" % info["held"])
        return 0
    rows = {k: len(v["rows"]) for k, v in payload["tables"].items()}
    print("first batch (not sent): %d polls, rows %s, gaps %s, %d bytes gzip, held %s" % (
        len(payload["tables"].get("polls", {}).get("rows", [])), rows, payload["gaps"], Client.size(payload),
        info["held"]))
    backlog = up.open().execute("SELECT count(*), min(ts) FROM polls WHERE id > ?",
                                (up.state["cursor"]["polls_id"],)).fetchone()
    print("backlog: %d polls since %s" % (backlog[0], backlog[1]))
    return 0


def main(argv):
    cfg = load_config()
    logging.basicConfig(level=getattr(logging, cfg["LOG_LEVEL"].upper(), logging.INFO),
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    if not cfg["INGEST_URL"] or not cfg["INGEST_TOKEN"]:
        log.error("INGEST_URL and the token (INGEST_TOKEN_FILE) must be set%s",
                  " - cannot read the token file (%s)" % cfg["TOKEN_ERROR"] if cfg["TOKEN_ERROR"] else "")
        return 2
    try:
        client = Client(cfg["INGEST_URL"], cfg["INGEST_TOKEN"], timeout=cfg["HTTP_TIMEOUT"])
    except Refused as e:
        log.error("%s", e)
        return 2
    up = Uploader(cfg, client)
    if "--check" in argv:
        return check(up)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    log.info("uploader %s start: %s, buffer %s, %s", VERSION, cfg["INGEST_URL"], cfg["DB_PATH"],
             describe_token(cfg["INGEST_TOKEN"]))
    while not _stop:
        if not up.buffer_ready():
            time.sleep(60)
            continue
        try:
            done = up.tick()
        except Exception:                                  # never die on one bad round
            log.exception("round failed")
            up.db = None
            done = False
            time.sleep(10)
        if "--once" in argv and done:
            break
        time.sleep(up.cfg["WATCH_S"])
    log.info("uploader stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
