#!/usr/bin/env python3
"""config_apply.py - the sensor's capture-configuration applier (root, no
network beyond Kismet's loopback REST; server/docs/config-delivery.md 5).

    config_apply.py apply          the desired.json the uploader wrote (wifi-sensor-config-apply.path)
    config_apply.py recover        at boot, before Kismet: undo an apply a reboot interrupted
    config_apply.py capabilities   Kismet's channel list for the source + the regulatory country (hourly)

apply: validate desired.json again (it is untrusted input: the unprivileged
uploader wrote it) against the live adapter; keep the last good files; write
the new kismet_site.conf source options (channels, channel_hoprate) and
collector.conf POLL_INTERVAL atomically; apply the hop list live through
Kismet's set_channel.cmd (the file first, so the hop guard pushes towards
it); restart the collector only when the poll interval changed; verify that
collecting polls with the new hop list arrive; otherwise restore the last
good files, apply and verify them. The outcome goes to result.json
(applied / rejected / rolled_back / failed, with the reason and the checks),
which the uploader reports; applied.json names the running configuration
(the collector stores its version with every poll). A refused hash is never
applied again unless the server marks the delivery a retry.

Configuration - environment, else KEY=value lines in CONFIG_APPLY_CONF
(default /etc/wifi-sensor/config-apply.conf):
  DESIRED            Default: /var/lib/wifi-sensor-uploader/config/desired.json
  STATE_DIR          Default: /var/lib/wifi-sensor-config
  KISMET_SITE_CONF   Default: /etc/kismet/kismet_site.conf
  COLLECTOR_CONF     Default: /etc/wifi-sensor/collector.conf
  DB_PATH            Default: /var/lib/wifi-sensor/buffer.db (read-only)
  KISMET_URL         Default: http://127.0.0.1:2501
  KISMET_AUTH_FILE   the sensor user's kismet_httpd.conf (admin login)
  SOURCE_NAME        Default: capture
  VERIFY_S 180, ROLLBACK_VERIFY_S 120, CHECK_EVERY_S 10, VERIFY_POLLS 3
Standard library only. Tests: python3 -m unittest discover -s sensor/tests
"""

import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import capture_config as cc  # noqa: E402

DEFAULTS = {
    "CONFIG_APPLY_CONF": "/etc/wifi-sensor/config-apply.conf",
    "DESIRED": "/var/lib/wifi-sensor-uploader/config/desired.json",
    "STATE_DIR": "/var/lib/wifi-sensor-config",
    "KISMET_SITE_CONF": "/etc/kismet/kismet_site.conf",
    "COLLECTOR_CONF": "/etc/wifi-sensor/collector.conf",
    "DB_PATH": "/var/lib/wifi-sensor/buffer.db",
    "KISMET_URL": "http://127.0.0.1:2501",
    "KISMET_AUTH_FILE": "",
    "SOURCE_NAME": "capture",
    "VERIFY_S": "180", "ROLLBACK_VERIFY_S": "120", "CHECK_EVERY_S": "10", "VERIFY_POLLS": "3",
}
NUMERIC = ("VERIFY_S", "ROLLBACK_VERIFY_S", "CHECK_EVERY_S", "VERIFY_POLLS")
MAX_DESIRED_BYTES = 8192
COLLECTOR_UNIT = "wifi-sensor-collector.service"


def log(msg):
    print("config-apply: %s" % msg, flush=True)


# --- files -----------------------------------------------------------------------------------

def read_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def load_config(env=None):
    env = os.environ if env is None else env
    cfg = dict(DEFAULTS)
    cfg.update(read_kv(env.get("CONFIG_APPLY_CONF", cfg["CONFIG_APPLY_CONF"])))
    cfg.update({k: v for k, v in env.items() if k in DEFAULTS})
    for k in NUMERIC:
        cfg[k] = float(cfg[k])
    return cfg


def load_json(path, limit=None):
    try:
        if limit is not None and os.path.getsize(path) > limit:
            return None
        with open(path, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else None
    except (OSError, ValueError):
        return None


def write_atomic(path, text, mode=0o644):
    """temp file in the same directory, fsync, rename, fsync the directory.
    An existing file keeps its mode and owner (collector.conf is 0640
    root:<sensor user> when it holds KISMET_PASS)."""
    d = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(d, ".%s.tmp" % os.path.basename(path))
    try:
        st = os.stat(path)
        mode, owner = st.st_mode & 0o7777, (st.st_uid, st.st_gid)
    except FileNotFoundError:
        owner = None
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    if owner is not None and hasattr(os, "chown"):
        try:
            os.chown(tmp, *owner)
        except PermissionError:          # not root (tests): the owner is the caller's anyway
            pass
    os.replace(tmp, path)
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, obj):
    write_atomic(path, json.dumps(obj, sort_keys=True) + "\n")


