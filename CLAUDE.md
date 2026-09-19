# CLAUDE.md — wifi-audit monorepo

Master's thesis project: a passive Wi-Fi monitoring system. This repository holds
both halves; each half has its own `CLAUDE.md` with the full context and rules.
Read the one for the folder you are working in.

## Layout

- `wifi-sensor/` — the **Raspberry Pi sensor**: Kismet provisioning
  (`install_sensor.sh`), the collector daemon (`collector/`), systemd units,
  read-only analysis scripts and the findings so far. **Read
  `wifi-sensor/CLAUDE.md` before touching anything in it.** Every relative path
  and command in that file is relative to `wifi-sensor/`; on the Pi the
  installers are run from that folder.
- `server/` — the **backend** that will receive store-and-forward uploads from
  sensors and store/analyse them. **Placeholder only** — nothing is built until
  the thesis contribution has been chosen from the accumulated data (see the
  scope rules in `wifi-sensor/CLAUDE.md`). Do not create server code unless
  explicitly asked.

## Shared conventions

- English only for identifiers, comments, commit messages, filenames.
- Never commit credentials (`.env`, `sensor.conf`, `collector.conf`, Kismet
  httpd config are gitignored) and never collect bystander PII on the sensor.
- One git repository, one history: commit sensor and server changes as
  separate, reviewable commits; do not split into submodules.
- Claude Code runs on the developer's Mac. Read-only SSH exploration of the Pi
  (`ssh pi`) is allowed; installers are run by the developer, never by Claude.
