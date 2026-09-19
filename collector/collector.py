#!/usr/bin/env python3
"""wifi-sensor collector: read Kismet -> shape -> store (Milestone 2, part 1).

Polls the local Kismet REST API every POLL_INTERVAL seconds, reduces each
active device to a compact record and appends it to a local SQLite buffer.
No detection, no upload (the upload step comes later and reads the buffer).

Configuration - environment variables, optionally loaded from a KEY=value file
(COLLECTOR_CONF, default /etc/wifi-sensor/collector.conf; the systemd unit
passes the same file as EnvironmentFile). Environment wins over the file.

  KISMET_URL         Kismet REST base URL.      Default: http://127.0.0.1:2501
  KISMET_AUTH_FILE   Kismet httpd auth file with httpd_username= / httpd_password=
                     lines (the file Kismet itself uses, mode 0600).
                     Default: ~/.kismet/kismet_httpd.conf
  KISMET_USER        Override the credentials from KISMET_AUTH_FILE (dev use).
  KISMET_PASS
  DB_PATH            SQLite buffer.              Default: /var/lib/wifi-sensor/buffer.db
  POLL_INTERVAL      Seconds between polls.      Default: 30
  POLL_OVERLAP       Seconds re-queried on every poll so nothing is missed
                     between two polls.          Default: 5
  RETENTION_DAYS     Delete observations older than this.  Default: 14
  HTTP_TIMEOUT       Seconds per Kismet request. Default: 15
  LOG_LEVEL          DEBUG | INFO | WARNING.     Default: INFO

Run by hand:  python3 collector.py [--once]
"""

import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kismet_client import KismetClient, KismetError  # noqa: E402
from shape import DEVICE_FIELDS, shape_alert, shape_device  # noqa: E402
from store import Store  # noqa: E402

log = logging.getLogger("collector")

DEFAULTS = {
    "COLLECTOR_CONF": "/etc/wifi-sensor/collector.conf",
    "KISMET_URL": "http://127.0.0.1:2501",
    "KISMET_AUTH_FILE": "~/.kismet/kismet_httpd.conf",
    "KISMET_USER": "",
    "KISMET_PASS": "",
    "DB_PATH": "/var/lib/wifi-sensor/buffer.db",
    "POLL_INTERVAL": "30",
    "POLL_OVERLAP": "5",
    "RETENTION_DAYS": "14",
    "HTTP_TIMEOUT": "15",
    "LOG_LEVEL": "INFO",
}

PRUNE_EVERY = 3600  # seconds


# --- configuration --------------------------------------------------------------

def read_kv_file(path):
    """Parse KEY=value lines (comments and blank lines ignored, optional quotes)."""
    out = {}
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
    return out


def load_config(environ=None):
    env = dict(os.environ if environ is None else environ)
    cfg = dict(DEFAULTS)
    conf_path = env.get("COLLECTOR_CONF", cfg["COLLECTOR_CONF"])
    if os.path.isfile(conf_path):
        cfg.update(read_kv_file(conf_path))
    cfg.update({k: v for k, v in env.items() if k in DEFAULTS})

    if not (cfg["KISMET_USER"] and cfg["KISMET_PASS"]):
        auth_path = os.path.expanduser(cfg["KISMET_AUTH_FILE"])
        try:
            auth = read_kv_file(auth_path)
        except OSError as e:
            raise SystemExit("cannot read Kismet credentials from %s (%s); "
                             "set KISMET_USER/KISMET_PASS or KISMET_AUTH_FILE" % (auth_path, e))
        cfg["KISMET_USER"] = cfg["KISMET_USER"] or auth.get("httpd_username", "")
        cfg["KISMET_PASS"] = cfg["KISMET_PASS"] or auth.get("httpd_password", "")
        if not (cfg["KISMET_USER"] and cfg["KISMET_PASS"]):
            raise SystemExit("no httpd_username/httpd_password in %s" % auth_path)

    for k in ("POLL_INTERVAL", "POLL_OVERLAP", "RETENTION_DAYS"):
        cfg[k] = int(cfg[k])
    cfg["HTTP_TIMEOUT"] = float(cfg["HTTP_TIMEOUT"])
    if cfg["POLL_INTERVAL"] < 5:
        raise SystemExit("POLL_INTERVAL must be at least 5 seconds")
    cfg["DB_PATH"] = os.path.expanduser(cfg["DB_PATH"])
    return cfg


