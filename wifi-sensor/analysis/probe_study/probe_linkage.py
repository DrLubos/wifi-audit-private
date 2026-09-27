#!/usr/bin/env python3
"""Follow-up to probe_analyze.py: can randomised MACs be re-linked? (aggregates)

1. Sequence numbers across a MAC change: is the first sequence number of a new
   randomised MAC uniform (randomised/reset) or does it continue the previous
   MAC's counter? Tests the nearest successor MAC with the same IE-content
   fingerprint starting within --gap seconds against the chance rate.
2. Lifetime of randomised MACs (seconds between their first and last frame).
3. The largest IE-order fingerprint groups (one vote per MAC): MACs, frames,
   frames per MAC, directed-SSID frames and number of distinct directed SSIDs,
   inter-arrival times, RSSI, band, DS Parameter channels - e.g. one nearby
   device that changes its MAC on every scan but always probes for one SSID.

Usage:  python3 probe_linkage.py PROBES.pcapng [--gap S] [--top N]
"""
import argparse
import collections
import os
import statistics
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_common as pc  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "collector"))
from dataset_rules import rssi_value  # noqa: E402  (floors are censored)


def load(path):
    recs = []
    for ts, fr in pc.read_pcapng(path):
        rt, d = pc.dot11(fr)
        if not pc.is_probe_request(d):
            continue
        ies = pc.parse_ies(pc.probe_body(d))
        fpo, fpc = pc.fingerprints(ies)
        ssid = next((v for k, v in ies if k == "0"), b"")
        ds = next((v[0] for k, v in ies if k == "3" and v), None)
        recs.append({"ts": ts, "mac": pc.h(d[10:16]), "la": bool(d[10] & 2),
                     "seq": struct.unpack_from("<H", d, 22)[0] >> 4, "fpo": fpo, "fpc": fpc,
                     "ssid": pc.h(ssid) if ssid else None, "freq": rt.get("freq"),
                     "sig": rssi_value(rt["signals"][0]) if rt["signals"] else None, "ds": ds,
                     "ies": sorted({pc.ie_label(k) for k, _ in ies})})
    recs.sort(key=lambda r: r["ts"])
    macs = collections.defaultdict(list)
    for r in recs:
        macs[r["mac"]].append(r)
    return recs, macs


def q(vals, digits=1):
    if len(vals) < 2:
        return vals
    d = statistics.quantiles(vals, n=10)
    return {"p10": round(d[0], digits), "median": round(statistics.median(vals), digits), "p90": round(d[-1], digits)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pcapng")
    ap.add_argument("--gap", type=float, default=10, help="max seconds between MACs (default 10)")
    ap.add_argument("--top", type=int, default=3, help="fingerprint groups to show (default 3)")
    a = ap.parse_args()
    recs, macs = load(a.pcapng)
    la = {m: v for m, v in macs.items() if v[0]["la"]}
    if not la:
        raise SystemExit("no randomised MACs in %s" % a.pcapng)

    first = [v[0]["seq"] for v in la.values()]
    n = len(first)
    print("randomised MACs: %d; first seq < 16: %d (uniform: %.1f), < 256: %d (uniform: %.1f), median %d"
          % (n, sum(s < 16 for s in first), n * 16 / 4096, sum(s < 256 for s in first), n * 256 / 4096,
             statistics.median(first)))

    deltas = []
    for m, v in la.items():
        a_last = v[-1]
        cands = [w[0] for mm, w in la.items()
                 if mm != m and w[0]["fpc"] == a_last["fpc"] and 0 < w[0]["ts"] - a_last["ts"] <= a.gap]
        if cands:
            b = min(cands, key=lambda r: r["ts"])
            deltas.append((b["seq"] - a_last["seq"]) % 4096)
    print("nearest same-fingerprint successor within %gs: %d pairs; seq delta 1..64: %d (chance %.1f), 1..256: %d (chance %.1f)"
          % (a.gap, len(deltas), sum(1 <= x <= 64 for x in deltas), len(deltas) * 64 / 4096,
             sum(1 <= x <= 256 for x in deltas), len(deltas) * 256 / 4096))

    lt = [v[-1]["ts"] - v[0]["ts"] for v in la.values()]
    print("randomised MAC lifetime: single moment %d, <= 5 s %d, <= 60 s %d, max %.0f s"
          % (sum(x == 0 for x in lt), sum(x <= 5 for x in lt), sum(x <= 60 for x in lt), max(lt)))
    g = [v for v in macs.values() if not v[0]["la"]]
    if g:
        print("global MACs: %d with %d frames" % (len(g), sum(len(v) for v in g)))

    vote = collections.Counter(collections.Counter(r["fpo"] for r in v).most_common(1)[0][0] for v in macs.values())
    for rank, (fp, cnt) in enumerate(vote.most_common(a.top), 1):
        grp = [v for v in macs.values() if collections.Counter(r["fpo"] for r in v).most_common(1)[0][0] == fp]
        frs = sorted((r for v in grp for r in v), key=lambda r: r["ts"])
        ss = collections.Counter(r["ssid"] for r in frs)
        iat = [b["ts"] - a_["ts"] for a_, b in zip(frs, frs[1:])]
        sig = [r["sig"] for r in frs if r["sig"] is not None]
        print("\nfingerprint group %d: %d MACs (%d randomised), %d frames, frames/MAC median %s"
              % (rank, cnt, sum(v[0]["la"] for v in grp), len(frs), statistics.median(len(v) for v in grp)))
        print("  directed frames %d, distinct directed SSIDs %d, top-SSID share %.2f, wildcard frames %d"
              % (sum(1 for r in frs if r["ssid"]), len([s for s in ss if s]),
                 max((c for s, c in ss.items() if s), default=0) / len(frs), ss.get(None, 0)))
        print("  inter-arrival s %s; RSSI dBm %s (stdev %.1f); 2.4 GHz frames %d/%d"
              % (q(iat), q(sig, 0), statistics.pstdev(sig) if len(sig) > 1 else 0,
                 sum(1 for r in frs if r["freq"] and r["freq"] < 3000), len(frs)))
        print("  DS Parameter channels %s" % dict(sorted(collections.Counter(r["ds"] for r in frs).items(),
                                                         key=lambda x: (x[0] is None, x[0] or 0))))
        print("  IEs %s" % frs[0]["ies"])


if __name__ == "__main__":
    main()
