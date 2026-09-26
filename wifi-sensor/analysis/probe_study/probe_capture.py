#!/usr/bin/env python3
"""Short live sample of 802.11 probe requests from Kismet's pcapng stream.

Reads the per-datasource pcapng stream (GET /datasource/pcap/by-uuid/<uuid>/
packets.pcapng, Kismet's read-only role; Kismet cannot filter it, so every
frame is streamed), keeps only probe requests (management subtype 4) and writes
them to OUT as a small radiotap pcapng. Prints aggregate stream counters and
the cost of the capture on the Pi as JSON: frames per type, how many frames the
stream delivered compared with the datasource's packet counter, CPU of Kismet,
of its capture helper and of this process, RSS, whole-system CPU.

OUT holds raw frames (MAC addresses, SSIDs): it must be on tmpfs (e.g. /tmp on
the Pi) - anything else is refused unless --allow-disk - and must be deleted
after the analysis. Never copy it off the Pi or into the repository.

Usage:  python3 probe_capture.py OUT.pcapng [--duration S] [--source NAME]
Env:    KISMET_URL, KISMET_AUTH_FILE (defaults as for the collector).
"""
import argparse
import json
import os
import resource
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_common as pc  # noqa: E402

HELPER_COMM = "kismet_cap_linu"  # /proc comm is cut to 15 characters


def snapshot(k, uuid):
    src = next(s for s in k.sources(["kismet.datasource.uuid", "kismet.datasource.num_packets",
                                      "kismet.datasource.retry_attempts"])
               if s["kismet.datasource.uuid"] == uuid)
    kp = pc.find_pids("kismet")
    hp = pc.find_pids(HELPER_COMM, prefix=True)
    return {"t": time.time(), "num_packets": src["kismet.datasource.num_packets"],
            "retry_attempts": src["kismet.datasource.retry_attempts"],
            "kismet_pid": kp[0] if kp else None, "helper_pid": hp[0] if hp else None,
            "kismet_ticks": pc.proc_cpu_ticks(kp[0]) if kp else None,
            "helper_ticks": pc.proc_cpu_ticks(hp[0]) if hp else None,
            "kismet_rss_kb": pc.proc_rss_kb(kp[0]) if kp else None,
            "sys": pc.system_cpu()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", help="probe-request pcapng to write (tmpfs)")
    ap.add_argument("--duration", type=float, default=900, help="seconds (default 900)")
    ap.add_argument("--source", help="Kismet datasource name (default: the first one)")
    ap.add_argument("--allow-disk", action="store_true", help="allow OUT outside tmpfs")
    a = ap.parse_args()
    if not a.allow_disk and not pc.on_tmpfs(a.out):
        raise SystemExit("%s is not on tmpfs - raw frames must stay in RAM (or pass --allow-disk)" % a.out)

    k = pc.Kismet()
    uuid = k.source(a.source)["kismet.datasource.uuid"]
    hz = os.sysconf("SC_CLK_TCK")
    before = snapshot(k, uuid)
    writer = pc.PcapngWriter(a.out)
    st = {"epb": 0, "probe": 0, "types": {}, "mgmt_subtypes": {}, "fcs_bad": 0,
          "radiotap_errors": 0, "bytes": 0, "first_ts": None, "last_ts": None, "ended": "duration"}
    tsres = []
    end = time.time() + a.duration
    try:
        with k.pcap_stream(uuid) as stream:
            for btype, body in pc.iter_blocks(stream):
                st["bytes"] += len(body) + 12
                if btype == 0x0A0D0D0A:
                    tsres = []
                elif btype == 1:
                    tsres.append(pc.idb_tsresol(body))
                elif btype == 6:
                    ifid = struct.unpack_from("<I", body, 0)[0]
                    ts, _, fr = pc.epb(body, tsres[ifid] if ifid < len(tsres) else 6)
                    st["epb"] += 1
                    st["first_ts"] = st["first_ts"] or ts
                    st["last_ts"] = ts
                    try:
                        rt, d = pc.dot11(fr)
                    except (struct.error, IndexError):
                        st["radiotap_errors"] += 1
                        continue
                    if rt.get("flags", 0) & 0x40:
                        st["fcs_bad"] += 1
                    if len(d) >= 2:
                        t, sub = (d[0] >> 2) & 3, d[0] >> 4
                        st["types"][t] = st["types"].get(t, 0) + 1
                        if t == 0:
                            st["mgmt_subtypes"][sub] = st["mgmt_subtypes"].get(sub, 0) + 1
                    if pc.is_probe_request(d):
                        st["probe"] += 1
                        writer.write(ts, fr)
                if time.time() >= end:
                    break
            else:
                st["ended"] = "stream closed"
    except socket.timeout:
        st["ended"] = "stream stalled (timeout)"
    except KeyboardInterrupt:
        st["ended"] = "interrupted"
    finally:
        writer.close()
    after = snapshot(k, uuid)

    span = after["t"] - before["t"]
    stream_span = (st["last_ts"] - st["first_ts"]) if st["epb"] > 1 else 0
    counted = after["num_packets"] - before["num_packets"]
    ru = resource.getrusage(resource.RUSAGE_SELF)

    def pct(t0, t1):
        return round(100.0 * (t1 - t0) / hz / span, 2) if t0 is not None and t1 is not None else None

    busy = (after["sys"][0] - before["sys"][0]) / max(1, after["sys"][1] - before["sys"][1])
    print(json.dumps({
        "duration_s": round(span, 1), "ended": st["ended"],
        "stream_frames": st["epb"], "probe_requests": st["probe"],
        "probe_share": round(st["probe"] / st["epb"], 4) if st["epb"] else None,
        "types": st["types"], "mgmt_subtypes": st["mgmt_subtypes"],
        "fcs_bad_flag": st["fcs_bad"], "radiotap_errors": st["radiotap_errors"],
        "stream_kb_per_s": round(st["bytes"] / 1024 / span, 1),
        "datasource_packets_delta": counted,
        "stream_vs_counter": round(st["epb"] / (counted * stream_span / span), 3)
        if counted and stream_span else None,
        "helper_restarts_during": after["retry_attempts"] - before["retry_attempts"],
        "cpu_pct_of_one_core": {
            "kismet": pct(before["kismet_ticks"], after["kismet_ticks"])
            if before["kismet_pid"] == after["kismet_pid"] else None,
            "helper": pct(before["helper_ticks"], after["helper_ticks"])
            if before["helper_pid"] == after["helper_pid"] else None,
            "this_process": round(100.0 * (ru.ru_utime + ru.ru_stime) / span, 2)},
        "this_process_maxrss_kb": ru.ru_maxrss,
        "kismet_rss_kb": [before["kismet_rss_kb"], after["kismet_rss_kb"]],
        "system_busy_pct_all_cores": round(100 * busy, 1),
        "out": a.out,
    }, indent=1))


if __name__ == "__main__":
    main()
