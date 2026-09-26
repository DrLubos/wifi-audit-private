#!/usr/bin/env python3
"""Measure the real channel-hop schedule of the monitor interface.

Polls `iw dev IFACE info` as fast as it can (~4 ms per call on a Pi 3B+,
read-only, no root) for --duration seconds and reports the dwell per hop
(time between channel changes), the entries with the longest mean dwell (a
stall shows up here, e.g. 721 ms on 165HT40- before 2026-09-26), the number of
distinct channel settings visited (compare with Kismet's hop list: a shuffle
stride that shares a factor with the list length visits only part of it) and
the 2.4 GHz time share.

Usage:  python3 dwell_poll.py IFACE [--duration S] [--list]
        (IFACE is the monitor VIF, e.g. wlan1mon)
"""
import argparse
import collections
import re
import shutil
import statistics
import subprocess
import time

CHAN_RE = re.compile(r"channel (\d+) \((\d+) MHz\), width: ([^,\n]+)(?:, center1: (\d+))?")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("iface")
    ap.add_argument("--duration", type=float, default=90, help="seconds (default 90)")
    ap.add_argument("--list", action="store_true", help="also print the distinct settings seen")
    a = ap.parse_args()
    iw = shutil.which("iw") or "/usr/sbin/iw"
    samples = []
    end = time.time() + a.duration
    while time.time() < end:
        t = time.time()
        out = subprocess.run([iw, "dev", a.iface, "info"], capture_output=True, text=True).stdout
        m = CHAN_RE.search(out)
        if m:
            width = m.group(3).replace(" MHz", "").replace(" (no HT)", "")
            samples.append((t, "%s/%s/%s" % (m.group(1), width, m.group(4))))
    if len(samples) < 10:
        raise SystemExit("no channel information for %s (is it the monitor interface?)" % a.iface)
    segs = []
    for t, c in samples:
        if segs and segs[-1][0] == c:
            segs[-1][2] = t
        else:
            segs.append([c, t, t])
    # the first and last segments are cut by the measurement window
    dur = [(s[0], segs[i + 1][1] - s[1]) for i, s in enumerate(segs[:-1]) if i > 0]
    if len(dur) < 10:
        raise SystemExit("too few hops (%d) - is the source hopping?" % len(dur))
    d = [x for _, x in dur]
    dec = statistics.quantiles(d, n=10)
    per = collections.defaultdict(list)
    for c, x in dur:
        per[c].append(x)
    longest = sorted(per.items(), key=lambda kv: -statistics.mean(kv[1]))[:5]
    total = sum(d)
    print("samples %d, median poll interval %.4f s, hops %d, distinct channel settings %d"
          % (len(samples), statistics.median(b[0] - a_[0] for a_, b in zip(samples, samples[1:])),
             len(dur), len(per)))
    print("dwell s: median %.3f, p10 %.3f, p90 %.3f, max %.3f" % (statistics.median(d), dec[0], dec[-1], max(d)))
    print("longest mean dwell (setting, hops, s): %s"
          % [(c, len(v), round(statistics.mean(v), 3)) for c, v in longest])
    print("time share 2.4 GHz: %.3f" % (sum(x for c, x in dur if int(c.split("/")[0]) <= 14) / total))
    if a.list:
        print("settings (channel/width/center1): %s" % sorted(per, key=lambda c: (int(c.split("/")[0]), c)))


if __name__ == "__main__":
    main()
