"""Shared helpers for the probe-request feasibility scripts (stdlib only).

Kismet access is resolved like the collector does it: KISMET_URL (default
http://127.0.0.1:2501) and KISMET_AUTH_FILE (default ~/.kismet/kismet_httpd.conf,
httpd_username= / httpd_password=). Credentials go into an Authorization
header, never onto a command line.

Privacy: MAC addresses and SSIDs are only ever handled as keyed hashes. The key
(SALT) is random per process and never written anywhere, so hashes cannot be
linked across runs or reversed from the output.
"""
import base64
import hashlib
import json
import os
import struct
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:2501"
DEFAULT_AUTH_FILE = "~/.kismet/kismet_httpd.conf"

SALT = os.urandom(16)


def h(data):
    """Keyed 64-bit hash of a MAC/SSID; the key lives only in this process."""
    return hashlib.blake2b(data, key=SALT, digest_size=8).hexdigest()


# ------------------------------------------------------------------ Kismet --
class Kismet:
    def __init__(self, url=None, auth_file=None):
        self.url = (url or os.environ.get("KISMET_URL") or DEFAULT_URL).rstrip("/")
        path = os.path.expanduser(auth_file or os.environ.get("KISMET_AUTH_FILE") or DEFAULT_AUTH_FILE)
        conf = {}
        with open(path) as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    conf[k] = v
        try:
            token = "%s:%s" % (conf["httpd_username"], conf["httpd_password"])
        except KeyError:
            raise SystemExit("no httpd_username/httpd_password in %s" % path)
        self._auth = "Basic " + base64.b64encode(token.encode()).decode()

    def request(self, path, fields=None, timeout=15):
        data = None
        if fields:
            data = urllib.parse.urlencode({"json": json.dumps({"fields": fields})}).encode()
        req = urllib.request.Request(self.url + path, data=data, headers={"Authorization": self._auth})
        return urllib.request.urlopen(req, timeout=timeout)

    def sources(self, fields=None):
        with self.request("/datasource/all_sources.json", fields) as r:
            return json.load(r)

    def source(self, name=None):
        """The datasource called NAME, or the only/first one."""
        srcs = self.sources()
        if name:
            srcs = [s for s in srcs if s.get("kismet.datasource.name") == name]
        if not srcs:
            raise SystemExit("no Kismet datasource%s" % (" named %r" % name if name else ""))
        return srcs[0]

    def pcap_stream(self, uuid, timeout=30):
        """Live pcapng of every frame the datasource delivers (Kismet's read-only
        role is enough). Kismet cannot filter it by frame type."""
        return self.request("/datasource/pcap/by-uuid/%s/packets.pcapng" % uuid, timeout=timeout)


# ------------------------------------------------------------------ pcapng --
def read_exact(fh, n):
    buf = b""
    while len(buf) < n:
        chunk = fh.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def iter_blocks(fh):
    """Yield (block_type, body_without_trailing_length) from a little-endian
    pcapng stream or file; stops cleanly at EOF."""
    try:
        while True:
            btype, blen = struct.unpack("<II", read_exact(fh, 8))
            yield btype, read_exact(fh, blen - 8)[:-4]
    except EOFError:
        return


def epb(body, tsresol=6):
    """(timestamp_s, interface_id, frame) of an Enhanced Packet Block body."""
    ifid, tsh, tsl, cap, _ = struct.unpack_from("<IIIII", body, 0)
    ts = (tsh << 32) | tsl
    return ts / (10 ** tsresol), ifid, body[20:20 + cap]


def idb_tsresol(body):
    """if_tsresol option of an Interface Description Block (default 6 = us)."""
    off = 8
    while off + 4 <= len(body):
        code, olen = struct.unpack_from("<HH", body, off)
        if code == 0:
            break
        if code == 9:
            return body[off + 4]
        off += 4 + ((olen + 3) & ~3)
    return 6