# --- the two files ---------------------------------------------------------------------------------

_SOURCE = re.compile(r"^(\s*source\s*=\s*)([^:\s]+):(.*?)\s*$")


def _split_opts(opts):
    """Kismet source options split at commas outside double quotes."""
    out, cur, quoted = [], "", False
    for ch in opts:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _source_line_index(lines, source_name):
    first = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = _SOURCE.match(line)
        if not m:
            continue
        if first is None:
            first = i
        if any(o.strip() == "name=%s" % source_name for o in _split_opts(m.group(3))):
            return i
    return first


def hop_rate(dwell_s):
    """dwell 0.2 / 0.5 / 1.0 s -> Kismet's channels per second 5 / 2 / 1."""
    return max(1, int(round(1.0 / float(dwell_s))))


def site_conf_with(text, source_name, channels, dwell_s):
    """kismet_site.conf with the source line's channels= and channel_hoprate=
    replaced (added when missing); every other line and option kept as it was."""
    lines = text.split("\n")
    i = _source_line_index(lines, source_name)
    if i is None:
        raise ValueError("no source= line in kismet_site.conf")
    m = _SOURCE.match(lines[i])
    opts = [o for o in _split_opts(m.group(3))
            if not o.strip().startswith(("channels=", "channel_hoprate="))]
    opts += ['channels="%s"' % ",".join(channels), "channel_hoprate=%d/sec" % hop_rate(dwell_s)]
    lines[i] = "%s%s:%s" % (m.group(1), m.group(2), ",".join(opts))
    return "\n".join(lines)


def site_conf_config(text, source_name):
    """(channels, dwell_s) of the source line, or (None, None)."""
    lines = text.split("\n")
    i = _source_line_index(lines, source_name)
    if i is None:
        return None, None
    chans, dwell = None, 0.2                  # Kismet's channel_hop_speed=5/sec (kismet.conf) by default
    for o in _split_opts(_SOURCE.match(lines[i]).group(3)):
        o = o.strip()
        if o.startswith("channels="):
            chans = [c.strip() for c in o[len("channels="):].strip('"').split(",") if c.strip()]
        elif o.startswith("channel_hoprate="):
            m = re.match(r"(\d+)/sec", o[len("channel_hoprate="):])
            if m and int(m.group(1)) > 0:
                dwell = round(1.0 / int(m.group(1)), 3)
    return chans, dwell


def collector_conf_with(text, poll_s):
    lines, done = text.split("\n"), False
    for i, line in enumerate(lines):
        if re.match(r"^\s*POLL_INTERVAL\s*=", line):
            lines[i] = "POLL_INTERVAL=%d" % poll_s
            done = True
    if not done:
        lines.insert(len(lines) - 1 if lines and lines[-1] == "" else len(lines), "POLL_INTERVAL=%d" % poll_s)
    return "\n".join(lines)


def collector_poll_s(text):
    for line in text.split("\n"):
        m = re.match(r"^\s*POLL_INTERVAL\s*=\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return 30


# --- Kismet --------------------------------------------------------------------------------------------

class Kismet:
    """The two calls the applier needs, over Kismet's loopback REST (admin login)."""
    FIELDS = ["kismet.datasource.uuid", "kismet.datasource.name", "kismet.datasource.running",
              "kismet.datasource.hop_channels", "kismet.datasource.hop_rate", "kismet.datasource.hop_shuffle",
              "kismet.datasource.channels"]

    def __init__(self, url, auth_file, timeout=10):
        import base64
        self.url, self.timeout = url.rstrip("/"), timeout
        conf = read_kv(auth_file)
        if not conf.get("httpd_username") or not conf.get("httpd_password"):
            raise SystemExit("config-apply: no httpd_username/httpd_password in %s" % auth_file)
        token = "%s:%s" % (conf["httpd_username"], conf["httpd_password"])
        self._auth = "Basic " + base64.b64encode(token.encode()).decode()

    def _post(self, path, payload):
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"json": json.dumps(payload)}).encode()
        req = urllib.request.Request(self.url + path, data=data, headers={"Authorization": self._auth})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def sources(self):
        return self._post("/datasource/all_sources.json", {"fields": self.FIELDS})

    def set_hop(self, uuid, channels, rate, shuffle):
        return self._post("/datasource/by-uuid/%s/set_channel.cmd" % uuid,
                          {"channels": channels, "rate": rate, "shuffle": int(bool(shuffle))})


