#!/usr/bin/env python3
"""hop_guard.py - keep Kismet hopping the configured channel list.

Run as a long-lived service (wifi-sensor-hop-guard.service, as the sensor user,
installed by install_sensor.sh). Kismet 2025-09 re-opens a source after a
capture-helper crash from a definition it rebuilds without quoting
(kis_datasource.cc, generate_source_definition()), so an explicit
channels="a,b,..." list comes back as channels=a and the source hops a single
channel until Kismet restarts. The packet counter keeps moving, so the capture
watchdog cannot see it (docs/findings.md section 7: 12 h on channel 1).

Every HOP_GUARD_INTERVAL seconds the guard reads the live hop list from the REST
API and compares it with the channels= list of the source= line in
kismet_site.conf as an unordered multiset (same entries, same count, case-
insensitive - Kismet shuffles, so the order carries no meaning and must not
trigger a re-apply). On a mismatch it re-applies the configured list with
POST /datasource/by-uuid/<uuid>/set_channel.cmd (channels, rate, shuffle; admin
login from KISMET_AUTH_FILE). It never restarts Kismet - dead capture (no frames)
stays the capture watchdog's job. A source without an explicit list (Kismet
autodetect) survives a re-open, so there is nothing to guard.

It logs only its own actions and errors, never Kismet's log lines or device data.

Environment:
  KISMET_URL          REST base URL                (default http://127.0.0.1:2501)
  KISMET_AUTH_FILE    kismet_httpd.conf with httpd_username / httpd_password
                      (default ~/.kismet/kismet_httpd.conf)
  KISMET_SITE_CONF    config with the source= line (default /etc/kismet/kismet_site.conf)
  KISMET_SOURCE_NAME  source name= to guard when there are several (default: first)
  HOP_GUARD_INTERVAL  seconds between checks       (default 10)
Usage: hop_guard.py [--once]
"""

import base64
import collections
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BACKOFF_AFTER = 3        # consecutive failed / non-sticking re-applies
BACKOFF_INTERVAL = 60    # seconds between checks while backing off
FIELDS = ["kismet.datasource.uuid", "kismet.datasource.name", "kismet.datasource.running",
          "kismet.datasource.hopping", "kismet.datasource.hop_channels",
          "kismet.datasource.hop_rate", "kismet.datasource.hop_shuffle",
          "kismet.datasource.hop_shuffle_skip"]

# --- configured list (same parser as collector/hop_coverage.py; the collector and
# the sensor helpers install separately, so each carries its own copy) ----------
_SOURCE_RE = re.compile(r"^\s*source\s*=\s*(\S.*?)\s*$")
_CHANNELS_RE = re.compile(r"(?:^|[:,])channels=(\"[^\"]*\"|[^,]*)")
_NAME_RE = re.compile(r"(?:^|[:,])name=([^,]*)")


def configured_channels(text, source_name=None):
    """Explicit channels= list of the source= line, or None (autodetect / none)."""
    lines = [m.group(1) for m in (_SOURCE_RE.match(line) for line in text.splitlines()
                                  if not line.lstrip().startswith("#")) if m]
    if not lines:
        return None
    chosen = lines[0]
    if source_name:
        for d in lines:
            n = _NAME_RE.search(d)
            if n and n.group(1).strip('"') == source_name:
                chosen = d
                break
    m = _CHANNELS_RE.search(chosen)
    if not m:
        return None
    entries = [c.strip() for c in m.group(1).strip('"').split(",") if c.strip()]
    return entries or None


def same_hop_list(live, configured):
    """Same entries the same number of times, ignoring order and case."""
    def ms(entries):
        return collections.Counter(str(e).strip().lower() for e in entries)
    return ms(live) == ms(configured)


def visited(n, stride, shuffle):
    """Entries Kismet's helper really tunes: N / gcd(N, stride) when shuffling."""
    return n // math.gcd(n, stride) if n and shuffle and stride > 0 else n


