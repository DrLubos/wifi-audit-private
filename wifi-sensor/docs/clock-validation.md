# Clock correction – validation checklist

What it validates: the timestamp correction of the live upload
(`server/docs/live-upload.md` 8.3) – polls captured while the Pi's clock is
wrong are placed on the true time line from the boot's clock anchor and
CLOCK_MONOTONIC. Target ±10 s. The developer runs it; Claude only reads.

**Preconditions:** collector buffer v8 deployed (live upload step 3: every
poll has `mono`, `boot_id`, `clock_synced`; anchors in `clock_anchors`) and
the helpers (built 2026-10-18), run from the Pi's clone
`~/wifi-audit-private/wifi-sensor` (`git pull` first):
`sensor/clock-test.sh` (`arm` installs its own root-owned copy to
`/opt/wifi-sensor/sensor/clock-test.sh` - the one the chrony hook runs - so
later commands may use either path), `analysis/clock_validation.py`; on the
Mac `wifi-sensor/analysis/clock_truth.py`. Tests:
`python3 -m unittest discover -s sensor/tests` (on the Pi - GNU tools) and
`-s analysis/tests`.

**The uploader stays enabled** (it is since 2026-10-17): after 2 min of
contact without NTP it anchors the boot on the server's time (`SERVER_ANCHOR_AFTER_S`
120) and uploads the held polls; when chrony goes online the collector
records the NTP anchor too. So every run A also gives run D's comparison -
`clock_validation.py` reports the error of each anchor separately.

## How a wrong clock is produced without risking SSH

- The Pi has no RTC and no fake-hwclock: at boot systemd sets the clock to the
  mtime of `/var/lib/systemd/timesync/clock` (today 2026-09-15 13:52:32,
  `findings.md` section 13). `clock-test.sh arm` sets that mtime to a chosen
  time **in the past by a few hours**, so the next boot starts that far
  behind.
- It keeps chrony from correcting the clock for N minutes after that boot:
  a drop-in's `ExecStartPost` runs `clock-test.sh boot-hook` right after
  chronyd starts - it consumes the flag, runs `chronyc offline` and starts the
  transient unit `wifi-sensor-clocktest-hold`, which repeats `chronyc offline`
  every 30 s (a source added later, e.g. from DHCP, would come up online)
  and runs `chronyc online` after N minutes (timed on the uptime, so no clock
  step can shorten it). If chrony synchronised before the hook ran (lost
  race), the boot is a normal one and the log says so - disarm and arm again. chronyd itself runs
  (Kismet is ordered after `chrony.service`, so capture starts normally), and
  `makestep 1 3` steps the clock at its first update after going online.
- **Nothing touches the network**: interfaces, routes, firewall, Tailscale and
  sshd are not changed. The clock is never moved by hand (`date -s`) while
  connected: moving it back breaks WireGuard handshakes initiated by the Pi
  (replay protection) and could stall the Tailscale path.
- **Self-recovering**: the arm flag is consumed by the first boot that reads
  it (a second reboot is a normal boot); the drop-in step is `ExecStartPost=-`
  (it can never stop chrony from starting); chrony goes online by itself
  after N minutes; `sudo clock-test.sh end` ends the test at once (a bare
  `chronyc online` would be undone by the hold within 30 s);
  `clock-test.sh disarm` removes the flag and the drop-in and saves the boot
  clock again (`clock-save.sh`); its log stays in `/var/lib/wifi-sensor-clocktest/`.
- While armed, the N11 clock save (`clock-save.sh`, hourly and at shutdown)
  leaves the boot clock file alone.
- **Why a few hours and not the real 33-day stale date**: with a clock weeks
  behind, TLS fails for every certificate issued after that date - Tailscale's
  control plane and the ingest server included - so SSH over Tailscale
  (`ssh pi` = 100.x) would be gone until chrony steps. A clock 6 h behind
  keeps every certificate valid. Run C (the real stale date) is only for when
  you are on the dorm LAN and can use the LAN address.

## Run A – reboot 6 h behind, NTP held off for 30 min (then 3 h, 24 h)

1. **On the Mac, a terminal for the ground truth** (Mac clock = NTP; keep it
   running through the whole run and keep the Mac awake - `caffeinate -i`;
   one ssh session, the Pi's CLOCK_MONOTONIC once a minute, the Mac's time
   right before and after, best of 3; ±15 ms measured 2026-10-18):
   `caffeinate -i python3 wifi-sensor/analysis/clock_truth.py --host pi --out ~/clock-truth-A30.csv`
2. **Pre-flight, read-only** (`ssh pi`):
   `chronyc -c tracking | awk -F, '{print $NF}'` -> `Normal`;
   `systemctl is-active kismet wifi-sensor-collector` -> `active active`;
   `cd ~/wifi-audit-private && git pull && cd wifi-sensor`;
   `sudo sensor/clock-test.sh status` -> `not armed`.
   Not during a staged experiment.
