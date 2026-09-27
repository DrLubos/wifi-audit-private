"""Dataset rules shared by the collector and the analysis scripts (one definition).

RSSI floors - censored values, not measurements
------------------------------------------------
The capture adapter (RTL8821CU, driver rtw88_8821cu) reports two clamp values:

  -106 dBm  CCK frames (every 2.4 GHz beacon at 1 Mbps). rtw8821c.c computes
            lna_gain_table[lna] - 2 * vga; with lna_gain_table_1 the smallest
            possible output is -44 - 2 * 31 = -106 (maximum-gain state).
  -120 dBm  OFDM frames: max(PWDB - 110, -120).

A floor means "at or below the floor (or AGC not settled)", not a level:
81,120 of 2.23M readings (3.6 %) were exactly -106 against 31 at -105 and
25 at -107 (docs/findings.md sections 10-11). Kismet uses the first radiotap
dBm_AntSignal field and, with dot11_ap_signal_from_beacon=true, an AP's
sig_last is a beacon, so weak 2.4 GHz APs sit on -106.

Rules:
  - a floor is never used as a signal level (no median, MAD, threshold on it);
  - it is not silently dropped either: it is censoring, so the share of floor
    readings per AP is kept (rssi_floor flag in the buffer, schema v5) and an AP
    with a high floor share is not eligible for RSSI baselines/detection;
  - raw history is never rewritten: buffer rows written before schema v5 still
    carry the raw -106/-120 in rssi, so every reader applies rssi_value().

The values are specific to this adapter/driver. A different capture adapter
needs its floors re-derived (and a dataset boundary in findings section 7).
The server has the same list in server/schema.sql (rssi_valid()); a collector
test keeps the two in sync.
"""

RSSI_FLOOR_DBM = frozenset({-106, -120})


def is_rssi_floor(v):
    """True when V is one of the adapter's floor (clamp) values."""
    return v is not None and int(v) in RSSI_FLOOR_DBM


def rssi_value(v):
    """V as a signal level, or None when V is unknown (None) or a floor."""
    if v is None or int(v) in RSSI_FLOOR_DBM:
        return None
    return int(v)


def sql_rssi(col="rssi"):
    """SQLite expression: COL as a level, NULL for a floor (for buffer readers)."""
    floors = ", ".join(str(v) for v in sorted(RSSI_FLOOR_DBM))
    return "(CASE WHEN %s IN (%s) THEN NULL ELSE %s END)" % (col, floors, col)


def sql_rssi_is_floor(col="rssi", flag_col=None):
    """SQLite expression: 1 when the row is a floor reading - a raw floor value
    in COL (rows before schema v5) or FLAG_COL = 1 (rows from v5 on)."""
    floors = ", ".join(str(v) for v in sorted(RSSI_FLOOR_DBM))
    expr = "%s IN (%s)" % (col, floors)
    if flag_col:
        expr = "(%s OR coalesce(%s, 0) = 1)" % (expr, flag_col)
    return "(CASE WHEN %s THEN 1 ELSE 0 END)" % expr
