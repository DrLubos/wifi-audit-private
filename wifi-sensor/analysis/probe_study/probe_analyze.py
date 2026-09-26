#!/usr/bin/env python3
"""Aggregate-only analysis of a probe-request pcapng written by probe_capture.py.

Reports, as JSON: probe rate and per-minute counts; share of locally
administered (randomised) source MACs; radiotap fields actually delivered
(presence, dBm signal, TSFT, flags, channel); received channel vs the DS
Parameter Set channel (adjacent-channel reception on 2.4 GHz); IE presence per
frame and per MAC; IE fingerprints (IE order, IE content; one vote per MAC) with
entropy and anonymity-set sizes; sequence-number behaviour within a MAC and
across candidate MAC changes; bursts; directed vs wildcard probes.

MACs and SSIDs are only handled as keyed hashes with a per-run random key;
nothing identifying is printed.

Usage:  python3 probe_analyze.py PROBES.pcapng
"""
import collections
import json
import math
import os
import statistics
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_common as pc  # noqa: E402

BAND_SPECIFIC = ("45", "191", "E35", "E108", "E59")  # HT/VHT/HE/EHT caps


def entropy(counter):
    n = sum(counter.values())
    return -sum(c / n * math.log2(c / n) for c in counter.values()) if n else 0.0


def load(path):
    recs, rt_stats = [], collections.defaultdict(collections.Counter)
    for ts, fr in pc.read_pcapng(path):
        rt, d = pc.dot11(fr)
        if not pc.is_probe_request(d):
            continue
        for p in set(rt["present"]):
            rt_stats["present"][p] += 1
        rt_stats["nwords"][rt["nwords"]] += 1
        rt_stats["nsig"][len(rt["signals"])] += 1
        rt_stats["flags"][rt.get("flags")] += 1
        rt_stats["unparsed"][bool(rt.get("unparsed"))] += 1
        ies = pc.parse_ies(pc.probe_body(d))
        ssid = next((v for k, v in ies if k == "0"), None)
        ds = next((v for k, v in ies if k == "3"), None)
        sa = d[10:16]
        fp_order, fp_content = pc.fingerprints(ies)
        recs.append({
            "ts": ts, "tsft": rt.get("tsft"), "sig": rt["signals"][0] if rt["signals"] else None,
            "freq": rt.get("freq"), "mac": pc.h(sa), "la": bool(sa[0] & 2), "mc": bool(sa[0] & 1),
            "da_bcast": d[4:10] == b"\xff" * 6, "bssid_bcast": d[16:22] == b"\xff" * 6,
            "seq": struct.unpack_from("<H", d, 22)[0] >> 4, "frag": d[22] & 0xF,
            "ssid_len": None if ssid is None else len(ssid), "ssid_h": pc.h(ssid) if ssid else None,
            "ds_ch": ds[0] if ds else None, "ie_keys": [k for k, _ in ies],
            "fp_order": fp_order, "fp_content": fp_content,
            "fp_no_caps": pc.h(repr([(k, b"" if k == pc.WPS_KEY else v) for k, v in ies
                                     if k not in ("0", "3") + BAND_SPECIFIC]).encode()),
            "wps_uuid": any(k == pc.WPS_KEY and b"\x10\x47" in v for k, v in ies),
        })
    recs.sort(key=lambda r: r["ts"])
    return recs, rt_stats


def fp_stats(macs, key):
    def vote(v):
        return collections.Counter(r[key] for r in v).most_common(1)[0][0]

    per_mac = collections.Counter(vote(v) for v in macs.values())
    sizes = collections.Counter(per_mac.values())
    out = {"distinct_over_frames": len({r[key] for v in macs.values() for r in v}),
           "distinct_over_macs": len(per_mac),
           "entropy_bits_over_macs": round(entropy(per_mac), 2),
           "max_entropy_bits": round(math.log2(len(macs)), 2),
           "macs_with_unique_fp": sizes.get(1, 0),
           "largest_anonymity_set": max(per_mac.values()),
           "macs_with_gt1_fp": sum(1 for v in macs.values() if len({r[key] for r in v}) > 1)}
    la = [v for v in macs.values() if v[0]["la"]]
    if la:
        pm = collections.Counter(vote(v) for v in la)
        out["la_only"] = {"macs": len(la), "distinct": len(pm), "entropy_bits": round(entropy(pm), 2),
                          "largest_set": max(pm.values())}
    return out