# --- Kismet REST -------------------------------------------------------------------
class Kismet:
    def __init__(self, url, auth_file, timeout=10):
        self.url = url.rstrip("/")
        self.timeout = timeout
        conf = {}
        with open(os.path.expanduser(auth_file)) as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    conf[k.strip()] = v
        if not conf.get("httpd_username") or not conf.get("httpd_password"):
            raise SystemExit("hop-guard: no httpd_username/httpd_password in %s" % auth_file)
        token = "%s:%s" % (conf["httpd_username"], conf["httpd_password"])
        self._auth = "Basic " + base64.b64encode(token.encode()).decode()

    def _post(self, path, payload):
        data = urllib.parse.urlencode({"json": json.dumps(payload)}).encode()
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Authorization": self._auth})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def sources(self):
        return self._post("/datasource/all_sources.json", {"fields": FIELDS})

    def set_hop(self, uuid, channels, rate, shuffle):
        return self._post("/datasource/by-uuid/%s/set_channel.cmd" % uuid,
                          {"channels": channels, "rate": rate, "shuffle": int(bool(shuffle))})


# --- guard loop --------------------------------------------------------------------
class Guard:
    def __init__(self, kismet, site_conf, source_name=None, log=print):
        self.kismet = kismet
        self.site_conf = site_conf
        self.source_name = source_name
        self.log = log
        self._stamp = None
        self._configured = None
        self._said_auto = False
        self.failures = 0

    def configured(self):
        try:
            stamp = os.stat(self.site_conf).st_mtime_ns
        except OSError as e:
            if self._stamp != "missing":
                self.log("hop-guard: cannot read %s (%s)" % (self.site_conf, e.strerror))
            self._stamp, self._configured = "missing", None
            return None
        if stamp != self._stamp:
            with open(self.site_conf) as f:
                self._configured = configured_channels(f.read(), self.source_name)
            self._stamp = stamp
            self._said_auto = False
            if self._configured:
                self.log("hop-guard: guarding a %d-entry hop list from %s"
                         % (len(self._configured), self.site_conf))
        if self._configured is None and not self._said_auto:
            self.log("hop-guard: no explicit channels= list in %s - nothing to guard"
                     % self.site_conf)
            self._said_auto = True
        return self._configured

    def check(self):
        """One check. Returns 'ok', 'reapplied', 'idle' or 'error'."""
        conf = self.configured()
        if not conf:
            return "idle"
        try:
            srcs = self.kismet.sources()
        except (OSError, ValueError) as e:  # Kismet restarting: try again next tick
            self.log("hop-guard: cannot read datasources (%s)" % e)
            return "error"
        if self.source_name:
            srcs = [s for s in srcs if s.get("kismet.datasource.name") == self.source_name]
        if not srcs:
            return "idle"
        s = srcs[0]
        if not s.get("kismet.datasource.running"):
            return "idle"     # not open (re-open pending); the next tick sees the result
        live = s.get("kismet.datasource.hop_channels") or []
        if same_hop_list(live, conf):
            self.failures = 0
            n = len(live)
            v = visited(n, int(s.get("kismet.datasource.hop_shuffle_skip") or 0),
                        s.get("kismet.datasource.hop_shuffle"))
            if v < n:
                self.log("hop-guard: WARNING: stride %s visits only %d of %d entries - REST cannot "
                         "set the stride; use a prime number of entries"
                         % (s.get("kismet.datasource.hop_shuffle_skip"), v, n))
            return "ok"
        rate = float(s.get("kismet.datasource.hop_rate") or 0) or 5.0
        shuffle = s.get("kismet.datasource.hop_shuffle", 1)
        try:
            self.kismet.set_hop(s["kismet.datasource.uuid"], conf, rate, shuffle)
        except (OSError, ValueError) as e:
            self.failures += 1
            self.log("hop-guard: re-applying the hop list failed (%s), attempt %d" % (e, self.failures))
            return "error"
        self.failures += 1     # reset only once a later check sees the list in place
        self.log("hop-guard: re-applied hop list: live %d entries -> %d (rate %g/s)"
                 % (len(live), len(conf), rate))
        return "reapplied"

    def interval(self, base):
        return BACKOFF_INTERVAL if self.failures >= BACKOFF_AFTER else base


def main(argv):
    url = os.environ.get("KISMET_URL", "http://127.0.0.1:2501")
    auth = os.environ.get("KISMET_AUTH_FILE", "~/.kismet/kismet_httpd.conf")
    site = os.environ.get("KISMET_SITE_CONF", "/etc/kismet/kismet_site.conf")
    base = float(os.environ.get("HOP_GUARD_INTERVAL", "10"))
    guard = Guard(Kismet(url, auth), site, os.environ.get("KISMET_SOURCE_NAME") or None,
                  log=lambda m: print(m, flush=True))
    if "--once" in argv:
        print("hop-guard: %s" % guard.check(), flush=True)
        return 0
    while True:
        guard.check()
        time.sleep(guard.interval(base))


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
