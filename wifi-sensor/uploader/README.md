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
  contact the server's time becomes the anchor); a boot that ended without
  one is time-unverified - counted, never uploaded.
- **Only what the server keeps**: polls, AP devices, configuration history,
  AP observations, AP load, AP frequency histograms, alerts - never a client
  device, associations, client BSSIDs or probes.
- **TLS always verified** (stdlib default context), https only; the token
  (`/etc/wifi-sensor/ingest.token`, root:<sensor user> 0640) is never logged.
- **Failures**: 413 halves the batch; 401/403 back off to 10 min (the server
  opens an `upload_unauthorized` event); 409 retries on the next tick;
  network errors, 5xx, 422 and 429 back off exponentially from 5 s to 10 min.

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