class PcapngWriter:
    """Minimal pcapng writer: one radiotap interface, microsecond timestamps."""

    def __init__(self, path):
        self.fh = open(path, "wb")
        self._block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
        self._block(1, struct.pack("<HHI", 127, 0, 0))

    def _block(self, btype, body):
        total = 12 + len(body)
        self.fh.write(struct.pack("<II", btype, total) + body + struct.pack("<I", total))

    def write(self, ts, frame):
        us = int(ts * 1e6)
        pad = (4 - len(frame) % 4) % 4
        self._block(6, struct.pack("<IIIII", 0, us >> 32, us & 0xFFFFFFFF, len(frame), len(frame))
                    + frame + b"\0" * pad)

    def close(self):
        self.fh.close()


def read_pcapng(path):
    """Yield (timestamp_s, frame) from a file written by PcapngWriter."""
    with open(path, "rb") as fh:
        for btype, body in iter_blocks(fh):
            if btype == 6:
                ts, _, frame = epb(body)
                yield ts, frame


# ---------------------------------------------------------------- radiotap --
# (alignment, size) of the radiotap-namespace fields
RT = {0: (8, 8), 1: (1, 1), 2: (1, 1), 3: (2, 4), 4: (1, 2), 5: (1, 1), 6: (1, 1),
      7: (2, 2), 8: (2, 2), 9: (2, 2), 10: (1, 1), 11: (1, 1), 12: (1, 1), 13: (1, 1),
      14: (2, 2), 15: (2, 2), 16: (1, 1), 17: (1, 1), 18: (4, 8), 19: (1, 3), 20: (4, 8),
      21: (2, 12), 22: (8, 12), 23: (2, 12), 24: (2, 12), 25: (2, 6), 26: (1, 1), 27: (2, 4)}
RT_NAMES = {0: "TSFT", 1: "Flags", 2: "Rate", 3: "Channel", 4: "FHSS", 5: "dBm_AntSignal",
            6: "dBm_AntNoise", 7: "LockQuality", 11: "Antenna", 12: "dB_AntSignal",
            14: "RX_flags", 18: "XChannel", 19: "MCS", 20: "A-MPDU", 21: "VHT",
            22: "timestamp", 23: "HE", 24: "HE-MU", 26: "0-len-PSDU", 27: "L-SIG"}


def parse_radiotap(fr):
    """Return (header_length, fields). fields: present (names, '@N' = N-th
    presence word), signals (all dBm_AntSignal values), nwords, and tsft,
    flags, rate, freq, chflags, rxflags when present."""
    rt_len = struct.unpack_from("<H", fr, 2)[0]
    words = []
    off = 4
    while True:
        w = struct.unpack_from("<I", fr, off)[0]
        words.append(w)
        off += 4
        if not w & 0x80000000:
            break
    out = {"present": [], "signals": [], "nwords": len(words)}
    ns = "rt"
    for wi, w in enumerate(words):
        for bit in range(29):
            if not w & (1 << bit) or ns != "rt":
                continue
            out["present"].append(RT_NAMES.get(bit, "b%d" % bit) + ("" if wi == 0 else "@%d" % wi))
            al, sz = RT.get(bit, (1, 0))
            if sz == 0:
                out["unparsed"] = True
                return rt_len, out
            off = (off + al - 1) & ~(al - 1)
            if bit == 0:
                out["tsft"] = struct.unpack_from("<Q", fr, off)[0]
            elif bit == 1:
                out["flags"] = fr[off]
            elif bit == 2:
                out["rate"] = fr[off]
            elif bit == 3:
                out["freq"], out["chflags"] = struct.unpack_from("<HH", fr, off)
            elif bit == 5:
                out["signals"].append(struct.unpack_from("<b", fr, off)[0])
            elif bit == 14:
                out["rxflags"] = struct.unpack_from("<H", fr, off)[0]
            off += sz
        if w & (1 << 29):
            ns = "rt"
        elif w & (1 << 30):
            ns = "vendor"
            off = (off + 1) & ~1
            off += 6 + struct.unpack_from("<H", fr, off + 4)[0]
    return rt_len, out


def dot11(fr):
    """802.11 part of a radiotap frame (FCS stripped when flagged)."""
    rt_len, rt = parse_radiotap(fr)
    d = fr[rt_len:]
    if rt.get("flags", 0) & 0x10:
        d = d[:-4]
    return rt, d


def is_probe_request(d):
    return len(d) >= 24 and (d[0] & 0xFC) == 0x40


