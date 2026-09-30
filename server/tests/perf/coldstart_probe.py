"""Cold-start probe, run ON THE BOX by measure.sh (MODE=coldstart) right after
it made the cache cold (stdlib only; host python3, not the api container):

  python3 - http KEY            first requests through Caddy (127.0.0.1:80)
  python3 - harness KEY B64     the cold request as measure.sh's default mode
                                does it: `docker compose exec -T api python -`
                                running http_timing.py (base64 in B64)

For every request it reports what else happened on the box meanwhile:
  stall   time the VM did not run (a thread sleeps 10 ms in a loop; any
          oversleep > 30 ms is a stall - the e2-micro's CPU throttle pauses
          the whole VM in ~220 ms slices once its burst credit is spent;
          `st` in vmstat does not show it)
  rd      KB read from disk (pgpgin: file reads, page-ins, swap-ins)
  swapin  pages swapped in
  top     the processes with the most major page faults (a fault that waits
          for the disk: swap-in or a file-backed page) and CPU time (ms)
Read-only: GET requests and /proc, no database access of its own.
"""

import base64
import http.client
import os
import subprocess
import sys
import threading
import time

TICK_MS = 1000.0 / os.sysconf("SC_CLK_TCK")
STALLS = []
_stop = threading.Event()


def meter():
    while not _stop.is_set():
        t = time.time()
        time.sleep(0.01)
        d = time.time() - t - 0.01
        if d > 0.03:
            STALLS.append((t, t + d + 0.01))


def vmstat():
    v = {}
    with open("/proc/vmstat") as f:
        for line in f:
            k, n = line.split()
            if k in ("pgmajfault", "pswpin", "pgpgin"):
                v[k] = int(n)
    return v


def procs():
    """pid -> (comm, majflt, cpu ticks); comm is the executable name."""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % pid) as f:
                s = f.read()
        except OSError:
            continue
        comm = s[s.index("(") + 1:s.rindex(")")]
        f = s[s.rindex(")") + 2:].split()
        out[pid] = (comm, int(f[9]), int(f[11]) + int(f[12]))   # majflt, utime + stime
    return out


def diff(p0, p1):
    by = {}
    for pid, (comm, mf, cpu) in p1.items():
        _, mf0, cpu0 = p0.get(pid, (comm, 0, 0))
        a = by.setdefault(comm, [0, 0])
        a[0] += mf - mf0
        a[1] += (cpu - cpu0) * TICK_MS
    return by


def stalled(t0, t1):
    return sum(max(0.0, min(b, t1) - max(a, t0)) for a, b in STALLS) * 1000.0


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", 80, timeout=300)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return "HTTP %d %d B" % (r.status, len(body))


def timed(label, fn):
    p0, v0, t0 = procs(), vmstat(), time.time()
    out = fn()
    t1, v1, p1 = time.time(), vmstat(), procs()
    by = diff(p0, p1)
    flt = sorted(((mf, c) for c, (mf, _) in by.items() if mf > 0), reverse=True)[:3]
    cpu = sorted(((ms, c) for c, (_, ms) in by.items() if ms > 0), reverse=True)[:4]
    print("  %-40s %6.0f ms  stall %5.0f  rd %6d KB  swapin %4d  %s" % (
        label[:40], (t1 - t0) * 1000.0, stalled(t0, t1), v1["pgpgin"] - v0["pgpgin"],
        v1["pswpin"] - v0["pswpin"], out))
    print("  %40s majflt: %s | cpu ms: %s" % (
        "", ", ".join("%s %d" % (c, n) for n, c in flt) or "-",
        ", ".join("%s %.0f" % (c, ms) for ms, c in cpu) or "-"))


def main(argv):
    mode, key = argv[0], argv[1]
    th = threading.Thread(target=meter, daemon=True)
    th.start()
    time.sleep(0.1)
    if mode == "http":
        for p in ("/api/health", "/api/overview", "/api/overview",
                  "/api/aps/%s" % key, "/api/aps/%s" % key, "/api/aps"):
            timed("GET " + p, lambda p=p: get(p))
    else:
        src = base64.b64decode(argv[2])

        def harness():
            r = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-",
                                "cold", "/api/health", "/api/overview"], input=src, capture_output=True)
            return r.stdout.decode().strip().replace("\n", " | ")
        timed("docker compose exec api python (whole)", harness)
        timed("GET /api/overview (right after)", lambda: get("/api/overview"))
    _stop.set()
    th.join()


if __name__ == "__main__":
    main(sys.argv[1:])
