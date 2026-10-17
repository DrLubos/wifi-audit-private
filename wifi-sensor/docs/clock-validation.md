# Clock correction – validation checklist

What it validates: the timestamp correction of the live upload
(`server/docs/live-upload.md` 8.3) – polls captured while the Pi's clock is
wrong are placed on the true time line from the boot's clock anchor and
CLOCK_MONOTONIC. Target ±10 s. The developer runs it; Claude only reads.

**Preconditions:** collector buffer v8 deployed (live upload step 3: every
poll has `mono`, `boot_id`, `clock_synced`; anchors in `clock_anchors`), and
the helper scripts of that step on the Pi: `/opt/wifi-sensor/sensor/clock-test.sh`,
`/opt/wifi-sensor/analysis/clock_validation.py`; on the Mac
`wifi-sensor/analysis/clock_truth.py`. The uploader (step 4) is optional for
runs A–C and needed for run D (server anchor).

## How a wrong clock is produced without risking SSH

- The Pi has no RTC and no fake-hwclock: at boot systemd sets the clock to the
  mtime of `/var/lib/systemd/timesync/clock` (today 2026-09-15 13:52:32,
  `findings.md` section 13). `clock-test.sh arm` sets that mtime to a chosen
  time **in the past by a few hours**, so the next boot starts that far
  behind.
- It keeps chrony from correcting the clock for N minutes after that boot:
  a one-shot drop-in runs `chronyc offline` right after chronyd starts and a
  transient timer runs `chronyc online` N minutes later. chronyd itself runs
  (Kismet is ordered after `chrony.service`, so capture starts normally), and
  `makestep 1 3` steps the clock at its first update after going online.
- **Nothing touches the network**: interfaces, routes, firewall, Tailscale and
  sshd are not changed. The clock is never moved by hand (`date -s`) while
  connected: moving it back breaks WireGuard handshakes initiated by the Pi
  (replay protection) and could stall the Tailscale path.
- **Self-recovering**: the arm flag is consumed by the first boot that reads
  it (a second reboot is a normal boot); the drop-in step is `ExecStartPost=-`
  (it can never stop chrony from starting); chrony goes online by itself
  after N minutes; `sudo chronyc online` ends the test at once;
  `clock-test.sh disarm` removes every trace.
- **Why a few hours and not the real 33-day stale date**: with a clock weeks
  behind, TLS fails for every certificate issued after that date - Tailscale's
  control plane and the ingest server included - so SSH over Tailscale
  (`ssh pi` = 100.x) would be gone until chrony steps. A clock 6 h behind
  keeps every certificate valid. Run C (the real stale date) is only for when
  you are on the dorm LAN and can use the LAN address.

## Run A – reboot 6 h behind, NTP held off for 30 min (then 3 h, 24 h)

1. **On the Mac, a terminal for the ground truth** (Mac clock = NTP; keep it
   running through the whole run; it samples the Pi's CLOCK_MONOTONIC once a
   minute over ssh with the Mac's time before and after each call):
   `python3 wifi-sensor/analysis/clock_truth.py --host pi --out ~/clock-truth-A30.csv`
2. **Pre-flight, read-only** (`ssh pi`):
   `chronyc -c tracking | awk -F, '{print $NF}'` -> `Normal`;
   `systemctl is-active kismet wifi-sensor-collector` -> `active active`;
   `sudo /opt/wifi-sensor/sensor/clock-test.sh status` -> `not armed`.
   Not during a staged experiment.
3. **Arm** (`ssh -t pi`): `sudo /opt/wifi-sensor/sensor/clock-test.sh arm --hours-back 6 --offline-min 30`.
   It prints the boot epoch it set, the flag and the drop-in. Nothing changes
   until the reboot.
4. **Reboot**: `sudo reboot`. Wait ~2 min; `ssh pi` again (the truth sampler
   logs the missed samples and continues). If `ssh pi` does not answer within
   5 min: wait - chrony goes online after 30 min and everything is back.
5. **During the 30 min, read-only checks**: `date -u` (~6 h behind);
   `chronyc -c tracking | awk -F, '{print $NF}'` -> `Not synchronised`;
   `journalctl -u wifi-sensor-collector -n 3` (polls continue, `clock=unsynced`).
6. **After 30 min**: `journalctl -u chrony | grep -i stepped` -> "System clock
   was stepped by ~21600 seconds"; `journalctl -u wifi-sensor-collector | grep -i anchor`
   -> the boot's anchor. (To end early: `sudo chronyc online`.)
7. Let the truth sampler run 10 more minutes, then stop it (Ctrl-C).
8. **Evaluate**: copy the truth file to the Pi
   (`scp ~/clock-truth-A30.csv pi:`) and run
   `python3 /opt/wifi-sensor/analysis/clock_validation.py --truth ~/clock-truth-A30.csv`
   (read-only on the buffer). It prints, per truth sample, the corrected time
   of its monotonic instant minus the true time (error), the polls corrected,
   the offset applied (~-21600 s), and max |error|. Expected: |error| < 1 s.
9. `sudo /opt/wifi-sensor/sensor/clock-test.sh status` -> `not armed`
   (consumed); `sudo /opt/wifi-sensor/sensor/clock-test.sh disarm` (removes the
   drop-in). 
10. Repeat with `--offline-min 180` and `--offline-min 1440` (the drift over
    3 h and 24 h without NTP).

## Run B – a boot that ends before its anchor (time unverified)

1. Truth sampler not needed. Arm as in A (`--hours-back 6 --offline-min 30`),
   `sudo reboot`.
2. Once back, wait for ~5 polls (`journalctl -u wifi-sensor-collector -n 5`),
   then **`sudo reboot` again within the 30 min** - that boot never got an
   anchor. The second boot is normal (the flag was consumed).
3. Check: the uploader's heartbeat / `clock_validation.py --unverified` lists
   the boot with its poll count; on the server `sensor_seq_gaps` reason
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
prints both and the difference (expected < 0.2 s). Not possible with the
real stale date (TLS, see above) - another reason for N11.

## Records

After each run: a row in `findings.md` section 7 (the window's polls are
corrected, not raw: from / to, offset, error statistics, run name) and the
truth CSV kept with the thesis data.