3. **Arm** (`ssh -t pi`, in `~/wifi-audit-private/wifi-sensor`):
   `sudo sensor/clock-test.sh arm --hours-back 6 --offline-min 30`.
   It prints the time the next boot starts at. It refuses unless chrony is
   synchronised now. Nothing changes until the reboot.
4. **Reboot**: `sudo reboot`. Wait ~2 min; `ssh pi` again (the truth sampler
   logs the missed samples and continues). If `ssh pi` does not answer within
   5 min: wait - chrony goes online after 30 min and everything is back.
5. **During the 30 min, read-only checks**: `date -u` (~6 h behind);
   `sudo /opt/wifi-sensor/sensor/clock-test.sh status` -> "NTP hold running"
   and the boot line of the log; `chronyc -c tracking | awk -F, '{print $NF}'`
   -> `Not synchronised`; `journalctl -u wifi-sensor-collector -n 3` (polls
   continue); after ~2 min `journalctl -u wifi-sensor-uploader | grep anchored`
   -> "no NTP - anchored on the server's time".
6. **After 30 min**: `journalctl -u chrony | grep -i stepped` -> "System clock
   was stepped by ~21600 seconds"; `journalctl -u wifi-sensor-collector | grep -i anchor`
   -> the boot's anchor. (To end early: `sudo /opt/wifi-sensor/sensor/clock-test.sh end`.)
7. Let the truth sampler run 10 more minutes, then stop it (Ctrl-C).
8. **Evaluate**: copy the truth file to the Pi
   (`scp ~/clock-truth-A30.csv pi:`) and run, in the clone's `wifi-sensor/`:
   `python3 analysis/clock_validation.py --uploader-dir /opt/wifi-sensor/uploader --truth ~/clock-truth-A30.csv --compare-anchors --csv ~/clock-errors-A30.csv`
   (read-only on the buffer; `--uploader-dir` = the deployed correction code).
   It prints the boot's anchors (NTP, server) with the offset applied
   (~+21600 s), the Pi's own clock error, per anchor the corrected - true
   error over all truth samples, the end-to-end error of the polls as the
   uploader sends them (integer ts, so up to ±0.5 s), server - NTP anchor,
   and max |error| with PASS/FAIL against 10 s. Expected: |error| < 1 s.
9. `sudo sensor/clock-test.sh status` -> `not armed` (consumed);
   `sudo sensor/clock-test.sh disarm` (removes the drop-in). 
10. Repeat with `--offline-min 180` and `--offline-min 1440` (the drift over
    3 h and 24 h without NTP).

## Run B – a boot that ends before its anchor (time unverified)

1. Truth sampler not needed. Arm as in A (`--hours-back 6 --offline-min 30`),
   `sudo reboot`.
2. Once back, wait for ~5 polls (`journalctl -u wifi-sensor-collector -n 5`),
   then **`sudo reboot` again within the 30 min** - that boot never got an
   anchor. The second boot is normal (the flag was consumed).
3. Check: `python3 analysis/clock_validation.py --unverified` lists the boot
   (UNVERIFIED, with its poll count) - unless the uploader anchored it on the
   server's time first (2 min of contact): for a true run B, reboot again
   within 2 min of the first boot's SSH being back, or stop the uploader for
   the run (`sudo systemctl stop wifi-sensor-uploader`, start it after); on the server `sensor_seq_gaps` reason
   `unverified` and the Sensors page count (after step 4/5 of the live upload).

## Run C – the real stale date (only on the dorm LAN)

Only when you can reach the Pi on its LAN address (check first:
`ssh -o HostName=<LAN address> pi true`): as run A with
`arm --epoch 2026-09-15T13:52:32Z --offline-min 30`. Expect `ssh pi` over
Tailscale to fail until chrony steps; use the LAN address. Tests the 33-day
offset and the Kismet-time placement rules with a large step.

## Run D – server anchor (uploader running, NTP held off)

As run A with `--offline-min 30` and the uploader enabled: after 2 min of
contact the uploader anchors the boot on the server's time (heartbeat
response) and uploads the held-back polls; when chrony goes online the
collector records the NTP anchor too. `clock_validation.py --compare-anchors`
prints both and the difference (designed < 0.2 s; the heartbeat's
receive - send reads 0.35-0.4 s with synchronised clocks (2026-10-18, TCP +
TLS setup included), so up to ~0.5 s is plausible - the run measures it).
Since the uploader is enabled, every run A is also a run D. Not possible
with the real stale date (TLS, see above) - another reason for N11.

## Records

After each run: a row in `findings.md` section 7 (the window's polls are
corrected, not raw: from / to, offset, error statistics, run name) and the
truth CSV kept with the thesis data.
