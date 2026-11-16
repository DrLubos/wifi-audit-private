# wifi-sensor uploader

Sends each poll of the collector's buffer to the server - live upload,
design in `../../server/docs/live-upload.md` (sections 4, 5 and 8.3).

- **A separate process** (`wifi-sensor-uploader.service`, the sensor user,
  `Nice=10`, `MemoryMax=80M`): capture never waits on the network, and
  disabling the uploader leaves the collector untouched.
- **The buffer is the spool.** It is opened read-only; the uploader keeps
  only a cursor of what the server acknowledged
  (`/var/lib/wifi-sensor-uploader/state.json`, replaced atomically) and
  rebuilds a batch from the buffer on every attempt. The cursor moves only
  on a 2xx, so an outage, a crash or a refused batch never loses data - the
  server merges idempotently (`ingest_merge()`).
- **Every poll at once**: every `WATCH_S` (2 s) a `PRAGMA data_version`
  check; a new poll goes as one small batch (steady state: one per poll,
  every 30 s); after an outage catch-up batches of `CATCHUP_POLLS` (120) with
  `CATCHUP_PAUSE_S` (2 s) between them, oldest first. A heartbeat rides along
  with every batch, a standalone one goes when no batch went for 60 s.
- **The true time line** (`timeline.py`): polls of a boot without NTP are
  placed from the boot's clock anchor and CLOCK_MONOTONIC (buffer v8); polls
  of the current boot without an anchor are held back (after 2 min of
  contact the server's time becomes the anchor - after 30 min,
  `SERVER_ANCHOR_SYNCED_S`, while chrony is synchronised: the collector's
  own anchor is then on its way); a boot that ended without one is
  time-unverified - counted, never uploaded. A device first seen on a stale
  clock keeps the stale `first_seen` on the sensor for good, so the stale
  window of every such boot stays in the state file (`stale_windows`) and
  the value is corrected in every later batch, also after the retention has
  removed the boot's polls. A boot the state cannot know - its polls were
  pruned before uploader 1.3, or the state file was lost since - is given
  in `uploader.conf` (`STALE_WINDOWS`, uploader 1.4: `boot_id:from:to:delta`
  entries, printed by the server's `tests/sql/check_first_seen.sql`); the
  configuration is used like the remembered windows and never copied into
  the state.
- **AP load after a late NTP anchor**: the collector folds a stretch's
  pending AP load into `ap_load_hourly` when its NTP anchor commits. If the
  uploader had sent that stretch on a server anchor before, it moves its
  load cursor back once (`load_resent` in the state) and sends those hours
  again, at most 5,000 load rows a batch; the server keeps the row with
  more polls, so a second copy changes nothing. (A boot that never gets an
  NTP anchor keeps its pending load on the sensor: it is not uploaded.)
- **The installed Kismet version** (uploader 1.5, `sensorinfo.kismet_version()`:
  `kismet-core` from dpkg's status file, re-read when it changes) rides in
  every heartbeat; the server records a change of it as a dated `change`
  boundary (the version is pinned: `install_sensor.sh`, `KISMET_VERSION`).
- **Only what the server keeps**: polls, AP devices, configuration history,
  AP observations, AP load, AP frequency histograms, alerts - never a client
  device, associations, client BSSIDs or probes.
- **TLS always verified** (stdlib default context), https only; the token
  (`/etc/wifi-sensor/ingest.token`, root:<sensor user> 0640) is never logged.
- **A buffer that lost its tail**: a power cut can drop the buffer's newest
  transactions after they were uploaded, and the next boot's collector then
  reuses their row ids - new rows *before* the cursor. The state remembers
  the newest acknowledged poll (`last_poll`: id, raw ts, boot); when the
  buffer's poll with that id is another one, the cursor goes back to the
  last poll of the acknowledged boot and the rows after it are sent
  (`repair_cursor()`; found 2026-11-14: the first one or two polls after
  the power cuts of 2026-11-02 and 11-12 reached the server without their
  observations). The collector commits with `synchronous=FULL` since then,
  so the tail is no longer lost in the first place.
- **Limits**: a batch is cut to fewer polls until it has at most
  `MAX_BATCH_BYTES` (1 MiB gzip) and `MAX_BATCH_ROWS` (25,000; the server
  takes 30,000).
- **Failures**: 413 halves the batch, and so does a 422 for more than one
  poll - down to the single poll the server refuses, which then backs off
  like the rest (the server opens an `upload_rejected` event naming it);
  401/403 back off to 10 min (the server opens an `upload_unauthorized`
  event); 409 retries on the next tick; network errors, 5xx, 422 and 429
  back off exponentially from 5 s to 10 min. An unreadable state file is
  moved to `state.json.corrupt` and treated as lost: the uploader restarts
  from the server's last poll.

Files: `uploader.py` (loop, `--check`, `--once`), `batch.py` (one batch
from the buffer), `timeline.py` (time placement), `client.py` (HTTPS),
`sensorinfo.py` (adapter identity, disk, uptime), `state.py` (cursor file).
`clock.py` comes from the collector (installed next to it).

Install (on the Pi, after `install_collector.sh` of buffer v8):

    sudo ./install_uploader.sh               # see its header for the token
    sudo -u pi python3 /opt/wifi-sensor/uploader/uploader.py --check
    sudo systemctl enable --now wifi-sensor-uploader
    journalctl -u wifi-sensor-uploader -f

Rollback: `sudo systemctl disable --now wifi-sensor-uploader`.

Tests (stdlib; `test_e2e` needs the openssl command line):

    cd wifi-sensor && python3 -m unittest discover -s uploader/tests
