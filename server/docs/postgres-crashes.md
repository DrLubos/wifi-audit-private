# Postgres crash-restarts (investigation)

Status: **cause found 2026-11-14** (very likely; traced on the crash of
that day, not reproduced in isolation) - the db healthcheck's `pg_isready`,
orphaned when its probe is killed at the timeout, is adopted by the postmaster
(PID 1) and its exit status 2 is taken for a crashed server process. The fix
is in `docker-compose.yml` (`init: true`, `exec pg_isready -t 3`, timeout
10 s) and is **not deployed**: it recreates the db container (a short
database stop, and the container's log is deleted) - the developer's OK.

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
| 2026-11-14 12:41:45 | snapd refresh check 12:40:30; swap-out 135 pages/s, direct reclaim 100 pages/s in the 12:40-12:50 sample |

## What it is not

- **Not the OOM killer:** a SIGKILL shows as "terminated by signal 9"; kern.log
  has no OOM line, `docker inspect` OOMKilled=false, `systemd-oomd` inactive.
  `vm.overcommit_memory = 1`, so allocations do not fail either.
- **Not /dev/shm:** 64 MB, 2 % used (a DSM failure is an ERROR with a message).
- **Not a signal-terminated crash** (segfault, abort): those are logged as
  "terminated by signal n".
- ~~Not the healthcheck itself~~ - this was wrong, see "The cause" below: a
  killed probe cannot take a backend down, but its orphaned `pg_isready` can
  look like one to the postmaster.

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

## The cause (traced 2026-11-14)

The crash of 2026-11-14 12:41:45 UTC is the first with the diagnostic
logging on. The crashed PID 699556 has **no "connection received" line** -
it was never a backend - and no autovacuum or other server line either.
What the log, the PID sequence and dockerd's journal show:

| Time (UTC) | Event |
|---|---|
| 12:40:30 | snapd refresh check; the box starts swapping |
| 12:40:49.6 - 51.9 | health probe: its connection takes 2.5 s (normally 3 ms) |
| 12:40:58 | dockerd: "Could not send KILL signal to container process ... process already finished" for the db container - that probe hit the 5 s timeout |
| ~12:41:28 | the next probe starts (PIDs 699548 ...); its `pg_isready` is PID 699556 |
| ~12:41:33 | the probe's 5 s timeout: dockerd kills the probe's shell |
| 12:41:40.153 | an api connection gets PID 699557 |
| 12:41:42.256 | `pg_isready`'s connection is received by backend 699558 (authorized 42.466) |
| 12:41:45.710 | `server process (PID 699556) exited with exit code 2` - 3 s after it connected: pg_isready's own timeout, exit status 2 = "no response" |
| 12:41:46.163 | backend 699558: "could not send data to client: Broken pipe" |

The mechanism (steps 1 and 2 checked on the box; 3 and 4 are PostgreSQL 17's
behaviour as read from its source - `sendAuthRequest()` in auth.c,
`CleanupBackend()` in postmaster.c - and are what the reproduction below
confirms or refutes):

1. The healthcheck is `CMD-SHELL pg_isready ...`, i.e. `/bin/sh -c`. dash
   (0.5.12 in the postgres:17 image) **forks** for it: `docker compose exec
   -T db sh -c "cat /proc/self/stat"` shows the command with the shell as
   its parent. So `pg_isready` is a child of the probe's `sh`.
2. When a probe exceeds its timeout, dockerd kills the exec's main process -
   the `sh`. `pg_isready` is orphaned, and an orphan in a container goes to
   the PID namespace's init: **postgres itself is PID 1** (`HostConfig.Init`
   unset), so the postmaster adopts it.
3. `pg_isready` waits 3 s for an answer. A backend answers only after its
   start-up (the AuthenticationOk is not flushed before ReadyForQuery), which
   took longer than that while the box was swapping: exit status 2.
4. PostgreSQL 17's postmaster reaps the unknown child and `CleanupBackend()`
   calls `HandleChildCrash()` for any exit status other than 0 or 1 *before*
   it looks the PID up in its backend list: "server process (PID n) exited
   with exit code 2", every backend is terminated, crash recovery.

That is why nothing is logged by the dying process, why there is never a
"Failed process was running" detail, why it needs memory pressure (a probe
must both time out and then get no answer for 3 s - dockerd logged 86 killed
probes in 30 days across the box's containers, against 5 crashes) and why it is not reproducible with a
build alone. dockerd's journal shows killed health probes around the earlier
crashes too (10-14 06:06:19, 10-16 05:06:16, 10-17 21:02:41; the line is
logged only when the probe had already ended by the time of the KILL, so it
is not a one-to-one record).

**Check without the live database** (any machine with Docker; a throw-away
container):

    docker run -d --name pgtest -e POSTGRES_PASSWORD=x postgres:17
    docker exec -d pgtest sh -c '(sleep 5; exit 2) &'      # an orphan that exits with status 2
    sleep 8; docker logs pgtest 2>&1 | grep -A3 "exit code 2"; docker rm -f pgtest

With `docker run --init` in the first line the orphan goes to tini and
nothing is logged.

## The fix (in `docker-compose.yml`, not deployed)

- `init: true` on the db service: tini is PID 1 and reaps every orphan - also
  those of `docker compose exec db sh -c ...` sessions (psql scripts, the
  backup) that lose their parent.
- The healthcheck is `exec pg_isready -q -t 3 ...` with `timeout: 10s`: the
  shell becomes pg_isready (no child to orphan), and pg_isready gives up by
  itself well inside the timeout.

Deploying it is `docker compose up -d db`: the container is **recreated**
(the database stops for a few seconds - the uploader retries, the refresher
keeps its dirty mark - and the container's log, including the trace above,
is deleted). Note: once this file is on the box, a plain `docker compose up
-d` (all services) recreates db too; `docker compose up -d api` and the like
do not. Afterwards `docker inspect --format '{{.HostConfig.Init}}'
wifi-audit-db-1` is `true` and `docker compose exec -T db cat
/proc/1/comm` is `docker-init`; the diagnostic log flags can stay until a
few memory-pressure episodes have passed without a reset.

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

## Still worth doing

Less memory pressure on the box makes every request faster, whatever the
healthcheck does: the items of "What could be trimmed safely" stay the
developer's call. The pipeline rides through a reset either way: the uploader
retries (0 lost polls in all five), the refresher keeps its dirty mark across
a dead connection (since 2026-10-17).