def capture_source(sources, name):
    srcs = sources if isinstance(sources, list) else []
    for s in srcs:
        if s.get("kismet.datasource.name") == name:
            return s
    return srcs[0] if srcs else None


def same_list(live, wanted):
    return sorted(c.upper() for c in live or []) == sorted(c.upper() for c in wanted or [])


# --- the buffer (read-only) ------------------------------------------------------------------------------

def last_poll_id(db_path):
    with sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10) as db:
        r = db.execute("SELECT max(id) FROM polls").fetchone()
    return r[0] or 0


def collecting_polls(db_path, after_id, n_channels, poll_s):
    """Polls after AFTER_ID that collect with the expected hop list: ok,
    datasource running, devices > 0, ds_hop_n = N, ds_hop_ok = 1, and
    (from the second) spaced poll_s ± 2 s from the previous one."""
    with sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10) as db:
        rows = db.execute("SELECT id, ts, ok, ds_running, devices_active, ds_hop_n, ds_hop_ok, ds_packets "
                          "FROM polls WHERE id > ? ORDER BY id", (after_id,)).fetchall()
    good, prev = [], None
    for r in rows:
        pid, ts, ok, running, active, hop_n, hop_ok, packets = r
        fine = ok == 1 and running == 1 and (active or 0) > 0 and hop_n == n_channels and hop_ok == 1
        if fine and prev is not None and abs((ts - prev) - poll_s) > 2:
            fine = False
        if fine:
            good.append({"id": pid, "ts": ts, "packets": packets})
        else:
            good = []                     # consecutive good polls only
        prev = ts
    return good


# --- the applier ---------------------------------------------------------------------------------------------

