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
authentication. Volume: about 1-2 MB of container log a day (json-file,
3 x 10 MB rotation). **Revert:** remove the four log flags and
`docker compose up -d db`.

On the next crash: `docker compose logs db | grep -B50 "exit code 2"`, find
the PID's "connection received" / "connection authorized" lines, and note
`free -m`, `/proc/pressure/memory` and what ran on the box at the time.
