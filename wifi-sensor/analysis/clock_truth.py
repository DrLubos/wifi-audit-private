#!/usr/bin/env python3
"""Ground truth for the clock validation (docs/clock-validation.md), run on
the Mac (its clock = NTP) through a whole test run.

It keeps one ssh session to the Pi open, running a tiny responder there,
and once per --interval reads the Pi's CLOCK_MONOTONIC (the clock the
collector records with every poll), its wall clock and boot id; the Mac's
time is taken right before and after each exchange. Of --tries exchanges
the one with the shortest round trip is kept: its true time is the midpoint,
uncertain by half the round trip (tens of ms over Tailscale). Across the
reboot the session drops; the sampler logs the missed samples and
reconnects - a new boot id starts a new monotonic time line.

    caffeinate -i python3 wifi-sensor/analysis/clock_truth.py --host pi --out ~/clock-truth-A30.csv

CSV columns: mac_t0, mac_t1 (Mac time.time() around the exchange), true_ts
(midpoint), half_rtt_s, pi_mono (time.monotonic() on the Pi), pi_wall
(time.time() on the Pi - the wrong clock under test), pi_wall_err_s (pi_wall -
true_ts), boot_id, status (ok | missed: reason). Stop with Ctrl-C. Read-only
on the Pi; standard library only.
"""

import argparse
import csv
import os
import select
import shlex
import subprocess
import sys
import time

RESPONDER = (
    "import sys, time\n"
    "b = open('/proc/sys/kernel/random/boot_id').read().strip()\n"
    "for _ in sys.stdin:\n"
    "    m = time.monotonic(); w = time.time()\n"
    "    print(repr(m), repr(w), b, flush=True)\n"
)
FIELDS = ["mac_t0", "mac_t1", "true_ts", "half_rtt_s", "pi_mono", "pi_wall", "pi_wall_err_s", "boot_id", "status"]


class Session:
    def __init__(self, host, timeout):
        self.host, self.timeout, self.proc = host, timeout, None

    def open(self):
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
               self.host, "python3 -u -c " + shlex.quote(RESPONDER)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def close(self):
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        self.proc = None

    def exchange(self):
        """(mac_t0, mac_t1, pi_mono, pi_wall, boot_id); raises OSError."""
        if self.proc is None or self.proc.poll() is not None:
            self.close()
            self.open()
        t0 = time.time()
        self.proc.stdin.write("\n")
        self.proc.stdin.flush()
        ready, _, _ = select.select([self.proc.stdout], [], [], self.timeout)
        if not ready:
            raise OSError("no answer within %s s" % self.timeout)
        line = self.proc.stdout.readline()
        t1 = time.time()
        if not line:
            raise OSError("session closed")
        mono, wall, boot = line.split()
        return t0, t1, float(mono), float(wall), boot


def sample(session, tries):
    best = None
    for _ in range(tries):
        t0, t1, mono, wall, boot = session.exchange()
        if best is None or t1 - t0 < best[1] - best[0]:
            best = (t0, t1, mono, wall, boot)
    return best


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="pi", help="ssh host (default: pi)")
    ap.add_argument("--out", required=True, help="CSV to append to")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between samples (default 60)")
    ap.add_argument("--tries", type=int, default=3, help="exchanges per sample, shortest kept (default 3)")
    ap.add_argument("--timeout", type=float, default=15.0, help="seconds to wait for an answer (default 15)")
    args = ap.parse_args(argv)

    out = os.path.expanduser(args.out)
    new = not os.path.exists(out) or os.path.getsize(out) == 0
    session = Session(args.host, args.timeout)
    last_boot = None
    print("clock_truth: sampling %s every %.0f s into %s (Ctrl-C to stop)" % (args.host, args.interval, out))
    with open(out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        nxt = time.monotonic()
        try:
            while True:
                try:
                    t0, t1, mono, wall, boot = sample(session, args.tries)
                    mid = (t0 + t1) / 2
                    row = {"mac_t0": "%.6f" % t0, "mac_t1": "%.6f" % t1, "true_ts": "%.6f" % mid,
                           "half_rtt_s": "%.6f" % ((t1 - t0) / 2), "pi_mono": "%.6f" % mono,
                           "pi_wall": "%.6f" % wall, "pi_wall_err_s": "%.3f" % (wall - mid),
                           "boot_id": boot, "status": "ok"}
                    if boot != last_boot:
                        print("%s boot %s (Pi clock %+.1f s off)" % (time.strftime("%H:%M:%S"), boot, wall - mid))
                        last_boot = boot
                except (OSError, ValueError) as e:
                    session.close()
                    row = {k: "" for k in FIELDS}
                    row.update({"mac_t0": "%.6f" % time.time(), "status": "missed: %s" % e})
                    print("%s missed: %s" % (time.strftime("%H:%M:%S"), e))
                w.writerow(row)
                f.flush()
                nxt += args.interval
                time.sleep(max(0.0, nxt - time.monotonic()))
        except KeyboardInterrupt:
            print("\nclock_truth: stopped")
        finally:
            session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