# --------------------------------------------------------------------- IEs --
IE_NAMES = {0: "SSID", 1: "SuppRates", 3: "DSParam", 10: "Request", 45: "HTCap",
            50: "ExtRates", 59: "SuppOpClasses", 70: "RMEnabledCap", 107: "Interworking",
            114: "MeshID", 127: "ExtCap", 191: "VHTCap", 199: "OperatingModeNotif",
            55: "FTE", 48: "RSN", 54: "MobilityDomain", 221: "Vendor", 255: "Ext"}
EXT_NAMES = {35: "HECap", 108: "EHTCap", 2: "FILSReqParams", 59: "HE6GHzBandCap",
             106: "MultiLink", 107: "EHTOp"}
VENDOR_NAMES = {"0050f2:04": "WPS", "0050f2:02": "WMM", "0050f2:08": "Microsoft-08",
                "506f9a:09": "P2P", "506f9a:16": "MBO-OCE", "506f9a:10": "WFD",
                "00904c:33": "Epigram-HTCap", "00904c:04": "Broadcom", "001018:02": "Broadcom",
                "0017f2:0a": "Apple", "8cfdf0:01": "Qualcomm"}
WPS_KEY = "V:0050f2:04"


def parse_ies(body):
    """[(key, value)] - key is the element id, 'E<ext id>' for extension
    elements, 'V:<oui>:<type>' for vendor elements, 'TRUNC' for a cut-off one."""
    ies = []
    off = 0
    while off + 2 <= len(body):
        eid, ln = body[off], body[off + 1]
        val = body[off + 2: off + 2 + ln]
        if len(val) < ln:
            ies.append(("TRUNC", b""))
            break
        if eid == 221 and ln >= 4:
            key = "V:%s:%02x" % (val[:3].hex(), val[3])
        elif eid == 255 and ln >= 1:
            key = "E%d" % val[0]
        else:
            key = "%d" % eid
        ies.append((key, val))
        off += 2 + ln
    return ies


def ie_label(key):
    if key.startswith("V:"):
        return "Vendor " + VENDOR_NAMES.get(key[2:], "OUI " + key[2:])
    if key.startswith("E"):
        return "Ext " + EXT_NAMES.get(int(key[1:]), key[1:])
    if key == "TRUNC":
        return "TRUNCATED"
    return IE_NAMES.get(int(key), "IE " + key)


def probe_body(d):
    """IE part of a probe request (skips the 4-byte HT Control when +HTC is set)."""
    return d[28:] if d[1] & 0x80 else d[24:]


def fingerprints(ies):
    """(order_fp, content_fp) over all IEs except SSID and DS Parameter Set.
    WPS elements count by presence only (they carry UUID-E / device names)."""
    stable = [(k, v) for k, v in ies if k not in ("0", "3")]
    content = [(k, b"" if k == WPS_KEY else v) for k, v in stable]
    return h(repr([k for k, _ in stable]).encode()), h(repr(content).encode())


# ------------------------------------------------------------------- /proc --
def find_pids(comm, prefix=False):
    """PIDs whose /proc comm equals COMM (or starts with it if PREFIX)."""
    pids = []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open("/proc/%s/comm" % p) as fh:
                    c = fh.read().strip()
            except OSError:
                continue
            if c == comm or (prefix and c.startswith(comm)):
                pids.append(int(p))
    return pids


def proc_cpu_ticks(pid):
    """utime + stime of a process in clock ticks (None if gone)."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            f = fh.read().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])
    except (OSError, IndexError, ValueError):
        return None


def proc_rss_kb(pid):
    try:
        with open("/proc/%d/status" % pid) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def system_cpu():
    """(busy_ticks, total_ticks) from /proc/stat (iowait counted as idle)."""
    with open("/proc/stat") as fh:
        v = [int(x) for x in fh.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v) - idle, sum(v)


def on_tmpfs(path):
    """True when PATH (or its directory) lies on a tmpfs/ramfs mount."""
    path = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    best, fstype = "", None
    with open("/proc/mounts") as fh:
        for line in fh:
            _, mnt, fs = line.split()[:3]
            mnt = mnt.replace("\\040", " ")
            if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                best, fstype = mnt, fs
    return fstype in ("tmpfs", "ramfs")
