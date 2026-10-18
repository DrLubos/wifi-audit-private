# Postgres crash-restarts (investigation)

Status: **open** - diagnostic logging enabled 2026-10-18 (developer's approval
of 2026-10-18; logging only, any other configuration change needs the
developer's OK).

## Symptom

The db container's postmaster logs

    LOG:  server process (PID n) exited with exit code 2
    LOG:  terminating any other active server processes
    LOG:  all server processes terminated; reinitializing
    ...   database system was not properly shut down; automatic recovery in progress
    LOG:  database system is ready to accept connections        (~10-40 s later)

with **nothing logged by the dying process before it** and no "Failed
process was running: ..." detail (the postmaster prints that whenever the
crashed backend had a current query in pg_stat_activity - so it had none:
idle, or still starting up).

| Time (UTC) | Context |
|---|---|
| 2026-10-14 06:05:21 | journald "Under memory pressure, flushing caches" at 06:05:05; unattended-upgrades / PackageKit running |
| 2026-10-16 05:05:30 | snapd refresh check 05:04:56 |
| 2026-10-17 21:04:34 | frontend image build (step 5) |
| 2026-10-17 21:23:53 | api image build + api/refresher recreated 21:23:16-19 |

## What it is not

- **Not the OOM killer:** a SIGKILL shows as "terminated by signal 9"; kern.log
  has no OOM line, `docker inspect` OOMKilled=false, `systemd-oomd` inactive.
  `vm.overcommit_memory = 1`, so allocations do not fail either.
- **Not /dev/shm:** 64 MB, 2 % used (a DSM failure is an ERROR with a message).
- **Not a signal-terminated crash** (segfault, abort): those are logged as
  "terminated by signal n".
- Not the healthcheck itself: `pg_isready` is a client; a killed probe cannot
  take a backend down.

**Memory pressure in every case** (sysstat, 10-min samples of the interval
holding the crash vs the one before):

| Crash | swap in/out per s | pages scanned (kswapd / direct) per s | before |
|---|---|---|---|
| 10-14 06:05 | 121 / 138 | 2156 / 78 | 0.4 / 0, no scanning |
| 10-16 05:05 | 29 / 37 | 738 / 56 | 0 / 0, no scanning |
| 10-17 21:04 and 21:23 | 113 / 188 | 1532 / 46 | 21 / 5 |

Direct reclaim (`pgscand`) - processes stalling to free memory themselves -
appears only in those intervals. No userspace OOM daemon runs on the box
(no systemd-oomd, earlyoom); the image builds of 10-18 (14:10, 14:34) did not
trigger one, so it is not reproducible on demand.

In PostgreSQL 17 a backend `_exit(2)`s in its SIGQUIT handler (`quickdie`),
which the postmaster sends to all children only *after* a crash - here the
first child exits with 2 by itself, so either something else delivered a
SIGQUIT or the process exited with status 2 on another path. The common
factor of all four is memory pressure on the 1 GB box (builds, apt).

## Diagnostic logging (enabled 2026-10-18)

`docker-compose.yml`, db `command`:

    -c log_line_prefix='%m [%p] %q%u@%d %a %r '
    -c log_connections=on -c log_disconnections=on
    -c log_error_verbosity=verbose

Each connection is logged with its PID, user, database, application name and
client address (the api, the refresher - `wifi-audit-refresher` -, the
healthcheck's local socket), so the PID of the next "exited with exit code 2"
can be traced to who opened it, when, and whether it ever got past
authentication. Volume: about 4 MB of container log a day (the healthcheck's
connection every 30 s is most of it; json-file 3 x 10 MB rotation keeps
about a week - recreating the db container deletes it). **Revert:** remove the four log flags and
`docker compose up -d db`.

On the next crash: `docker compose logs db | grep -B50 "exit code 2"`, find
the PID's "connection received" / "connection authorized" lines, and note
`free -m`, `/proc/pressure/memory` and what ran on the box at the time.

## What runs at the crash times

- 10-14 06:05:21: `apt-daily-upgrade` (unattended-upgrades) ran 06:02:54-06:05.
- 10-16 05:05:30: a snapd refresh check at 05:04:56 (snapd refreshes 4 x a day,
  `timer: 00:00~24:00/4`).
- 10-17 21:04 and 21:23: our own image builds (frontend, api).

## Memory on the box (2026-10-18, 955 MB RAM, 4 GB swap, swappiness 60)

Steady state about 300 MB available. Largest resident sets: postgres about
330 MB in all (shared_buffers 128 MB), dockerd 56, TwitchDropsMiner 30 (the
developer's, not the stack), multipathd 27, systemd-journald 23, uvicorn
23, containerd 21, snapd 19, caddy 16, google agents 16. Transient: an
image build (legacy builder - buildx is not installed; the frontend's npm +
vite build is the largest), unattended-upgrades (Python, ~100+ MB while it
runs), snapd refreshes.

## What could be trimmed safely (a list - nothing changed)

| Item | Saves | Risk / note |
|---|---|---|
| **Builds with a memory cap**: `docker build --memory=400m` (the legacy builder honours it) or `NODE_OPTIONS=--max-old-space-size=256` for the frontend build | the largest spike we cause | a build that needs more fails instead of pushing postgres into swap |
| **Builds with the refresher stopped** (`docker compose stop refresher`, build, `up -d`) | ~30 MB + the refresh queries during the build | rollups wait a few minutes; the dirty mark keeps them |
| **Build elsewhere** (GitHub Actions or the Mac, push to a registry, `docker compose pull` on the box) | no build on the box at all | changes the "everything is built on the server" convention (server/CLAUDE.md); a registry and its credentials |
| **unattended-upgrades at a fixed quiet hour** (a `apt-daily-upgrade.timer` drop-in: e.g. `OnCalendar=*-*-* 03:00`, `RandomizedDelaySec=0`) | moves the ~100 MB spike to a known time (not into the evening dorm peak) | security updates still daily |
| **snapd refreshes once a week at a fixed time** (`snap set system refresh.timer=sun,04:00`) | 3 of 4 daily refresh checks | snaps update weekly; installed: core22, core24, snapd, google-cloud-cli |
| **Remove the google-cloud-cli snap** (and with it most of snapd's work) | ~19 MB snapd + the refresh spikes | gcloud on the box is gone - the developer uses the console; Claude never runs gcloud |
| **Disable multipathd** (`systemctl disable --now multipathd.service multipathd.socket`) | 27 MB | none on a single-disk GCE VM (no multipath storage) |
| **TwitchDropsMiner container** | 30 MB | the developer's, outside the stack |
| **vm.swappiness 60 -> 10** | keeps postgres pages in RAM under pressure | a kernel setting; trades page cache for anonymous memory |

None of these is applied; each is the developer's call (system settings).

## Candidate fixes (for the developer's OK once the cause is known)

- If the crashed PID turns out to be a healthcheck connection under memory
  pressure: a longer healthcheck `timeout` / `interval` (compose).
- If it is a client backend (api, refresher, uploads): less memory pressure
  on the box - image builds are the one heavy thing we start ourselves
  (build less often, or with the api/refresher stopped), and the box's own
  snapd / unattended-upgrades runs (the developer's call).
- Either way the pipeline already rides through a reset: the uploader
  retries (0 lost polls in all four), the refresher keeps its dirty mark
  across a dead connection (since 2026-10-17).

