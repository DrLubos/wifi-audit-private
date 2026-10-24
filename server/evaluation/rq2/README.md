# RQ2 – false positives of the events (read-only)

Counts and rates of the operator's labels per event kind, type and rule, the
label reasons grouped, and the share of events traced to a **known cause**
(RSSI floor, channel hopping, Kismet rule defects such as NOCLIENTMFP,
incomplete allow-lists, this project's own pipeline bugs such as event 866).
The labels are the developer's, set in the UI (Events -> label: status,
reason, note); this tool only reads them.

```
# on the box, from server/
evaluation/rq2/export.sh                                  # every event up to now
evaluation/rq2/export.sh --from 2026-09-15 --to 2026-11-01 --out ~/rq2-exports/thesis
# on the Mac
scp -r google:rq2-exports/<folder> .
# the analysis alone, on any events_raw.csv (Python >= 3.10, standard library)
python3 -m evaluation.rq2 events_raw.csv --out DIR
```

`events.sql` runs as the read-only role `claude_ro` inside the db container
and writes `events_raw.csv` (one row per event, no MAC address; MAC-shaped
text in a note is masked as `<mac>`); `python3 -m evaluation.rq2` computes
the tables from that file only. Tests: `python3 -m unittest discover -s
evaluation/tests` (`test_rq2.py`; Python >= 3.10 like the rest of
`evaluation/`).

## Labels (input)

`detections.status` and `status_reason` (schema 12; the database refuses any
other combination):

| status | reasons | meaning for RQ2 |
|---|---|---|
| `open` | – | not labelled yet |
| `confirmed` | `security_relevant`, `staged_test` | a real security event (or our own test) |
| `benign` | `expected_behaviour`, `accepted_risk` | true as described, but not a security problem |
| `false_positive` | `rule_defect`, `artefact` | not true as described: the rule is wrong, or the data is an artefact |

**Cause tag (convention, needs your OK - see "Decisions" below):** write
`cause:<slug>` anywhere in the label note, e.g. `open network, cause:kismet_rule`.
The first tag counts; a slug outside the vocabulary counts as `other` and is
listed in `summary.csv` (`note_unknown_slugs`). A bulk label with one note
tags every event of the bulk.

## Causes

| slug | cause |
|---|---|
| `rssi_floor` | RSSI floor censoring: the RTL8821CU's -106/-120 dBm sentinel read as a level (`wifi-sensor/docs/findings.md` 10-11) |
| `channel_hopping` | channel hopping / reduced channel coverage: hop list collapsed or halved, off-channel reception (findings 7) |
| `kismet_rule` | a Kismet rule that does not describe a security event, or a defective one (NOCLIENTMFP, findings 12) |
| `allow_list_incomplete` | the network's allow-list (BSSIDs / OUIs) was incomplete at the time |
| `pipeline_bug` | a bug in this project's own pipeline (event 866: a retention-risk event for a backlog the server already held) |
| `sensor_degraded` | the capture was down or delivered no frames at the time (not hopping) |
| `ap_behaviour` | normal AP behaviour (dynamic channel selection, DFS, reboot, a change by its owner) |
| `client_behaviour` | normal client behaviour (roaming, leaving, power save) |
| `staged_test` | this project's own staged test |
| `other` | another cause (name it in the note), or an unknown slug |

An event's `cause` is the note's tag if there is one (`cause_source` =
`note`), else the first matching rule below (`rule`), else empty (`none`).
The rule's answer is kept in `rule_cause` even when a tag overrides it, so
`agreement.csv` shows where the rules and the operator differ.

### Rule-based causes (first match wins)

Each rule is a fact the database holds; none is a guess about intent.

1. **`pipeline_bug`** – an `upload_retention_risk` event whose "oldest
   unsent" poll (and the hour after it) the server had already received
   before the event was raised (`polls.received_at <= event ts`, or a
   snapshot-imported poll). That is event 866 of 2026-10-17 (the first
   `uploader.py --check`); fixed in `847404f`.
