# Dataset overview (read-only)

One script for the thesis's dataset description: the collection period,
polls, coverage and outages, the configuration history (dataset
boundaries), APs and networks per band and security level, events per kind,
Kismet alerts per header - as CSV, plus `dataset.md` with the same tables.

```
# on the box, from server/
evaluation/dataset/export.sh                                       # all data up to now
evaluation/dataset/export.sh --from 2026-09-15 --to 2026-11-01 --out ~/dataset-exports/thesis
# on the Mac
scp -r google:dataset-exports/<folder> .
```

The SQL files run as the read-only role `claude_ro` inside the db container
(a few seconds in all); `python3 -m evaluation.dataset DIR` renders
`dataset.md` from the CSVs (standard library; tests in
`evaluation/tests/test_dataset.py`).

## Files and definitions

| File | Content |
|---|---|
| `period.csv` | metric, value, unit, note - the headline figures below |
| `outages.csv` | every `sensor_periods` outage / limited period overlapping the range, with the cause recorded in `sensor_annotations` |
| `boundaries.csv` | the configuration history: `sensor_annotations` that are dataset boundaries or dated changes (`change`, `hardware_change`, `coverage`) |
| `aps.csv` | APs and networks per band x security level, with totals (`*`) |
| `events.csv` | events per kind, type and severity, with the label status; a `*` type row totals a kind |
| `alerts.csv` | Kismet alerts per header, whether a header becomes an event (`kismet_alert_rules`) |
| `dataset.md` | all of it as Markdown tables |

`period.csv`:

| Metric | Definition |
|---|---|
| `first_poll`, `last_poll`, `span_days` | the polls' span inside the range |
| `polls`, `polls_expected` | stored polls; span / 30 s + 1 |
| `polls_ok` | the collector reached Kismet |
| `polls_collecting` | **ok, datasource running and devices > 0** - the project's definition of a collecting poll (schema 9) |
| `polls_limited` | collecting, but the hop list was degraded (`ds_hop_ok` false; recorded from buffer v4, 2026-09-27) |
| `polls_live`, `polls_snapshot` | uploaded by the live ingest (from 2026-10-17 20:46) vs loaded by snapshot imports |
| `polls_clock_corrected` | polls the uploader placed on the true time line (`ts_offset_s`) |
| `outages`, `outage_days` | `sensor_periods` outages (a poll gap > 90 s or no collecting poll), clipped to the span |
| `limited_periods`, `limited_hours` | `sensor_periods` limited capture |
| `coverage` | 1 - outage time / span |
| `collecting_hours` | UTC hours with at least one collecting poll |
| `seq_lost`, `seq_unverified` | polls that never reached the server (`sensor_seq_gaps`; recorded since the live upload) |
| `ap_observation_rows` | AP observation rows on the server (the server keeps AP rows only; other device types stay on the Pi) |
| `config_history_rows`, `config_changes` | raw `device_config_history` rows (incl. the pre-v5 churn) vs the changes of `ap_config_changes` (findings 11) |
| `security_changes` | new `ap_security_periods` in the range |
| `alerts` | Kismet alerts, incl. the capture-source ones that are not events |
| `ap_load_hours` | `ap_load_hourly` rows |
| `adapter` | the capture adapter on record (`sensor_adapters`) |

**APs** are those with an observation in the range (`ap_summary.first_obs` /
`last_obs`); **band** and **security level** are the AP's *current*
configuration (`account_aps`: `band_of_channel(adv_channel)`,
`security_level()` - open / legacy / psk_no_mfp / strong / unknown); an AP
that changed its security during the range is counted at its latest level
(the changes are `security_changes`). A **network** is a distinct SSID, or the
name a hidden AP revealed (`network_name`); hidden APs that never revealed a
name count as APs, not networks (`hidden_unnamed_aps`). `mobile_aps` and the
classifications are `account_aps`' (the account's rules plus manual
classifications as of the export).

Client devices, probe requests and associations are not on the server since
the live upload (D4: they stay on the Pi for its 14 days), so the overview
has no client figures; the snapshot-era client rows on the server are not
counted.
