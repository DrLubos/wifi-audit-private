# wifi-audit server (placeholder)

Planned backend of the passive Wi-Fi monitoring system: it will receive
store-and-forward uploads from one or more `wifi-sensor` deployments, keep the
long-term history that the sensors' 14-day buffers discard, and host the
analysis and any dashboard.

**Not started.** The sensor side is collecting data first; the server is built
only after the thesis contribution has been chosen from that data. See the
scope rules in the root `CLAUDE.md` and in `wifi-sensor/CLAUDE.md`.

What the sensor already produces for it: a SQLite buffer whose tables carry a
`sent` column (default 0) reserved for the upload step
(`wifi-sensor/collector/README.md`, "Buffer schema").
