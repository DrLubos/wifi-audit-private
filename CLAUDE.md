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
- `server/` — the **backend**: PostgreSQL + FastAPI + Caddy/React on the GCP
  box, the dashboard, the batch detectors and (being rolled out) the live
  upload from the sensor (`server/docs/live-upload.md`). **Read
  `server/CLAUDE.md` before touching anything in it.**

## Shared conventions

- English only for identifiers, comments, commit messages, filenames.
- Never commit credentials (`.env`, `sensor.conf`, `collector.conf`, Kismet
  httpd config are gitignored) and never collect bystander PII on the sensor.
- One git repository, one history: commit sensor and server changes as
  separate, reviewable commits; do not split into submodules.
- Claude Code runs on the developer's Mac and reaches the Pi (`ssh pi`) and
  the server (`ssh google`). Since 2026-10-17 (live-upload rollout) it may
  commit and push its own commits (never force-push or rewrite history) and
  run the rollout's operations itself - exactly those listed under
  "Operating rules" in `server/CLAUDE.md` and `wifi-sensor/CLAUDE.md`; every
  other change on either machine stays the developer's.
