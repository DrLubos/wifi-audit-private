#!/usr/bin/env python3
"""Does the Kismet stream lose probe requests? Cross-check against a raw socket.

Needs root (CAP_NET_RAW). For --duration seconds it counts probe requests on
  - a raw AF_PACKET socket bound to the monitor VIF, with a kernel BPF filter
    that accepts only probe requests (receive only - nothing is ever sent), and
  - Kismet's pcapng stream for the same datasource,
and compares the two sets by keyed (SA, sequence number) hashes held in memory.
Prints aggregates only (counts, overlap, the socket's kernel drop counter, CPU
seconds per thread); writes nothing.

Usage:  sudo python3 raw_vs_kismet.py IFACE [--duration S] [--auth-file PATH]
        (IFACE is the monitor VIF, e.g. wlan1mon; the auth file defaults to
        ~SUDO_USER/.kismet/kismet_httpd.conf)
Result 2026-09-26/27 (night, 300 s): 58/58 probe requests on both, 0 drops.
"""
import argparse
import ctypes
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_common as pc  # noqa: E402

SOL_PACKET, PACKET_STATISTICS, SO_ATTACH_FILTER, ETH_P_ALL = 263, 6, 26, 3

# cBPF: A = radiotap length (LE u16 at offset 2); accept iff (frame[A] & 0xfc) == 0x40
BPF_PROBE_REQ = [(0x30, 0, 0, 3), (0x64, 0, 0, 8), (0x07, 0, 0, 0), (0x30, 0, 0, 2),
                 (0x0c, 0, 0, 0), (0x07, 0, 0, 0), (0x50, 0, 0, 0), (0x54, 0, 0, 0xfc),
                 (0x15, 0, 1, 0x40), (0x06, 0, 0, 0xffff), (0x06, 0, 0, 0)]


def key(frame):
    rt_len = struct.unpack_from("<H", frame, 2)[0]
    d = frame[rt_len:]
    if not pc.is_probe_request(d):
        return None
    return hashlib.blake2b(d[10:16] + d[22:24], key=pc.SALT, digest_size=8).digest()


def raw_thread(iface, duration, res):
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    buf = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *i) for i in BPF_PROBE_REQ))
    s.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, struct.pack("HL", len(BPF_PROBE_REQ), ctypes.addressof(buf)))
    s.bind((iface, 0))
    s.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8)  # reading resets the counters
    s.settimeout(0.5)
    end = time.time() + duration
    while time.time() < end:
        try:
            k = key(s.recv(65535))
        except socket.timeout:
            continue
        if k:
            res["raw"].add(k)
    res["raw_sock_packets"], res["raw_sock_drops"] = struct.unpack("II", s.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
    res["raw_cpu"] = time.thread_time()


def kismet_thread(kis, uuid, duration, res):
    n = 0
    end = time.time() + duration
    try:
        with kis.pcap_stream(uuid) as stream:
            for btype, body in pc.iter_blocks(stream):
                if btype == 6:
                    n += 1
                    k = key(pc.epb(body)[2])
                    if k:
                        res["kis"].add(k)
                if time.time() >= end:
                    break
    except socket.timeout:
        res["kis_stalled"] = True
    res["kis_frames_all"] = n
    res["kis_cpu"] = time.thread_time()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("iface", help="monitor VIF, e.g. wlan1mon")
    ap.add_argument("--duration", type=float, default=300, help="seconds (default 300)")
    ap.add_argument("--auth-file", help="Kismet httpd auth file")
    ap.add_argument("--source", help="Kismet datasource name (default: the first one)")
    a = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("needs root for the raw socket: sudo python3 %s ..." % sys.argv[0])
    auth = a.auth_file or os.environ.get("KISMET_AUTH_FILE")
    if not auth and os.environ.get("SUDO_USER"):
        auth = os.path.expanduser("~%s/.kismet/kismet_httpd.conf" % os.environ["SUDO_USER"])
    kis = pc.Kismet(auth_file=auth)
    uuid = kis.source(a.source)["kismet.datasource.uuid"]
    res = {"raw": set(), "kis": set(), "raw_cpu": 0.0, "kis_cpu": 0.0}
    ts = [threading.Thread(target=raw_thread, args=(a.iface, a.duration, res)),
          threading.Thread(target=kismet_thread, args=(kis, uuid, a.duration, res))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    raw, kset = res["raw"], res["kis"]
    print(json.dumps({
        "duration_s": a.duration, "probe_keys_raw": len(raw), "probe_keys_kismet": len(kset),
        "both": len(raw & kset), "raw_only": len(raw - kset), "kismet_only": len(kset - raw),
        "raw_socket_packets_after_bpf": res.get("raw_sock_packets"),
        "raw_socket_drops": res.get("raw_sock_drops"),
        "kismet_stream_frames_all": res.get("kis_frames_all"),
        "kismet_stream_stalled": res.get("kis_stalled", False),
        "cpu_s_raw_thread": round(res["raw_cpu"], 2), "cpu_s_kismet_stream_thread": round(res["kis_cpu"], 2),
    }, indent=1))


if __name__ == "__main__":
    main()