class Applier:
    def __init__(self, cfg, kismet=None, systemctl=None, sleep=time.sleep, now=time.time):
        self.cfg = cfg
        self.kismet = kismet
        self.systemctl = systemctl or (lambda *a: subprocess.run(["systemctl"] + list(a), check=True, timeout=120))
        self.sleep, self.now = sleep, now
        self.dir = cfg["STATE_DIR"]
        self.last_good = os.path.join(self.dir, "last-good")
        self.refused = os.path.join(self.dir, "refused")

    def path(self, name):
        return os.path.join(self.dir, name)

    def result(self, desired, result, reason=None, **kw):
        out = {"version": desired.get("version"), "sha256": desired.get("sha256"), "result": result,
               "reason": reason, "started_poll": kw.get("started_poll"), "effective_poll": kw.get("effective_poll"),
               "restored_poll": kw.get("restored_poll"), "checks": kw.get("checks") or {}, "at": self.now()}
        write_json(self.path("result.json"), out)
        log("v%s: %s%s" % (out["version"], result, " - %s" % reason if reason else ""))
        return out

    def _refuse(self, sha):
        os.makedirs(self.refused, exist_ok=True)
        write_atomic(os.path.join(self.refused, sha), "%f\n" % self.now())

    # --- the running configuration
    def files(self):
        with open(self.cfg["KISMET_SITE_CONF"]) as f:
            site = f.read()
        with open(self.cfg["COLLECTOR_CONF"]) as f:
            coll = f.read()
        return site, coll

    def write_files(self, site, coll):
        write_atomic(self.cfg["KISMET_SITE_CONF"], site)
        write_atomic(self.cfg["COLLECTOR_CONF"], coll)

    def keep_last_good(self, site, coll):
        os.makedirs(self.last_good, exist_ok=True)
        write_atomic(os.path.join(self.last_good, "kismet_site.conf"), site)
        write_atomic(os.path.join(self.last_good, "collector.conf"), coll)

    def read_last_good(self):
        with open(os.path.join(self.last_good, "kismet_site.conf")) as f:
            site = f.read()
        with open(os.path.join(self.last_good, "collector.conf")) as f:
            coll = f.read()
        return site, coll

    def go_live(self, channels, dwell_s, poll_changed):
        src = capture_source(self.kismet.sources(), self.cfg["SOURCE_NAME"])
        if src is None:
            raise RuntimeError("no Kismet datasource")
        self.kismet.set_hop(src["kismet.datasource.uuid"], ",".join(channels), hop_rate(dwell_s),
                            src.get("kismet.datasource.hop_shuffle", 1))
        if poll_changed:
            self.systemctl("restart", COLLECTOR_UNIT)

    def verify(self, channels, dwell_s, poll_s, after_id, seconds):
        """(ok, checks) - waits up to SECONDS for VERIFY_POLLS consecutive
        collecting polls with the hop list, and Kismet's live list and rate."""
        need = int(self.cfg["VERIFY_POLLS"])
        deadline = self.now() + seconds
        checks = {}
        while True:
            try:
                src = capture_source(self.kismet.sources(), self.cfg["SOURCE_NAME"]) or {}
            except Exception as e:                       # Kismet restarting: keep waiting
                src = {}
                checks["kismet_error"] = str(e)[:200]
            live = src.get("kismet.datasource.hop_channels") or []
            good = collecting_polls(self.cfg["DB_PATH"], after_id, len(channels), poll_s)
            packets = [g["packets"] for g in good if g["packets"] is not None]
            checks = {"live_list_ok": same_list(live, channels), "live_n": len(live),
                      "live_rate": src.get("kismet.datasource.hop_rate"), "rate_ok":
                      float(src.get("kismet.datasource.hop_rate") or 0) == float(hop_rate(dwell_s)),
                      "running": bool(src.get("kismet.datasource.running")), "collecting_polls": len(good),
                      "packets_rising": len(packets) >= 2 and packets[-1] > packets[0]}
            if (checks["live_list_ok"] and checks["rate_ok"] and checks["running"] and len(good) >= need
                    and checks["packets_rising"]):
                checks["first_poll"] = good[0]["id"]
                return True, checks
            if self.now() >= deadline:
                return False, checks
            self.sleep(self.cfg["CHECK_EVERY_S"])

    def capabilities(self, sources=None):
        src = capture_source(sources if sources is not None else self.kismet.sources(), self.cfg["SOURCE_NAME"])
        chans = (src or {}).get("kismet.datasource.channels") or None
        return {"channels_supported": chans, "reg_country": reg_country(), "at": self.now()}

    # --- the commands
    def apply(self):
        desired = load_json(self.cfg["DESIRED"], MAX_DESIRED_BYTES)
        if desired is None:
            log("no readable desired.json - nothing to do")
            return None
        meta = {"version": desired.get("version"), "sha256": desired.get("sha256")}
        cfg = desired.get("desired")
        if not (isinstance(meta["version"], int) and isinstance(meta["sha256"], str)
                and re.match(r"^[0-9a-f]{64}$", meta["sha256"]) and isinstance(cfg, dict)):
            return self.result(meta, "rejected", "desired.json is malformed")
        try:
            if cc.sha256(cfg) != meta["sha256"]:
                return self.result(meta, "rejected", "the configuration does not match its sha256")
        except (KeyError, TypeError, AttributeError, ValueError):
            return self.result(meta, "rejected", "desired.json is malformed")
        applied = load_json(self.path("applied.json")) or {}
        if applied.get("sha256") == meta["sha256"]:
            log("v%s is the running configuration - nothing to do" % meta["version"])
            return None
        marker = os.path.join(self.refused, meta["sha256"])
        if os.path.exists(marker):
            if not desired.get("retry"):
                log("v%s was refused before (no retry requested) - not applied" % meta["version"])
                return None
            os.remove(marker)
        sources = self.kismet.sources()
        caps = self.capabilities(sources)
        write_json(self.path("capabilities.json"), caps)
        ok, reason = cc.validate(cfg, caps)
        if not ok:
            self._refuse(meta["sha256"])
            return self.result(meta, "rejected", reason)
        src = capture_source(sources, self.cfg["SOURCE_NAME"])
        if src is None or not src.get("kismet.datasource.running"):
            return self.result(meta, "rejected", "the capture source is not running - nothing to verify against")
        new = cc.normalise(cfg)
        site, coll = self.files()
        old_chans, old_dwell = site_conf_config(site, self.cfg["SOURCE_NAME"])
        old_poll = collector_poll_s(coll)
        if not os.path.exists(os.path.join(self.last_good, "kismet_site.conf")):
            self.keep_last_good(site, coll)          # the installer's configuration, running and verified so far
        started = last_poll_id(self.cfg["DB_PATH"])
        write_json(self.path("attempt.json"), dict(meta, phase="apply", started_poll=started, at=self.now()))
        self.result(meta, "applying", started_poll=started)
        try:
            self.write_files(site_conf_with(site, self.cfg["SOURCE_NAME"], new["channels"], new["dwell_s"]),
                             collector_conf_with(coll, new["poll_s"]))
            self.go_live(new["channels"], new["dwell_s"], new["poll_s"] != old_poll)
            ok, checks = self.verify(new["channels"], new["dwell_s"], new["poll_s"], started, self.cfg["VERIFY_S"])
            reason = None if ok else "capture did not verify: %s" % _failed(checks)
        except Exception as e:                           # anything: roll back
            ok, checks, reason = False, {"error": str(e)[:200]}, "apply failed: %s" % str(e)[:200]
        if ok:
            write_json(self.path("applied.json"), dict(meta, effective_poll=checks["first_poll"], at=self.now()))
            self.keep_last_good(*self.files())
            os.remove(self.path("attempt.json"))
            return self.result(meta, "applied", started_poll=started, effective_poll=checks["first_poll"],
                               checks=checks)
        return self.rollback(meta, started, reason, checks, old_poll != new["poll_s"])

    def rollback(self, meta, started, reason, checks, poll_changed):
        self._refuse(meta["sha256"])
        try:
            site, coll = self.read_last_good()
            self.write_files(site, coll)
            chans, dwell = site_conf_config(site, self.cfg["SOURCE_NAME"])
            poll = collector_poll_s(coll)
            self.go_live(chans, dwell, poll_changed)
            after = last_poll_id(self.cfg["DB_PATH"])
            ok, rchecks = self.verify(chans, dwell, poll, after, self.cfg["ROLLBACK_VERIFY_S"])
        except Exception as e:
            ok, rchecks = False, {"error": str(e)[:200]}
        if os.path.exists(self.path("attempt.json")):
            os.remove(self.path("attempt.json"))
        if ok:
            return self.result(meta, "rolled_back", reason, started_poll=started,
                               restored_poll=rchecks["first_poll"], checks={"apply": checks, "rollback": rchecks})
        return self.result(meta, "failed", "%s; the rollback did not verify: %s" % (reason, _failed(rchecks)),
                           started_poll=started, checks={"apply": checks, "rollback": rchecks})

    def recover(self):
        """At boot, before Kismet: an attempt.json means an apply was cut short
        (reboot, power cut): restore the last good files; Kismet and the
        collector then start on them."""
        attempt = load_json(self.path("attempt.json"))
        if attempt is None:
            return None
        try:
            self.write_files(*self.read_last_good())
        except OSError as e:
            log("recover: cannot restore the last good files: %s" % e)
            return self.result(attempt, "failed", "interrupted apply; the last good files could not be restored")
        self._refuse(attempt["sha256"]) if isinstance(attempt.get("sha256"), str) else None
        os.remove(self.path("attempt.json"))
        return self.result(attempt, "rolled_back", "interrupted by a reboot or crash during the apply",
                           started_poll=attempt.get("started_poll"))


