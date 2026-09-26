#!/usr/bin/env python3
"""Which frequencies does the capture stream actually deliver, and how often?

Reads Kismet's pcapng stream for --duration seconds and keeps only
(timestamp, radiotap frequency) per frame in memory. Prints frames and "visits"
per frequency and the largest gaps without any frame. Note: while tuned to an
HT40/VHT80 channel the adapter reports frames on several 20 MHz frequencies,
so a frequency change in the stream is NOT a hop - use dwell_poll.py for the
hop schedule. Nothing is written.

Usage:  python3 hoplog.py [--duration S] [--source NAME]
Env:    KISMET_URL, KISMET_AUTH_FILE (defaults as for the collector).
"""
import argparse
import collections
import json
import os
import socket
import statistics
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_common as pc  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--duration", type=float, default=180, help="seconds (default 180)")
    ap.add_argument("--source", help="Kismet datasource name (default: the first one)")
    a = ap.parse_args()
    k = pc.Kismet()
    uuid = k.source(a.source)["kismet.datasource.uuid"]
    pts = []
    end = time.time() + a.duration
    try:
        with k.pcap_stream(uuid) as stream:
            for btype, body in pc.iter_blocks(stream):
                if btype == 6:
                    ts, _, fr = pc.epb(body)
                    try:
                        pts.append((ts, pc.parse_radiotap(fr)[1].get("freq")))
                    except (struct.error, IndexError):
                        pass
                if time.time() >= end:
                    break
    except (socket.timeout, KeyboardInterrupt):
        pass
    if len(pts) < 3:
        raise SystemExit("too few frames (%d)" % len(pts))
    pts.sort()
    segs = []  # [freq, start, end]
    for t, f in pts:
        if segs and segs[-1][0] == f:
            segs[-1][2] = t
        else:
            segs.append([f, t, t])
    span = pts[-1][0] - pts[0][0]
    visits = collections.Counter(s[0] for s in segs)
    frames = collections.Counter(f for _, f in pts)
    gaps = sorted(((b[0] - a_[0]), a_[1], b[1]) for a_, b in zip(pts, pts[1:]))
    starts = [b[1] - a_[1] for a_, b in zip(segs, segs[1:])]

    def key(x):
        return x[0] or 0
    print(json.dumps({
        "frames": len(pts), "span_s": round(span, 1), "segments": len(segs),
        "distinct_freqs_seen": len(visits),
        "segment_start_interval_s_median": round(statistics.median(starts), 3),
        "segments_by_band": dict(collections.Counter("2.4" if s[0] and s[0] < 3000 else "5" for s in segs)),
        "visits_per_freq_per_min": {str(f): round(v / span * 60, 2) for f, v in sorted(visits.items(), key=key)},
        "frames_per_freq": {str(f): v for f, v in sorted(frames.items(), key=key)},
        "largest_gaps_s": [(round(g, 3), fa, fb) for g, fa, fb in gaps[-8:]],
        "gaps_over_1s": sum(g > 1.0 for g, _, _ in gaps),
    }, indent=1))


if __name__ == "__main__":
    main()
