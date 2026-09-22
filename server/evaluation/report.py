"""Render the evaluation: Markdown tables, the per-trial CSV, and evaluation.md.

Pure formatting over the structures metrics.evaluate() returns; no database, no
ground-truth I/O. __main__.py fetches the data and calls render_markdown().
"""

import csv
from datetime import datetime

from detection.common import fmt_dur, iso
from . import metrics
from .metrics import channel_hop_penalty, median

CSV_FIELDS = ["trial_id", "type", "variant", "channel_config", "target_bssid",
              "detected", "n_detections", "severity", "rule", "sources_last",
              "sources_counter", "latency_frame_s", "latency_emit_s", "mgmt_ratio",
              "det_id", "pcap_frames", "frames_claimed", "frame_count_ok"]


def md_table(headers, rows):
    """A GitHub-flavoured Markdown table. Cells are stringified; None -> '-'."""
    def cell(v):
        return "-" if v is None else str(v)
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(cell(v) for v in r) + " |")
    return "\n".join(out)


def _pct(x):
    return "-" if x is None else "%.0f%%" % (100 * x)


def _ci(cell):
    lo, hi = cell.wilson
    if lo is None:
        return "-"
    return "%.0f-%.0f%%" % (100 * lo, 100 * hi)


def _counts(items):
    """'high x2, medium x1' from a list, most common first."""
    if not items:
        return "-"
    seen = {}
    for x in items:
        seen[x] = seen.get(x, 0) + 1
    return ", ".join("%s x%d" % (k, n) for k, n in
                     sorted(seen.items(), key=lambda kv: -kv[1]))


def recall_table(cells):
    rows = []
    for (variant, cfg) in sorted(cells):
        c = cells[(variant, cfg)]
        rows.append([variant, cfg, "%d/%d" % (c.detected, c.n), _pct(c.recall), _ci(c),
                     _counts(c.severities), _counts(c.rules),
                     "%d/%d" % (c.sources["last"], c.sources["counter"])])
    return md_table(["variant", "channel", "detected", "recall", "95% CI",
                     "severity", "rule", "burst last/counter"], rows)


def latency_table(cells):
    rows = []
    for (variant, cfg) in sorted(cells):
        c = cells[(variant, cfg)]
        mf, me = median(c.latency_frame), median(c.latency_emit)
        rows.append([variant, cfg, c.detected,
                     _rng(c.latency_frame), "-" if mf is None else "%.1fs" % mf,
                     _rng(c.latency_emit), "-" if me is None else "%.1fs" % me])
    return md_table(["variant", "channel", "n", "frame-anchor range", "frame median",
                     "emit-anchor range", "emit median"], rows)