# --- one poll -----------------------------------------------------------------------

def datasource_health(sources):
    running = 1
    error = None
    packets = 0
    for s in sources if isinstance(sources, list) else []:
        if not s.get("kismet.datasource.running"):
            running = 0
        if s.get("kismet.datasource.error") and error is None:
            error = "%s: %s" % (s.get("kismet.datasource.name"),
                                s.get("kismet.datasource.error_reason") or "error")
        packets += int(s.get("kismet.datasource.num_packets") or 0)
    if not sources:
        running = 0
        error = error or "no datasources"
    return running, error, packets


def poll_once(client, store, cfg, since):
    """Run one poll. Returns the Kismet timestamp to resume from next time."""
    ts = int(time.time())
    t0 = time.monotonic()
    try:
        status = client.status()
        sources = client.datasources()
        kismet_ts = int(status.get("kismet.system.timestamp.sec") or ts)
        query_from = max(0, since - cfg["POLL_OVERLAP"])
        raw_devices = client.devices_since(query_from, DEVICE_FIELDS)
        raw_alerts = client.alerts_since(query_from)
    except KismetError as e:
        ms = int((time.monotonic() - t0) * 1000)
        store.write_failed_poll(ts, str(e), ms)
        log.warning("poll failed after %d ms: %s", ms, e)
        return since

    records = []
    skipped = 0  # devices shape_device() rejected; a steady non-zero count means a shape.py bug
    for d in raw_devices if isinstance(raw_devices, list) else []:
        try:
            records.append(shape_device(d, ts))
        except (ValueError, TypeError) as e:
            skipped += 1
            log.info("skipping device: %s", e)
    alerts = [shape_alert(a) for a in raw_alerts if isinstance(a, dict)] \
        if isinstance(raw_alerts, list) else []

    ds_running, ds_error, ds_packets = datasource_health(sources)
    health = {
        "kismet_ts": kismet_ts,
        "devices_total": status.get("kismet.system.devices.count"),
        "ds_running": ds_running,
        "ds_error": ds_error,
        "ds_packets": ds_packets,
    }
    ms = int((time.monotonic() - t0) * 1000)
    new_obs, new_alerts = store.write_poll(ts, health, records, alerts, ms)
    log.info("poll ok: total=%s active=%d skipped=%d new_obs=%d alerts=%d/%d ds=%s %dms",
             health["devices_total"], len(records), skipped, new_obs, new_alerts, len(alerts),
             "up" if ds_running else ("DOWN " + (ds_error or "")), ms)
    if abs(kismet_ts - ts) > 5:
        log.warning("clock skew: kismet=%d collector=%d", kismet_ts, ts)
    return kismet_ts


# --- main loop --------------------------------------------------------------------------

class Stop(Exception):
    pass


def _on_signal(signum, frame):
    raise Stop()


def main(argv):
    once = "--once" in argv
    cfg = load_config()
    logging.basicConfig(level=getattr(logging, cfg["LOG_LEVEL"].upper(), logging.INFO),
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    store = Store(cfg["DB_PATH"])
    client = KismetClient(cfg["KISMET_URL"], cfg["KISMET_USER"], cfg["KISMET_PASS"],
                          timeout=cfg["HTTP_TIMEOUT"])
    # Resume from the last successful poll; a fresh buffer takes a full snapshot.
    since = int(store.get_meta("last_kismet_ts") or 0)
    log.info("collector start: kismet=%s db=%s interval=%ds since=%d",
             cfg["KISMET_URL"], cfg["DB_PATH"], cfg["POLL_INTERVAL"], since)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    last_prune = 0.0
    try:
        while True:
            started = time.monotonic()
            since = poll_once(client, store, cfg, since)
            if once:
                log.info("counts: %s", store.counts())
                break
            if started - last_prune >= PRUNE_EVERY:
                cutoff = int(time.time()) - cfg["RETENTION_DAYS"] * 86400
                removed = store.prune(cutoff)
                if any(removed.values()):
                    log.info("pruned rows older than %d days: %s", cfg["RETENTION_DAYS"], removed)
                last_prune = started
            delay = cfg["POLL_INTERVAL"] - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
    except Stop:
        log.info("collector stopping")
    finally:
        client.close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