def _failed(checks):
    keys = [k for k in ("live_list_ok", "rate_ok", "running", "packets_rising") if checks.get(k) is False]
    return ", ".join(keys + (["%d collecting polls" % checks["collecting_polls"]]
                             if "collecting_polls" in checks else [])) or "no checks"


def reg_country():
    for iw in ("/usr/sbin/iw", "/sbin/iw"):
        if os.path.exists(iw):
            try:
                out = subprocess.run([iw, "reg", "get"], capture_output=True, text=True, timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                return None
            m = re.search(r"country ([A-Z0-9]{2}):", out)
            return m.group(1) if m else None
    return None


def main(argv):
    cmd = argv[0] if argv else "apply"
    cfg = load_config()
    os.makedirs(cfg["STATE_DIR"], exist_ok=True)
    os.chmod(cfg["STATE_DIR"], 0o755)
    with open(os.path.join(cfg["STATE_DIR"], ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cmd == "recover":
            Applier(cfg).recover()
            return 0
        kismet = Kismet(cfg["KISMET_URL"], cfg["KISMET_AUTH_FILE"])
        a = Applier(cfg, kismet)
        if cmd == "capabilities":
            write_json(a.path("capabilities.json"), a.capabilities())
            return 0
        if cmd == "apply":
            a.apply()
            return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
