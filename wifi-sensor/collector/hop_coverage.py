"""Hop-list coverage of the capture source, recorded per poll (polls.ds_hop_*).

Two Kismet 2025-09 defects reduce the channel coverage without touching the
packet counter, so they are invisible in ds_running / ds_packets:

  - After a capture-helper crash Kismet re-opens the source from a definition it
    rebuilds without quoting, so an explicit channels="a,b,..." list comes back
    as channels=a and the source hops a single channel until Kismet restarts
    (or sensor/hop_guard.py re-applies the list).
  - The Linux Wi-Fi helper hops with a stride derived from the list length only
    and visits just N / gcd(N, stride) entries.

Each poll therefore records the live list length, how many entries are really
visited, and whether the live list is the configured one. The comparison is an
unordered multiset (same entries, same count, case-insensitive): Kismet
shuffles, order carries no meaning. See docs/findings.md sections 7 and 9.

sensor/hop_guard.py carries its own copy of the parser and the comparison on
purpose: the collector and the sensor helpers are installed separately.
"""

import collections
import math
import os
import re

_SOURCE_RE = re.compile(r"^\s*source\s*=\s*(\S.*?)\s*$")
_CHANNELS_RE = re.compile(r"(?:^|[:,])channels=(\"[^\"]*\"|[^,]*)")
_NAME_RE = re.compile(r"(?:^|[:,])name=([^,]*)")


def configured_channels(text, source_name=None):
    """Explicit hop list of a source= line in kismet_site.conf text.

    Returns the list of entries, or None when the source has no channels=
    option (Kismet autodetects - such a definition survives a re-open) or no
    source= line exists. With several source= lines the one whose name= equals
    SOURCE_NAME wins, otherwise the first.
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _SOURCE_RE.match(line)
        if m:
            lines.append(m.group(1))
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
    """True when both lists hold the same entries the same number of times,
    ignoring order and case."""
    def ms(entries):
        return collections.Counter(str(e).strip().lower() for e in entries)
    return ms(live) == ms(configured)


def hop_health(sources, configured):
    """(hop_n, hop_visited, hop_ok) of the capture source (the first datasource;
    the sensor has one).

    hop_n       length of the live hop list
    hop_visited entries actually tuned: N / gcd(N, shuffle stride) when
                shuffling, else N
    hop_ok      1 when the live list equals CONFIGURED (unordered multiset) and
                every entry is visited, 0 when degraded, None when no explicit
                list is configured (nothing to compare against)
    Returns (None, None, None) when there is no datasource.
    """
    srcs = sources if isinstance(sources, list) else []
    if not srcs:
        return None, None, None
    s = srcs[0]
    live = s.get("kismet.datasource.hop_channels") or []
    n = len(live)
    stride = int(s.get("kismet.datasource.hop_shuffle_skip") or 0)
    if n and s.get("kismet.datasource.hop_shuffle") and stride > 0:
        visited = n // math.gcd(n, stride)
    else:
        visited = n
    if configured is None:
        return n, visited, None
    ok = bool(s.get("kismet.datasource.running")) and visited == n \
        and same_hop_list(live, configured)
    return n, visited, int(ok)


class SiteConf:
    """kismet_site.conf, re-parsed only when its mtime changes."""

    def __init__(self, path, source_name=None):
        self.path = path
        self.source_name = source_name
        self._stamp = None
        self._channels = None

    def channels(self):
        try:
            stamp = os.stat(self.path).st_mtime_ns
        except OSError:
            self._stamp, self._channels = None, None
            return None
        if stamp != self._stamp:
            try:
                with open(self.path) as f:
                    self._channels = configured_channels(f.read(), self.source_name)
            except OSError:
                self._channels = None
            self._stamp = stamp
        return self._channels