def _rng(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "-"
    return "%.1f..%.1fs" % (min(xs), max(xs))


def penalty_table(cells):
    rows = []
    for v, (lk, hp, pen) in channel_hop_penalty(cells).items():
        rows.append([v, _pct(lk), _pct(hp), "-" if pen is None else "%+.0f pts" % (100 * pen)])
    return md_table(["variant", "locked recall", "hopping recall", "hop penalty"], rows)


def counterfactual_table(cells_v2, cells_counter):
    """v2 (exact rule) recall vs counter-only recall, per variant, pooled over
    channel configs. The point: variant 'slow' should collapse under counter-only."""
    rows = []
    variants = sorted({v for (v, _c) in cells_v2} | {v for (v, _c) in cells_counter})
    for v in variants:
        d2 = sum(c.detected for (vv, _), c in cells_v2.items() if vv == v)
        n2 = sum(c.n for (vv, _), c in cells_v2.items() if vv == v)
        dc = sum(c.detected for (vv, _), c in cells_counter.items() if vv == v)
        nc = sum(c.n for (vv, _), c in cells_counter.items() if vv == v)
        rows.append([v, "%d/%d" % (d2, n2), _pct(d2 / n2 if n2 else None),
                     "%d/%d" % (dc, nc), _pct(dc / nc if nc else None)])
    return md_table(["variant", "v2 detected", "v2 recall",
                     "counter-only detected", "counter-only recall"], rows)


def fp_table(fps, clean_hours, background_note):
    rate = "-" if not clean_hours else "%.2f/clean-h" % (len(fps) / clean_hours)
    rows = [["staged eval window (non-attack time)", len(fps),
             "%.2f h" % clean_hours if clean_hours else "-", rate]]
    rows.append(["real background (seeded)", background_note, "", ""])
    return md_table(["denominator", "false positives", "clean time", "rate"], rows)


def write_csv(path, per_trial):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in per_trial:
            src = r.get("sources") or {}
            w.writerow({
                "trial_id": r["trial_id"], "type": r["type"], "variant": r["variant"],
                "channel_config": r["channel_config"], "target_bssid": r["target_bssid"] or "",
                "detected": int(r["detected"]), "n_detections": r["n_detections"],
                "severity": r["severity"], "rule": r["rule"],
                "sources_last": src.get("last", ""), "sources_counter": src.get("counter", ""),
                "latency_frame_s": _num(r["latency_frame_s"]), "latency_emit_s": _num(r["latency_emit_s"]),
                "mgmt_ratio": _num(r["mgmt_ratio"]), "det_id": r["det_id"] if r["det_id"] else "",
                "pcap_frames": r["pcap_frames"] if r["pcap_frames"] is not None else "",
                "frames_claimed": r["frames_claimed"] if r["frames_claimed"] is not None else "",
                "frame_count_ok": r["frame_count_ok"]})


def _num(v, digits=2):
    return "" if v is None else round(float(v), digits)


LIMITATIONS = """\
- **Single everything.** One sensor, one location, one adapter/driver
  (RTL8821CU/rtw88), one test AP, one attack tool. These numbers characterise
  *this deployment*, not a population; external validity is limited.
- **Pseudo-replication.** Trials share one environment and are close in time:
  they are repeated measures, not independent samples. The Wilson interval is
  reported but understates that dependence.
- **Small N.** At N per cell in the low tens, one miss moves recall by several
  points; recall is given as x/N with a 95% interval, never as a lone percentage.
- **Two same-day blocks.** Locked and hopping ran as separate blocks; the
  morning/afternoon RF drift between them is an intra-day confound (block design
  was chosen over interleaving to avoid crash-risky mid-session reconfiguration).
- **Channel lock changes the sensor.** Locked runs are not the production config;
  the hopping runs are the realistic recall, the locked runs isolate the detector
  from channel-dwell luck. Both are reported; neither alone is "the" recall.
- **False-positive denominator.** The dedicated clean minutes are too few for a
  stable rate; the honest FP evidence is the multi-week real background, stated
  alongside.
- **Latency granularity.** Emission latency is bounded by the 30 s poll and the
  batch cadence; only the frame-anchor latency is ~1 s. The two are reported
  separately and never conflated.
- **Attacker realism.** Fixed rate profiles from one tool; a real adversary
  varies timing and spoofing. Evasion below the sensitivity floor (constant-size
  bursts > 1 s apart that never move `disconnects_last`) remains possible."""


def render_markdown(meta, cells_v2, cells_counter, per_trial, fps, strays,
                    clean_hours, background_note):
    """Assemble evaluation.md from the computed tables. `meta` carries sensor,
    window, generation time and counts for the header."""
    parts = []
    parts.append("# deauth_flood evaluation against staged attacks\n")
    parts.append("_Generated %s by `python -m evaluation report`. Tables are "
                 "machine-generated; re-run to refresh. Interpretation prose is "
                 "the author's to add._\n" % iso(meta["generated_at"]))
    parts.append("## Method\n")
    parts.append(meta.get("method_note", _DEFAULT_METHOD) + "\n")
    parts.append("## Dataset\n")
    parts.append(md_table(["field", "value"], [
        ["sensor", meta["sensor"]],
        ["window (UTC)", "%s .. %s" % (meta["from"], meta["to"])],
        ["trials (attack / clean)", "%d / %d" % (meta["n_attack"], meta["n_clean"])],
        ["detections in window", meta["n_detections"]],
        ["detector version", meta.get("detector_version", "?")],
    ]) + "\n")
    parts.append("## Recall by variant and channel config\n")
    parts.append(recall_table(cells_v2) + "\n")
    parts.append("## Latency (two anchors — see Limitations)\n")
    parts.append(latency_table(cells_v2) + "\n")
    parts.append("## Channel-hop penalty\n")
    parts.append(penalty_table(cells_v2) + "\n")
    if cells_counter:
        parts.append("## Old vs new: v2 exact rule vs counter-only\n")
        parts.append(counterfactual_table(cells_v2, cells_counter) + "\n")
    parts.append("## False positives\n")
    parts.append(fp_table(fps, clean_hours, background_note) + "\n")
    if strays:
        parts.append("_%d detection(s) on the test AP matched no trial "
                     "(leftover/edge — inspect):_ %s\n"
                     % (len(strays), ", ".join("#%d" % d.id for d in strays)))
    parts.append("## Limitations\n")
    parts.append(LIMITATIONS + "\n")
    parts.append("## Raw data\n")
    parts.append("Per-trial join: `%s`.\n" % meta.get("csv_name", "evaluation_trials.csv"))
    return "\n".join(parts)


_DEFAULT_METHOD = """\
Deauth/disassoc frames were staged against a dedicated, operator-owned test AP on
an otherwise-empty channel, from a second device, in three rate profiles — fast
(> 10 frames/s, trips Kismet's flood alert), moderate (~1/s) and slow (1 every
2-3 s, below every rate rule). Each trial ran >= 3 min, spaced > 5 min. Ground
truth is an independent per-trial pcap taken beside the attack (frame count and
first-frame time), not the attack tool's own clock. The Pi collector and the
seed -> import -> `deauth-flood` pipeline were unchanged; this report joins the
resulting `detections` rows against the ground-truth log."""