2. **`kismet_rule`** – NOCLIENTMFP (findings 12: the rule looks only at the
   client's association request) when the named AP was, at the alert's
   time (`ap_security_periods`), **open** (MFP is impossible there and the
   client sends no RSN element) or offered **no MFP** with a known security
   level (`psk_no_mfp`, or Enterprise without MFP - the AP's own level
   already records it). An AP with unknown security (no beacon record; its
   MFP flags are Kismet's missing-field 0) or one that offered MFP gets no
   rule cause - those are the interesting ones to label.
3. **`allow_list_incomplete`** – `unknown_bssid_managed_ssid` whose BSSID's
   OUI is in the network's allow-list now but was not in the allow-list the
   event recorded (`evidence.allowed_ouis`), or that is classified `managed`
   / `friendly` now.
4. **`rssi_floor`** – an `evil_twin` event whose baseline median is at or
   below -100 dBm (on the censoring floor). No such event is stored today;
   the rule is there for when the detector's events are written.
5. **`channel_hopping`** – an event of kind detection / kismet / config
   that overlaps a period of reduced channel coverage: `sensor_periods`
   'limited' (the hop list degraded, from polls since buffer v4) or a
   `sensor_annotations` 'coverage' window (the halved list and the channel-1
   collapse of 2026-09-26/27, findings 7).
6. **`sensor_degraded`** – the same kinds overlapping a `sensor_periods`
   'outage' or a 'gap' / 'no_frames' annotation.

**Indicators, not causes** (columns of `events.csv`, for the labelling):
`channel_change_1h` - the AP changed its advertised channel within ±1 h of
the event (timing only: campus radios change channel 15-19 times a day, so
±1 h covers ~41 % of their time, findings 10); `reverted_within_s` - for a
security change, the seconds until the AP's next period returned to the
state before the change (a flip and back); `ap_level_at` / `ap_mfp_at` -
the subject AP's security at the event's time. Off-channel reception of a
Kismet alert is not tested: an alert's `channel` is Kismet's, and the AP's
advertised channel at that moment is not a reliable comparison.

## Files and metrics

Rates are over **labelled** events (status other than `open`); an empty cell
means nothing was labelled (never read it as 0). Every rate has a **Wilson
95 % interval** (`_lo`, `_hi`), because most groups are small.

### `by_rule.csv` – one row per (kind, type, rule)

`rule` is the Kismet header for kind `kismet`; `<type>:<rules that fired>`
for the batch detectors (e.g. `deauth_flood:alerts+bursts`);
`security_change:<direction>` for config events (`downgrade_to_open`,
`downgrade`, `upgrade`, `same_level` - an MFP or cipher change inside one
level - or `unknown_level`); the type otherwise. Rows with `rule = *` total a
type with several rules; `*security` (kinds detection, kismet, config),
`*sensor` (kind sensor) and `*all` total everything.

| Column | Meaning |
|---|---|
| `n`, `open`, `labelled` | events; not labelled; labelled = n - open |
| `confirmed`, `benign`, `false_positive` | labelled events per outcome |
| `label_coverage` | labelled / n |
| `confirmed_rate` | confirmed / labelled - the **precision** of the rule as a security alarm |
| `benign_rate` | benign / labelled - true, but not a security problem |
| `fp_rate` | false_positive / labelled - **the false-positive rate** in the strict sense |
| `not_security_rate` | (benign + false_positive) / labelled = 1 - precision: the alarm load that is not a security event |
| `traced`, `traced_rule`, `traced_note` | events with a known cause; from a rule; from a note tag |
| `traced_share` | traced / n |
| `not_security_traced_share` | of the benign + false-positive events, the share with a known cause - **how much of the noise is explained** |

### `by_reason.csv` – the reasons grouped

One row per (kind, type, status, reason), plus `*all`: `n`, `share_of_type`
(of the type's events), `share_of_status` (of the type's events in that
status). `open` rows have an empty reason.

### `by_cause.csv` – the causes

One row per (scope, kind, type, cause, source) with `n`, `share_of_type`,
the status counts of those events and the cause's description; `*` rows
total a scope and `*all` everything. An empty cause with source `none` =
not traced.

### `agreement.csv`

For events with a cause tag: `rule_cause` x `note_cause` -> `n`, `agree`.
Shows whether the rules match the operator's reading (a rule that disagrees
often should be fixed or dropped).

### `summary.csv`

Headline figures: events, security vs sensor events, labelled, label
coverage, outcome counts, `fp_rate`, `events_traced` / `traced_share` (by
note, by rule), `security_traced_share`, `not_security_traced_share`,
`fp_traced_share`, unknown slugs, and the count per cause.

### `events.csv`

The export's columns plus `scope`, `cause`, `cause_source`, `rule_cause`,
`rule_detail`, `note_cause`, `note_unknown_slug` - one row per event, for
checking any figure by hand.

## Reading the numbers (for the thesis)

- "False positive" in RQ2 should be reported both ways: strict
  (`fp_rate`: not true as described) and as the share of alarms that are
  not security events (`not_security_rate`, benign included). NOCLIENTMFP
  is the reason the two differ: it is true for every alert (the request had
  no MFPC) but describes no attack (findings 12).
- `traced_share` with no labels is the share the rules alone explain; once
  labelled, `not_security_traced_share` is the figure to quote.
- Sensor events (outages, upload stalls) describe the sensor, not the air;
  keep them out of the detection precision (`*security` rows), report them
  separately (`*sensor`).

## Decisions (developer)

- **Cause tag in the note** (`cause:<slug>`), or a structured cause field
  (a column + a select in the label dialog - a schema and UI change, not
  built). The tag works today with no change; the field would make typos
  impossible.
- **The vocabulary** above - add or merge causes before labelling starts.