def main(path):
    recs, rts = load(path)
    n = len(recs)
    if n < 2:
        print(json.dumps({"frames": n}))
        return
    R = {"frames": n}
    t0, t1 = recs[0]["ts"], recs[-1]["ts"]
    mins = (t1 - t0) / 60
    R["span_min"] = round(mins, 2)
    R["probes_per_min"] = round(n / mins, 1)
    per_min = collections.Counter(int((r["ts"] - t0) // 60) for r in recs)
    R["per_minute"] = [per_min.get(i, 0) for i in range(int(mins) + 1)]

    macs = collections.defaultdict(list)
    for r in recs:
        macs[r["mac"]].append(r)
    R["distinct_macs"] = len(macs)
    R["la_share_frames"] = round(sum(r["la"] for r in recs) / n, 3)
    R["la_share_macs"] = round(sum(v[0]["la"] for v in macs.values()) / len(macs), 3)
    R["multicast_sa"] = sum(r["mc"] for r in recs)
    R["frames_per_mac"] = dict(collections.Counter(
        "1" if len(v) == 1 else "2-5" if len(v) <= 5 else "6-20" if len(v) <= 20 else ">20"
        for v in macs.values()))

    # radiotap
    R["radiotap_present_share"] = {k: round(c / n, 3) for k, c in rts["present"].most_common()}
    R["radiotap_presence_words"] = dict(rts["nwords"])
    R["radiotap_n_dbm_signals"] = dict(rts["nsig"])
    R["radiotap_flags_values"] = {("None" if k is None else hex(k)): c for k, c in rts["flags"].items()}
    R["radiotap_unparsed"] = rts["unparsed"][True]
    sigs = [r["sig"] for r in recs if r["sig"] is not None]
    if len(sigs) > 1:
        q = statistics.quantiles(sigs, n=10)
        R["dbm_signal"] = {"min": min(sigs), "p10": q[0], "median": statistics.median(sigs),
                           "p90": q[-1], "max": max(sigs)}
    tsft = [r["tsft"] for r in recs if r["tsft"] is not None]
    if len(tsft) > 2:
        R["tsft"] = {"frames_with": len(tsft),
                     "monotonic_pairs_share": round(sum(b >= a for a, b in zip(tsft, tsft[1:])) / (len(tsft) - 1), 3),
                     "distinct": len(set(tsft)), "zero": tsft.count(0)}
    freqs = collections.Counter(r["freq"] for r in recs)
    R["probe_band"] = dict(collections.Counter(
        "2.4" if f and f < 3000 else "5" if f and f < 5950 else "6/unknown" for f in freqs.elements()))
    R["probe_freq"] = {str(k): c for k, c in sorted(freqs.items(), key=lambda x: x[0] or 0)}

    def f2c(f):
        return 14 if f == 2484 else (f - 2407) // 5
    ds = [(r["freq"], r["ds_ch"]) for r in recs if r["ds_ch"] and r["freq"] and r["freq"] < 3000]
    if ds:
        R["dsparam_vs_rx_channel_2g"] = dict(collections.Counter(
            "same" if f2c(f) == c else "off_by_%d" % abs(f2c(f) - c) for f, c in ds))

    # IEs
    ie_frames, ie_macs = collections.Counter(), collections.Counter()
    for r in recs:
        ie_frames.update({pc.ie_label(k) for k in r["ie_keys"]})
    for v in macs.values():
        ie_macs.update({pc.ie_label(k) for r in v for k in r["ie_keys"]})
    R["ie_share_frames"] = {k: round(c / n, 3) for k, c in ie_frames.most_common()}
    R["ie_share_macs"] = {k: round(c / len(macs), 3) for k, c in ie_macs.most_common()}
    R["wps_uuid_frames"] = sum(r["wps_uuid"] for r in recs)
    R["ie_count_per_frame"] = dict(sorted(collections.Counter(len(r["ie_keys"]) for r in recs).items()))
    for key in ("fp_order", "fp_content", "fp_no_caps"):
        R[key] = fp_stats(macs, key)

    # sequence numbers within one MAC
    within = collections.Counter()
    for v in macs.values():
        for a, b in zip(v, v[1:]):
            dlt = (b["seq"] - a["seq"]) % 4096
            within["same" if dlt == 0 else "+1..16" if dlt <= 16 else "+17..256" if dlt <= 256
                   else "+257..2047" if dlt < 2048 else "backwards"] += 1
    R["seq_within_mac_consecutive_deltas"] = dict(within)
    R["seq_zero_frames"] = sum(r["seq"] == 0 for r in recs)
    R["frag_nonzero"] = sum(bool(r["frag"]) for r in recs)

    # candidate MAC changes: A's last frame -> B's first frame within 60 s (both randomised)
    firsts = sorted((v[0]["ts"], m) for m, v in macs.items() if v[0]["la"])
    link = collections.Counter()
    for m, v in macs.items():
        if not v[0]["la"]:
            continue
        a = v[-1]
        for tsb, mb in firsts:
            if mb == m or not 0 < tsb - a["ts"] <= 60:
                continue
            b = macs[mb][0]
            kind = "samefp" if b["fp_content"] == a["fp_content"] else "difffp"
            link[kind + "_pairs"] += 1
            link[kind + "_seq_close"] += 1 <= (b["seq"] - a["seq"]) % 4096 <= 64
    R["mac_change_candidates_60s"] = dict(link)
    R["seq_close_chance_rate"] = round(64 / 4096, 4)

    # bursts: per MAC, split on gaps > 2 s
    bursts = []
    for v in macs.values():
        cur = [v[0]]
        for a, b in zip(v, v[1:]):
            if b["ts"] - a["ts"] > 2:
                bursts.append(cur)
                cur = []
            cur.append(b)
        bursts.append(cur)
    R["bursts"] = len(bursts)
    R["bursts_per_min"] = round(len(bursts) / mins, 1)
    R["frames_per_burst"] = dict(sorted(collections.Counter(min(len(b), 10) for b in bursts).items()))
    R["channels_per_burst"] = dict(sorted(collections.Counter(len({r["freq"] for r in b}) for b in bursts).items()))
    R["burst_span_s_median"] = round(statistics.median(b[-1]["ts"] - b[0]["ts"] for b in bursts), 3)
    sd = [statistics.pstdev([r["sig"] for r in v]) for v in macs.values()
          if len(v) >= 3 and all(r["sig"] is not None for r in v)]
    if sd:
        R["rssi_stdev_within_mac_median"] = round(statistics.median(sd), 2)

    # directed vs wildcard
    R["ssid_ie"] = {"wildcard": sum(r["ssid_len"] == 0 for r in recs),
                    "directed": sum(bool(r["ssid_len"]) for r in recs),
                    "missing": sum(r["ssid_len"] is None for r in recs)}
    R["directed_distinct_ssids"] = len({r["ssid_h"] for r in recs if r["ssid_h"]})
    R["macs_sending_directed"] = sum(any(r["ssid_len"] for r in v) for v in macs.values())
    R["la_macs_sending_directed"] = sum(v[0]["la"] and any(r["ssid_len"] for r in v) for v in macs.values())
    R["da_broadcast_share"] = round(sum(r["da_bcast"] for r in recs) / n, 3)
    R["bssid_wildcard_share"] = round(sum(r["bssid_bcast"] for r in recs) / n, 3)
    print(json.dumps(R, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__.strip().splitlines()[-1])
    main(sys.argv[1])
