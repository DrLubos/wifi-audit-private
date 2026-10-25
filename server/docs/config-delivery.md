# Configuration delivery to the sensor – design

Status: **built 2026-10-25 and deployed with `CONFIG_APPLY=0`** - delivery
is OFF: no sensor accepts a configuration until the developer sets
`CONFIG_APPLY=1` after the staged test of section 12 (which changes capture
and needs the developer's OK). Decisions C1-C13 accepted as recommended
(2026-10-25). As built and deviations: section 14. It takes up what live
upload D9 deferred (`live-upload.md` 13.1): the desired capture
configuration the operator saves in Settings -> Sensors (`sensor_config`)
reaches the Pi, is applied safely, and its effect is a dated dataset
boundary.

Inputs (read 2026-10-24): `api/app/v2/writes.py` (`parse_capture_config`,
`plan_channels`, the stride rule), `app/v2/sql/writes.sql`
(`sensor_config_insert`), `schema.sql` (`sensor_config`,
`sensor_annotations`), `wifi-sensor/install_sensor.sh` (the `source=` line,
`hop_stride_ok`), `sensor/hop_guard.py`, `collector/collector.py`
(`POLL_INTERVAL`), `uploader/uploader.py`, and on the Pi read-only:
`/etc/kismet/kismet_site.conf` and `/etc/wifi-sensor/*.conf` are
`root:root 0644`, the uploader runs as `pi` with `ProtectSystem=strict`,
the regulatory domain is **SK (DFS-ETSI)** (`iw reg get`).

Contents: 1 Summary · 2 What a configuration is · 3 Delivery (pull) ·
4 Validation · 5 Safe apply on the Pi · 6 Dataset boundary · 7 Authorization
· 8 UI · 9 Schema 17 / buffer v9 · 10 Decisions · 11 Failure modes ·
12 Tests · 13 Effort and build order

---

## 1. Summary

```
Settings -> Sensors (WRITE_TOKEN, audit_log)          server                      Pi
  sensor_config vN (desired) ─────────────────> heartbeat / batch response ──> uploader (pi, no root)
                                                  "config": {version, sha256,      writes desired.json
                                                   desired}  only when the          (its state dir)
                                                   sensor's applied version              │ systemd .path
                                                   differs                               ▼
                                                                               config-apply (root, no network)
                                                                                 validate again (adapter, regdomain)
                                                                                 keep last-good, write files,
                                                                                 apply live, verify capture,
                                                                                 roll back on failure
                                                                                 -> result.json
  sensor_config_attempts, confirmed_at,  <──── next heartbeat: "config_state" ── uploader reads result.json
  sensor_annotations boundary (applied)
```

- The **server never connects to the Pi**: the uploader already talks to the
  server every 30-60 s; the desired version rides in the responses it gets.
- **Two processes on the Pi**: the network-facing uploader stays
  unprivileged and only hands the desired configuration over as a file; a
  small root applier with no network validates, applies, verifies and rolls
  back. Neither trusts the other's input.
- **Validated on both sides**: the server at save time and at delivery
  (rules + the capabilities the sensor reported), the Pi again against the
  live adapter. A value outside the rules is refused before anything is
  written.
- **Apply = write the files, apply live, verify that collecting polls
  resume with the new hop list; otherwise restore the last good files and
  verify that too.** Reported as applied / rejected / rolled_back / failed,
  with the reason.
- **Every applied change is a dated dataset boundary** (`sensor_annotations`
  'change', `boundary` true, at the first collecting poll under the new
  configuration) and each poll carries the configuration version it was
  captured with (buffer v9 / schema 17, C6).

## 2. What a configuration is

The fields Settings -> Sensors already stores (`sensor_config`, validated by
`parse_capture_config`):

| Field | Values today | Where it lands on the Pi | Takes effect by |
|---|---|---|---|
| `channel_plan` + `channels` | `all_89`, `ht20_37`, `locked` (one 20 MHz channel), `custom` (1-200 Kismet entries, each once) | `/etc/kismet/kismet_site.conf`, the `channels="…"` option of the `source=` line | live: Kismet's `POST /datasource/by-uuid/<uuid>/set_channel.cmd` (what the hop guard does) - no restart (C1) |
| `dwell_s` | 0.2, 0.5, 1.0 s | the source line's hop rate (5, 2, 1 per second) | the same call (`rate`) |
| `poll_s` | 15, 30, 60 s | `/etc/wifi-sensor/collector.conf` `POLL_INTERVAL` | a collector restart (it reads its config at start; Kismet keeps capturing, the resume point covers the seconds) |

A configuration is identified by `(sensor, version)` and by the **sha256 of
its canonical JSON** (`{"v":1,"channels":[…],"dwell_s":…,"poll_s":…}`, keys
sorted, no whitespace) - the Pi reports the hash it applied, so "applied"
means byte-for-byte this configuration, not just the number.

## 3. Delivery: the sensor pulls

- Every heartbeat (inside a batch or standalone) gains
  `"config_state": {"applied_version", "applied_sha256", "attempt": {…} | null,
  "capabilities": {…}}` (section 4.2, 5.5).
- `POST /heartbeat` and `POST /batch` responses gain `"config"`:
  `{"version": N, "sha256": "…", "desired": {…}}` **only when** the newest
  `sensor_config` row of the sensor is not the applied one and has not
  been refused for this hash already (section 5.6); otherwise `"config":
  null`. Steady state costs nothing; a pending change reaches the sensor
  within one poll (~30 s) of being saved, or at the next contact after an
  outage.
- The uploader validates the shape (section 4.3), writes
  `/var/lib/wifi-sensor-uploader/config/desired.json` atomically (temp,
  fsync, rename; `0640 pi:pi`) and does nothing else with it. The server
  records `delivered_at` when the next heartbeat echoes the version in
  `config_state.attempt` (state `received`).
- `CONFIG_APPLY=0|1` in `uploader.conf` (default **0**, C12): with 0 the
  uploader reports `config_state.accepting = false` and never writes the
  file - the UI then says "this sensor does not accept configuration".

## 4. Validation (a bad value can never be applied)

### 4.1 Rules (one table, two implementations, one shared fixture)

| Rule | Why |
|---|---|
| plan in `all_89`, `ht20_37`, `locked`, `custom`; `custom`: 1-200 entries matching `^\d{1,3}(HT40[+-]\|VHT80\|VHT160)?$`, no duplicates | today's `plan_channels` |
| every entry is in the sensor's **supported** set (4.2) | the adapter cannot tune what it does not list; the driver's list already follows the regulatory domain |
| none of the **known-invalid** entries: `165HT40-`, `140HT40-` (no 802.11 40 MHz partner; `165HT40-` caused the rtw88 WARN storm, findings 7) - even if Kismet lists them | `install_sensor.sh` |
| **stride rule**: gcd(N, Kismet's stride for N) = 1 (`hop_stride` / `hop_stride_ok`); a prime N is always safe | the 2026-09-26 half-coverage incident |
| `dwell_s` in {0.2, 0.5, 1.0}, `poll_s` in {15, 30, 60} | today's bounds; a full cycle (N x dwell) is shown in the UI (89 x 1.0 s = 89 s) |
| on the Pi only: the configuration differs from the running one; the source and the adapter are present (`wlan1`, Kismet datasource running) | nothing to do / nothing to verify against |

The server's rules stay in `app/v2/writes.py`; the Pi's in a new
stdlib module `wifi-sensor/sensor/capture_config.py`. A JSON fixture
(`wifi-sensor/sensor/tests/capture_config_cases.json`: configuration,
capabilities, expected verdict and reason) is run by both test suites, so
the two implementations cannot drift (the way `tests/test_writes.py`
already keeps `ALL_89` equal to the installer's list).

### 4.2 Capabilities: reported by the sensor, not assumed

The heartbeat's `config_state.capabilities`, refreshed hourly and after
every apply: Kismet's datasource channel list for the capture source
(`kismet.datasource.channels` - what the driver offers in the current
regulatory domain), the regulatory country (`iw reg get`; SK today), the
driver and `usb_id` (already in the heartbeat's adapter block), the live
hop list and rate. The server stores the latest in `sensor_status` and
validates every save against it; with no capabilities yet (old uploader)
only the static rules apply and the UI says so. A changed adapter
(`hardware_change`, live-upload 8.4) invalidates pending configurations
that are no longer supported - the server marks them `refused` (reason
"not supported by the adapter now") instead of delivering them.

### 4.3 Shape checks at the boundaries

- Server, ingest: `config_state` validated like the rest of the heartbeat
  (types, lengths, version an integer, hash 64 hex, reason ≤ 500 chars).
- Uploader: `config` in a response must have exactly `version`, `sha256`,
  `desired`; `desired` must hash to `sha256`; size ≤ 8 KB. Else ignored and
  logged once.
- Applier: reads `desired.json` as untrusted input (owned by the
  unprivileged user): ≤ 8 KB, strict schema, hash check, then the full rule
  table against the live capabilities. Never `eval`, never a shell built
  from its values: the channel list is written by a function that only
  joins validated tokens.

## 5. Safe apply on the Pi

### 5.1 Components

- `wifi-sensor/sensor/config_apply.py` (stdlib), run as **root** by
  `wifi-sensor-config-apply.service` (oneshot), started by
  `wifi-sensor-config-apply.path` (`PathChanged=` the uploader's
  `config/desired.json`). No network access (`IPAddressDeny=any`,
  `RestrictAddressFamilies=AF_UNIX`, except loopback for Kismet's REST:
  `IPAddressAllow=localhost`). `ReadWritePaths=` only `/etc/kismet/kismet_site.conf`'s
  directory, `/etc/wifi-sensor`, its own state dir
  `/var/lib/wifi-sensor-config/`. Installed by `install_sensor.sh`
  (developer, C2).
- State dir: `last-good/` (the two files + their version and hash),
  `applied.json` (version, sha256, effective poll id/ts), `attempt.json`
  (in progress: version, phase, started), `result.json` (the last
  outcome), `refused/<sha256>` markers.
- The uploader reads `result.json` / `applied.json` for `config_state`
  (read-only for it; `0644`).

### 5.2 The sequence

```
0  lock (flock on the state dir); read + validate desired.json (4.3, 4.1)
     invalid ............................................ result rejected (reason); stop
1  attempt.json {version, phase: "prepare"}; copy kismet_site.conf, collector.conf -> last-good/
     (only if the running configuration is the last applied one - never overwrite last-good
      with a configuration that was never verified)
2  write the new kismet_site.conf / collector.conf: temp file in the same dir, fsync, rename;
     the source= line is rebuilt from validated tokens, every other line kept byte-for-byte
3  apply: hop list + rate live via set_channel.cmd (C1); poll_s changed -> systemctl restart
     wifi-sensor-collector; phase "verify", t_apply = now (mono and wall)
4  verify for VERIFY_S (default 180 s, C4), checking every 10 s:
     - Kismet's live hop list = the desired list as a multiset, rate = 1/dwell
     - visited entries = N (Kismet's hop_shuffle_skip / the stride rule on the live N)
     - datasource running, num_packets rising
     - in the buffer (read-only): >= 3 collecting polls after t_apply with ds_hop_ok = 1,
       ds_hop_n = N, and poll spacing = poll_s ± 2 s
5  all true ......................................... applied.json {version, sha256, first poll id};
                                                       result applied; remove attempt.json
   any false at the deadline (or Kismet/collector down) -> rollback:
6  restore last-good/ files (atomic), apply them the same way, verify the same way (shorter: 120 s)
     ok ............................................. result rolled_back {reason: the failed check
                                                       and its values, window t_apply .. t_restored}
     not ok ......................................... result failed {reason}; leave the restored
                                                       files in place; the capture watchdog's normal
                                                       escalation takes over (it restarts Kismet /
                                                       the adapter, which then reads last-good)
```

- **The hop guard and the order of writes**: the guard compares Kismet's
  live list with `kismet_site.conf` every 10 s and re-applies the file's
  list on a mismatch. Writing the file *before* the live apply (step 2
  before 3), and restoring it before re-applying (step 6), means the guard
  always pushes towards the configuration being applied, never against
  it; and after a later capture-helper crash it restores the new list (the
  re-open bug, `wifi-sensor/CLAUDE.md`).
- **The capture watchdog** acts at strike 2 (~4 min of a frozen packet
  counter); `VERIFY_S` = 180 s stays below it, so a failed apply is rolled
  back by the applier before the watchdog starts restarting things.
- **A reboot or crash mid-apply**: `wifi-sensor-config-recover.service`
  (oneshot, `Before=kismet.service wifi-sensor-collector.service`) finds
  `attempt.json` and restores `last-good/` before Kismet starts, then
  reports `rolled_back` (reason "interrupted by a reboot"). So a boot always
  starts on a verified configuration.
- **No automatic retry**: a refused or rolled-back hash is not applied again
  (`refused/<sha256>`); the operator saves a new version (same content =
  same hash: the UI offers "retry" explicitly, which clears the marker via a
  `retry` flag in the delivered object - C9).
- **Clock**: the applier records poll ids, not wall times; the uploader
  turns them into corrected times on the true time line (live-upload 8.3),
  so an apply during a boot without NTP still gets a correct boundary.

### 5.3 What the operator sees as capture impact

Live apply (C1): no capture gap; the hop list changes between two hops.
A poll-interval change: one collector restart (~2 s; no poll lost, the
interval in between is irregular once). A rollback: the window t_apply ..
t_restored (≤ ~5 min) ran on the rejected configuration - recorded (6).

### 5.4 Not done by the applier

It never touches the network, Tailscale, SSH, chrony, the capture
adapter's USB port or driver, the buffer, the uploader's config or token,
or anything outside the two files' capture options. Interface choice
(`CAPTURE_IFACE`), Kismet's other options and the installer's settings
stay installer-only.

### 5.5 Reporting

`config_state` in every heartbeat: `accepting`, `applied_version`,
`applied_sha256`, `attempt` = the newest `result.json` (`version, sha256,
result: received | applying | applied | rejected | rolled_back | failed,
reason, started_poll, effective_poll, effective_ts (corrected), checks:
{…the verify values…}`), `capabilities`. The server writes each new attempt
once (`sensor_config_attempts`, keyed by sensor, version, sha256,
started_poll - a replayed heartbeat changes nothing).

### 5.6 Refusals on the server

An attempt with result rejected / rolled_back / failed marks that
`sensor_config` row `refused_at` + reason; it is not delivered again unless
retried (C9). A sensor event (`detections` kind `sensor`, type
`config_failed`, high) is opened for `failed` (capture state unknown) and
`rolled_back` (medium), closed when a later version is applied; `rejected`
is an inline message only (nothing changed on the sensor).

## 6. The dataset boundary

- On `applied`: `sensor_config.confirmed_at` = `effective_ts` (the first
  collecting poll under the new configuration, corrected time) and one
  `sensor_annotations` row: kind `change`, `boundary` **true**, label
  "Capture configuration vN: <plan>, N channels, dwell d s, poll p s",
  ref `sensor_config vN` - exactly how the seeded hop-list changes of
  2026-09-26/27 look (findings 7), so the Sensors page's configuration
  history and every boundary-aware reader (RSSI baselines read only
  `hardware_change`; per-channel figures, the RQ3 tool and the dataset
  overview read `change`) treat it alike.
- On `rolled_back`: a `coverage` annotation for the window t_apply ..
  t_restored ("configuration vN tried, rolled back: <reason>"), no
  boundary - the configuration before and after is the same (C5).
- **Per poll** (C6): the collector reads `applied.json` on every poll (one
  small file) and stores `polls.config_version` (buffer v9); the uploader
  sends it, the server keeps it (schema 17). Then a boundary is exact in
  the data itself, and any figure can be split by configuration without
  joining time ranges.
- The findings rule stays: a configuration change is a `findings.md`
  section 7 row - from now on written from the attempt record (the UI's
  configuration history has everything the row needs).

## 7. Authorization

- Only the existing write path creates a desired configuration: `POST`
  Settings -> Sensors with the `WRITE_TOKEN`, in `deps.write_tx()` (an
  `audit_log` row `sensor.config` with old and new, `write_version + 1`).
  No new write endpoint; "retry" is the same write with `retry: true`.
- The ingest token can only *report* (its own sensor's `config_state`); it
  can never create or change a desired configuration. The delivery reads
  the newest row of the token's own sensor only.
- The Pi accepts a configuration only from the uploader's verified-TLS
  response (live-upload amendment 2) and only for its own token; there is
  no local API, no other input path.
- Optional, not proposed: signing the delivered object (an HMAC key per
  sensor in `uploader.conf`). It would only add protection against someone
  writing `sensor_config` rows directly in the database - which already
  means full control of the server (C11).

## 8. UI

Settings -> Sensors (the configuration column) and the Sensors page card,
replacing today's "Saved, not delivered":

| State | Shown |
|---|---|
| applied = desired | **Applied vN** since <effective time>; the configuration |
| desired newer, sensor not accepting | **Pending vN** - "this sensor does not accept configuration (CONFIG_APPLY=0)" |
| desired newer, not yet received | **Pending vN** - "waiting for the sensor (last contact <time>)" |
| received / applying | **Applying vN** - "verifying capture, up to 3 min" |
| rejected | **Rejected by the sensor**: <reason>; still running vM |
| rolled_back | **Rolled back**: <reason>; running vM again; [Retry] |
| failed | **Failed**: <reason> - capture state unknown (sensor event) |

Plus: "Desired / Applied / Pending" as three rows of the card; the attempts
log (version, time, result, reason, verify values) under the configuration
history; the save dialog validates against the sensor's reported
capabilities and shows the full hop cycle (N x dwell) and the stride check.
Tests: the `settings.js` / `sensors.js` state functions (the existing
`configState` tests extended to every state above).

## 9. Schema 17 / buffer v9 (sketch)

```sql
ALTER TABLE sensor_config ADD COLUMN sha256 bytea,          -- canonical JSON hash
  ADD COLUMN delivered_at timestamptz,                      -- the sensor echoed it
  ADD COLUMN refused_at timestamptz, ADD COLUMN refused_reason text;
-- confirmed_at stays: = effective_ts of the applied attempt
CREATE TABLE sensor_config_attempts (
  sensor_id integer NOT NULL REFERENCES sensors(id), version integer NOT NULL,
  sha256 bytea NOT NULL, result text NOT NULL
    CHECK (result IN ('received','applying','applied','rejected','rolled_back','failed')),
  reason text, started_poll bigint, effective_poll bigint, effective_ts timestamptz,
  restored_ts timestamptz, checks jsonb NOT NULL DEFAULT '{}', reported_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sensor_id, version, sha256, result));
ALTER TABLE sensor_status ADD COLUMN capabilities jsonb, ADD COLUMN config_state jsonb;
ALTER TABLE polls ADD COLUMN config_version integer;        -- NULL before buffer v9
-- stage_polls gains config_version; ingest_merge() copies it
```

Buffer v9: `polls.config_version INTEGER` (metadata-only `ADD COLUMN`,
rollback `meta.schema_version` 8). Migration and rollback as
`ops/migrate_schema16.*`; a `check_schema17.sql`; fixture tests below.

## 10. Decisions (the developer's)

| # | Question | Recommendation |
|---|---|---|
| C1 | Apply the hop list and rate **live** through Kismet's `set_channel.cmd` (no capture gap; the path the hop guard has used since 2026-09-27) after writing `kismet_site.conf`, or restart Kismet (30-60 s gap, the boot path) | live; the file is what every restart reads, so both paths end on the same list |
| C2 | Privilege: a root applier triggered by a `.path` unit, the uploader unprivileged - or a sudoers rule for the uploader | the root applier (the network-facing process never gets root) |
| C3 | A poll-interval change restarts the collector | yes (no capture impact) |
| C4 | Verify window and criteria: 180 s, the checks of 5.2 step 4 | yes; below the watchdog's 4 min |
| C5 | A rolled-back attempt's window recorded as a `coverage` annotation | yes |
| C6 | `polls.config_version` per poll (buffer v9, schema 17) | yes - exact boundaries in the data |
| C7 | Capabilities from the sensor (Kismet's datasource list + `iw reg get`) in the heartbeat; the server validates every save against them | yes |
| C8 | Keep the dwell / poll bounds (0.2/0.5/1.0 s; 15/30/60 s) and the plans as they are | yes - no new options in this phase |
| C9 | No automatic retry; an explicit Retry in the UI | yes |
| C10 | `locked` = a one-entry hop list (hopping on one channel = no hopping; the hop guard keeps it) rather than Kismet's `channel_hop=false` | one-entry list (one code path) |
| C11 | No signature on the delivered object (TLS-verified pull, write-token-only creation) | no signature |
| C12 | `CONFIG_APPLY` defaults to 0 until the staged test (12) passed on the Pi | yes |
| C13 | The `findings.md` section 7 row for each applied change: written by Claude from the attempt record, as now | yes |

## 11. Failure modes

| # | Failure | Effect | Handling |
|---|---|---|---|
| K1 | Invalid value saved | – | refused at save (server rules + capabilities); never delivered |
| K2 | Valid on the server, not for the live adapter | – | the Pi's validation: `rejected`, nothing changed |
| K3 | Applied, capture does not resume / hop list wrong | ≤ 3 min on the new config | rollback, `rolled_back` + coverage annotation |
| K4 | Rollback also fails | capture state unknown | `failed`, sensor event; the watchdog escalates; last-good files in place |
| K5 | Reboot / power cut mid-apply | – | recover unit restores last-good before Kismet starts |
| K6 | Response lost / heartbeat replayed | – | idempotent: same hash, attempts keyed; the file write is atomic |
| K7 | Upload outage while pending | delivered later | the desired version waits on the server; nothing on the Pi changes |
| K8 | Desired file tampered by the unprivileged user | – | the applier re-validates everything (it is untrusted input) |
| K9 | Two saves in quick succession | – | the newest wins; an apply in progress finishes (flock) and the next one starts from its result |
| K10 | Adapter replaced | pending config may be unsupported | re-validated against the new capabilities, refused if needed (4.2) |
| K11 | Hop guard re-applies the old list | – | prevented by writing the file before the live apply |
| K12 | Clock unsynced during the apply | – | poll ids, corrected by the uploader |

## 12. Tests

- **Shared fixture** (4.1): every rule, valid and invalid, run by the
  server's `api/tests/test_writes.py` and the Pi's
  `sensor/tests/test_capture_config.py`.
- **Server** (stdlib + SQL fixtures): delivery only when versions differ and
  not refused; `config_state` validation; attempts idempotent under replay;
  `confirmed_at`, the boundary annotation and the coverage annotation from
  the attempts (`tests/sql/config_delivery_test.sql`); a refused hash not
  re-delivered; `config_failed` events open/close; capabilities change
  refuses an unsupported pending config; v2 endpoints' new states;
  `v2_smoke.py` (no write without the token; ingest cannot create config).
- **Uploader**: response parsing (bad hash, oversized, extra keys ignored),
  atomic `desired.json`, `CONFIG_APPLY=0` never writes, `config_state` from
  `result.json`.
- **Applier** (`sensor/tests/test_config_apply.py`, a fake Kismet REST, temp
  `/etc` tree, fake `systemctl`, a fixture buffer written by the real
  `store.py`): success; each verify check failing -> rollback; rollback
  failing -> failed; the hop guard's file comparison during the sequence;
  interrupted apply -> recover; untrusted file cases; no re-apply of a
  refused hash; the `source=` line rebuilt with every other line unchanged.
- **End to end on the Mac**: `fake_server.py` delivering a configuration,
  uploader + applier against the fakes, the result back in the heartbeat.
- **On the Pi (the developer's, staged)**: with `CONFIG_APPLY=1`, `all_89 ->
  ht20_37 -> all_89` (dwell 0.2), checking `dwell_poll.py` coverage after
  each, and one deliberately failing case (a fake capability mismatch via a
  test flag) to see a real rollback; a `findings.md` section 7 row each.

## 13. Effort and build order

| Step | Content | Estimate |
|---|---|---|
| 1 | Shared fixture + `sensor/capture_config.py` + server validation against capabilities | 0.5 d |
| 2 | Schema 17 (migration, rollback, check, fixture tests) | 0.5 d |
| 3 | Server: delivery in the responses, `config_state` ingest, attempts, annotations, events | 1 d |
| 4 | Pi: `config_apply.py`, recover, units, installer changes, tests | 2 d |
| 5 | Uploader 1.2: deliver/report, `CONFIG_APPLY`; collector buffer v9 (`config_version`) | 0.5 d |
| 6 | UI: states, attempts log, save-time capability check | 1 d |
| 7 | Docs, both `CLAUDE.md`, the staged test on the Pi, findings rows | 0.5 d |
| | **Total** | **~6 days** |

Each step is deployable alone: 1-3 change nothing for a sensor that does
not report `config_state`; 4-5 do nothing with `CONFIG_APPLY=0`; the UI
shows the new states once reports exist. Rollback of the whole feature:
`CONFIG_APPLY=0` on the Pi (the applier never runs), schema 17's
`rollback_schema17.sql`.

## 14. As built (2026-10-25)

| Part | Where |
|---|---|
| Rules, canonical JSON, sha256 | `wifi-sensor/sensor/capture_config.py`; server `api/app/v2/writes.py` `check_capture_config()`, `config_canonical()`; one fixture `wifi-sensor/sensor/tests/capture_config_cases.json` run by both (`sensor/tests/test_capture_config.py`, `api/tests/test_config_delivery.py`) |
| Schema 18 | `ops/migrate_schema18.sh` / `.sql`, `rollback_schema18.sql`; `tests/test_schema_files.py` Schema18 |
| Ingest | `api/app/ingest/validate.py` (`config_state`, `capabilities` of the heartbeat), `config.py` (delivery, attempt actions), `routes.py` `_config_report()` / `_config_out()`, `sql/ingest.sql` (the `config_*` statements); `api/tests/test_ingest_config.py` |
| Save | `POST /api/v2/settings/sensors`: checked against the sensor's capabilities, stored with its sha256 |
| Pi | collector buffer v9 (`polls.config_version`), uploader 1.2 (`uploader/configsync.py`), the applier `sensor/config_apply.py` + units `systemd/wifi-sensor-config-*`, `install_config_apply.sh`; tests `collector/tests/test_store_v9.py`, `uploader/tests/test_configsync.py`, `sensor/tests/test_config_apply.py` |
| UI | `frontend/src/pages/settings/settings.js` `configState()`, `sensors/sensors.js` `footText()`, the configuration history shows rolled-back windows |

Deviations from the design text above:

- `sensor_config.sha256` is hex `text` (with a CHECK), not `bytea`; the
  dwell is rounded to 3 decimals in the canonical JSON (a `real` column
  reads 0.2 back as 0.20000000298).
- `config_state` is stored with the rest of the heartbeat
  (`sensor_status.heartbeat`); only the capabilities got their own columns.
- The UI shows each pending version's state, the reason of a refusal and
  the rolled-back windows in the configuration history; the
  `sensor_config_attempts` rows are not listed one by one (the settings
  response carries the newest attempt).
- The applier's verification counts 3 consecutive collecting polls with
  `ds_hop_n` = N and `ds_hop_ok` (the collector compares the live list with
  the new file), spaced `poll_s` ± 2 s, rising `ds_packets`, plus Kismet's
  live list and rate.

**Before enabling (`CONFIG_APPLY=1`) - open points for the developer:**

1. **`install_sensor.sh` and a delivered configuration**: the installer
   rewrites `kismet_site.conf` from its own `CAPTURE_CHANNELS`. After a
   delivered configuration is applied, a later `install_sensor.sh` run would
   silently replace it (the applier's `applied.json` would then name a
   configuration that no longer runs). Options: the installer reads
   `/var/lib/wifi-sensor-config/applied.json` and keeps its list, or warns
   and refuses while a delivered configuration is applied. Not built (an
   `install_sensor.sh` change is the developer's).
2. The staged test of section 12 (`all_89 -> ht20_37 -> all_89`, one forced
   rollback), with a `findings.md` section 7 row each.

**Deploy record (2026-10-25, UTC):** backup `wifi_audit-20261025T090024Z.dump`
copied to the Mac, sha256 equal; `ops/migrate_schema18.sh` 09:09:23-09:09:37,
all 11 fixture tests PASS (incl. `events` with the cause through the merge,
`labels`); api + refresher 09:10, frontend (14/14 test files) 09:11; smoke
0 failures (one transient 304 miss in the first run: a refresh between the
two requests); collector v9 09:14:37, uploader 1.2 09:15:41, applier units
09:17:00 on the Pi - Kismet and the collector not restarted by the applier.
On the server: heartbeat `config_state.accepting` false, capabilities 91
settings / SK, 0 attempts; polls contiguous (0 seq gaps), latency p50 2.7 s.
