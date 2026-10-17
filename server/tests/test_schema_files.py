"""Static checks of schema.sql and the schema-7/8/9/10/11/12/13/14 migrations (standard
library, no database):

  cd server && python3 -m unittest discover -s tests -p 'test_*.py'

- the functions and tables the migrations create are the ones schema.sql
  defines (schema 7: the band functions; schema 8: ap_load_hourly,
  ap_channel_at(), load_ap_load_samples(); schema 9: the new tables;
  schema 10: devices.ever_ap, its index and trigger, nothing reads the
  device type for the AP set any more; schema 11: the status reason and its
  constraint, the capture hardware tables, hardware-bounded baselines;
  schema 14: the live-upload tables, the polls / sensor_uploads columns,
  ingest_stage_create() / ingest_merge() / refresh_contact_periods(), and the
  importer's staging columns are the ones ingest_stage_create() creates);
- schema.sql is the complete definition, without upgrade-only statements
  (ADD COLUMN IF NOT EXISTS, DROP INDEX IF EXISTS, view-to-matview DO block);
- the newest migration sets the version schema.sql declares.
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = open(os.path.join(HERE, "..", "schema.sql"), encoding="utf-8").read()
MIGRATION = open(os.path.join(HERE, "..", "ops", "migrate_schema7.sql"), encoding="utf-8").read()
MIGRATION8 = open(os.path.join(HERE, "..", "ops", "migrate_schema8.sql"), encoding="utf-8").read()
MIGRATION9 = open(os.path.join(HERE, "..", "ops", "migrate_schema9.sql"), encoding="utf-8").read()
MIGRATION10 = open(os.path.join(HERE, "..", "ops", "migrate_schema10.sql"), encoding="utf-8").read()
MIGRATION11 = open(os.path.join(HERE, "..", "ops", "migrate_schema11.sql"), encoding="utf-8").read()
MIGRATION12 = open(os.path.join(HERE, "..", "ops", "migrate_schema12.sql"), encoding="utf-8").read()
MIGRATION13 = open(os.path.join(HERE, "..", "ops", "migrate_schema13.sql"), encoding="utf-8").read()
MIGRATION14 = open(os.path.join(HERE, "..", "ops", "migrate_schema14.sql"), encoding="utf-8").read()
ROLLBACK14 = open(os.path.join(HERE, "..", "ops", "rollback_schema14.sql"), encoding="utf-8").read()
MIGRATION15 = open(os.path.join(HERE, "..", "ops", "migrate_schema15.sql"), encoding="utf-8").read()
ROLLBACK15 = open(os.path.join(HERE, "..", "ops", "rollback_schema15.sql"), encoding="utf-8").read()
SCHEMA14_TABLES = ("sensor_tokens", "sensor_status", "sensor_heartbeats", "ingest_state",
                   "sensor_clock_boots", "sensor_seq_gaps")
SCHEMA14_FUNCTIONS = ("ingest_stage_create", "ingest_merge", "refresh_contact_periods")
SCHEMA9_TABLES = ("accounts", "account_settings", "networks", "aps", "ap_sensors", "sensor_annotations",
                  "sensor_config", "sensor_uploads", "kismet_alert_rules", "audit_log",
                  "ap_security_periods", "sensor_periods")


def function(sql, name):
    m = re.search(r"CREATE OR REPLACE FUNCTION %s\(.*?\n\$\$;" % name, sql, re.S)
    return m.group(0) if m else None


def table_body(sql, table):
    """The column/constraint list of CREATE TABLE [IF NOT EXISTS] <table> (...)."""
    m = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?%s \((.*?)\);" % table, sql, re.S)
    return m.group(1) if m else None


class SchemaFiles(unittest.TestCase):
    def test_migration_band_functions_equal_schema(self):
        for name in ("band_of_channel", "band_of_freq", "ap_band"):
            self.assertIsNotNone(function(SCHEMA, name), name)
            self.assertEqual(function(MIGRATION, name), function(SCHEMA, name), name)

    def test_no_upgrade_only_statements(self):
        code = re.sub(r"--[^\n]*", "", SCHEMA)            # statements, not the version notes
        for pattern in (r"ADD COLUMN IF NOT EXISTS", r"DROP INDEX IF EXISTS", r"DROP VIEW",
                        r"main_freq_khz"):
            self.assertNotRegex(code, pattern)

    def test_migration8_equals_schema(self):
        for name in ("ap_channel_at", "load_ap_load_samples"):
            self.assertIsNotNone(function(SCHEMA, name), name)
            # schema 10 changed only the AP set (devices.ever_ap); the migration stays as it ran
            self.assertEqual(function(MIGRATION8, name), function(SCHEMA, name).replace("d.ever_ap", "d.type = 'ap'"),
                             name)
        self.assertIsNotNone(table_body(SCHEMA, "ap_load_hourly"))
        self.assertEqual(table_body(MIGRATION8, "ap_load_hourly"), table_body(SCHEMA, "ap_load_hourly"))
        self.assertIn("CREATE INDEX ap_load_hourly_hour ON ap_load_hourly (sensor_id, hour);", MIGRATION8)
        self.assertIn("ap_load_hourly_hour ON ap_load_hourly (sensor_id, hour);", SCHEMA)

    def test_migration9_tables_equal_schema(self):
        for t in SCHEMA9_TABLES:
            if t == "sensor_config":       # schema 12 changed its plan CHECK (checked here instead)
                self.assertIn("CONSTRAINT sensor_config_channel_plan_check", table_body(SCHEMA, t))
                self.assertIn("ADD CONSTRAINT sensor_config_channel_plan_check", MIGRATION12)
                continue
            if t == "sensor_annotations":  # schema 13/14 added entry kinds
                self.assertIn("CONSTRAINT sensor_annotations_kind_check", table_body(SCHEMA, t))
                self.assertIn("ADD CONSTRAINT sensor_annotations_kind_check", MIGRATION13)
                self.assertIn("ADD CONSTRAINT sensor_annotations_kind_check", MIGRATION14)
                continue
            if t == "sensor_uploads":      # schema 14 added the batch columns
                self.assertIn("CONSTRAINT sensor_uploads_batch UNIQUE", table_body(SCHEMA, t))
                continue
            self.assertIsNotNone(table_body(SCHEMA, t), t)
            self.assertEqual(table_body(MIGRATION9, t), table_body(SCHEMA, t), t)

    def test_schema9_detections_and_columns(self):
        body = re.sub(r"--[^\n]*", "", table_body(SCHEMA, "detections"))
        for col in ("kind", "status", "status_at", "status_note", "ts_end", "updated_at"):
            self.assertRegex(body, r"\n\s+%s\s" % col, col)
        for gone in ("acked", "ack_note"):
            self.assertNotRegex(body, r"\b%s\b" % gone, gone)
        # the columns the migration adds exist in the fresh definitions too
        for table, cols in (("sensor_hourly", ("capture_polls", "limited_polls")),
                            ("sensor_summary", ("last_capture_at", "last_hop_n", "last_hop_visited",
                                                "last_hop_ok")),
                            ("ap_summary", ("security", "security_label", "crypt_bits_known",
                                            "rssi_typical")),
                            ("sensors", ("account_id", "hardware"))):
            for c in cols:
                self.assertRegex(table_body(SCHEMA, table), r"\n\s+%s\s" % c, "%s.%s" % (table, c))
                self.assertIn(c, MIGRATION9, c)

    def test_version(self):
        def version(sql):
            return re.search(r"VALUES \('schema_version', '(\d+)'\)", sql).group(1)
        self.assertEqual((version(SCHEMA), version(MIGRATION15), version(MIGRATION14), version(MIGRATION13),
                          version(MIGRATION12), version(MIGRATION11), version(MIGRATION10), version(MIGRATION9),
                          version(MIGRATION8), version(MIGRATION)),
                         ("15", "15", "14", "13", "12", "11", "10", "9", "8", "7"))
        self.assertEqual(version(ROLLBACK14), "13")
        self.assertEqual(version(ROLLBACK15), "14")

    def test_schema11_reasons_and_hardware(self):
        body = re.sub(r"--[^\n]*", "", table_body(SCHEMA, "detections"))
        self.assertRegex(body, r"\n\s+status_reason\s+text,")
        constraint = re.search(r"CONSTRAINT detections_status_reason CHECK \((CASE status.*?END)\)\);", SCHEMA, re.S)
        self.assertIsNotNone(constraint)
        # schema 12: the migration adds the same constraint as the fresh definition
        norm = lambda t: re.sub(r"\s+", " ", t).strip()
        migrated = re.search(r"ADD CONSTRAINT detections_status_reason CHECK \((CASE status.*?END)\);", MIGRATION12, re.S)
        self.assertEqual(norm(migrated.group(1)), norm(constraint.group(1)))
        for reason in ("security_relevant", "staged_test", "expected_behaviour", "accepted_risk", "rule_defect", "artefact"):
            self.assertIn(reason, constraint.group(1))
        # every resolved branch refuses a NULL reason (a NULL CHECK passes)
        for outcome in ("confirmed", "benign", "false_positive"):
            self.assertRegex(constraint.group(1), r"WHEN '%s' THEN status_reason IS NOT NULL AND" % outcome)
        self.assertIn("'open', 'confirmed', 'benign', 'false_positive'", SCHEMA)
        self.assertNotIn("no_empty_dfs_59", SCHEMA)
        self.assertIn("ADD COLUMN status_reason text;", MIGRATION11)
        self.assertRegex(table_body(SCHEMA, "ap_baselines"), r"\n\s+hw_from\s+timestamptz,")
        self.assertIn("ALTER TABLE ap_baselines ADD COLUMN hw_from timestamptz;", MIGRATION11)
        # a baseline is learned inside one hardware period; the merge keeps the reason
        rab = function(SCHEMA, "refresh_ap_baselines")
        self.assertIn("o.ts >= lower(v_hw) AND o.ts < upper(v_hw)", rab)
        self.assertIn("status_reason = v_reason", function(SCHEMA, "merge_duplicate_kismet_events"))

    def test_schema13_no_antenna_catalog(self):
        code = re.sub(r"--[^\n]*", "", SCHEMA)
        # the catalog and the attachment history are gone from the fresh definition ...
        for gone in ("capture_hardware", "sensor_hardware", "antenna"):
            self.assertNotIn(gone, code, gone)
        # ... and the migration drops them
        for stmt in ("DROP TABLE sensor_hardware;", "DROP TABLE capture_hardware;", "DROP FUNCTION sensor_hardware_kinds();"):
            self.assertIn(stmt, MIGRATION13)
        # the adapter record: the same table as in the fresh definition
        self.assertIsNotNone(table_body(SCHEMA, "sensor_adapters"))
        self.assertEqual(table_body(MIGRATION13, "sensor_adapters"), table_body(SCHEMA, "sensor_adapters"))
        # the boundary is the history entry kind, in the table and in the migration
        self.assertIn("'hardware_change'", table_body(SCHEMA, "sensor_annotations"))
        self.assertIn("'hardware_change'", MIGRATION13)
        # hardware_period() is the same function in both and reads the entries
        hp = function(SCHEMA, "hardware_period")
        self.assertEqual(hp, function(MIGRATION13, "hardware_period"))
        self.assertIn("a.kind = 'hardware_change'", hp)
        self.assertNotIn("sensor_hardware", hp)

    def test_schema10_ever_ap(self):
        body = re.sub(r"--[^\n]*", "", table_body(SCHEMA, "devices"))
        self.assertRegex(body, r"\n\s+ever_ap\s+boolean NOT NULL DEFAULT false,")
        self.assertIn("ADD COLUMN ever_ap boolean NOT NULL DEFAULT false;", MIGRATION10)
        index = "devices_ever_ap ON devices (sensor_id, device_key) WHERE ever_ap;"
        self.assertIn("CREATE INDEX IF NOT EXISTS " + index.replace("devices_ever_ap", "devices_ever_ap  "), SCHEMA)
        self.assertIn("CREATE INDEX " + index, MIGRATION10)
        self.assertIn("CREATE OR REPLACE TRIGGER devices_ever_ap BEFORE INSERT OR UPDATE ON devices", SCHEMA)
        # the AP set is ever_ap everywhere; the type is read only by the trigger
        code = re.sub(r"--[^\n]*", "", SCHEMA)
        self.assertEqual(re.findall(r"type = 'ap'", code), ["type = 'ap'"])
        # schema 14: the rule is ap_evidence(), shared with the live ingest's client filter
        self.assertIn("ap_evidence(NEW.type, NEW.ssid, NEW.crypt, NEW.adv_channel)", function(SCHEMA, "devices_ever_ap"))
        self.assertIn("p_type = 'ap'", function(SCHEMA, "ap_evidence"))

    def test_observations_definition_matches_the_migration(self):
        def cols(sql, table):
            sql = re.sub(r"--[^\n]*", "", sql)
            start = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?%s \(" % table, sql).end()
            body = sql[start:sql.index(";", start)]
            return re.findall(r"^\s+(\w+)\s+(timestamptz|bigint|integer|smallint|boolean|text)\b",
                              body, re.M)
        self.assertEqual(cols(SCHEMA, "observations"), cols(MIGRATION, "observations_new"))
        self.assertEqual([c for c, _ in cols(SCHEMA, "observations")],
                         ["ts", "pk_total", "pk_data", "bss_timestamp", "disconnects_last",
                          "sensor_id", "disconnects", "rssi", "rssi_floor", "device_key"])

    def test_schema14_tables_and_functions_equal_schema(self):
        for t in SCHEMA14_TABLES:
            self.assertIsNotNone(table_body(SCHEMA, t), t)
            self.assertEqual(table_body(MIGRATION14, t), table_body(SCHEMA, t), t)
            self.assertIn("DROP TABLE %s;" % t, ROLLBACK14, t)
        for name in SCHEMA14_FUNCTIONS:
            self.assertIsNotNone(function(SCHEMA, name), name)
            self.assertEqual(function(MIGRATION14, name), function(SCHEMA, name), name)
            self.assertIn("DROP FUNCTION %s(" % name, ROLLBACK14, name)

    def test_schema14_columns(self):
        for table, cols in (("polls", ("seq", "received_at", "ts_offset_s")),
                            ("sensor_uploads", ("buffer_id", "batch_seq", "sent_at", "attempt", "replays",
                                                "bytes", "duration_ms"))):
            body = re.sub(r"--[^\n]*", "", table_body(SCHEMA, table))
            for c in cols:
                self.assertRegex(body, r"\n\s+%s\s" % c, "%s.%s" % (table, c))
                self.assertRegex(MIGRATION14, r"ADD COLUMN %s\b" % c, "%s.%s" % (table, c))
                self.assertRegex(ROLLBACK14, r"DROP COLUMN %s\b" % c, "%s.%s" % (table, c))
        for kind in ("'lost'", "'unreachable'"):
            self.assertIn(kind, table_body(SCHEMA, "sensor_annotations"))
            self.assertIn(kind, MIGRATION14)

    def test_schema14_merge_is_the_only_writer_of_the_staged_tables(self):
        merge = function(SCHEMA, "ingest_merge")
        for t in ("devices", "polls", "device_config_history", "observations", "ap_load_hourly",
                  "device_freq_hist", "associations", "client_bssids", "probes", "alerts"):
            self.assertRegex(merge, r"INSERT INTO %s\b" % t, t)
        # the AP set of the time series is ever_ap, never the device type
        self.assertNotIn("type = 'ap'", re.sub(r"--[^\n]*", "", merge))
        self.assertIn("load_ap_load_samples(p_sensor_id)", merge)
        # live ingest never writes client data, whatever is staged
        self.assertIn("p_via = 'snapshot'", merge)
        # a newer row without a configuration-change time keeps the stored one (N9)
        self.assertIn("coalesce(EXCLUDED.config_changed_at, devices.config_changed_at)", merge)
        # polls record when the server stored them
        self.assertIn("received_at", merge)
        # session staging tables: created once per connection, emptied by every commit
        stage = function(SCHEMA, "ingest_stage_create")
        self.assertEqual(stage.count("CREATE TEMP TABLE IF NOT EXISTS"), 11)
        self.assertEqual(stage.count("ON COMMIT DELETE ROWS"), 11)

    def test_importer_stages_the_columns_ingest_stage_create_creates(self):
        import sys
        sys.path.insert(0, os.path.join(HERE, "..", "seed"))
        import import_snapshot as imp
        stages = stage_tables(function(SCHEMA, "ingest_stage_create"))
        for spec in imp.TABLES:
            cols = [c[0] for c in spec["stage"]]
            self.assertIn(spec["name"], stages, spec["name"])
            self.assertTrue(set(cols) <= set(stages[spec["name"]]),
                            "%s: %s not in %s" % (spec["name"], cols, stages[spec["name"]]))

    def test_load_samples_stage_matches_the_loader(self):
        stages = stage_tables(function(SCHEMA, "ingest_stage_create"))
        self.assertEqual(stages["load_samples"], ["ts", "device_key", "n_clients", "qbss_stations", "util_pct"])


class Schema15(unittest.TestCase):
    """The incremental refresh (live upload N8): ap_config_changes a table
    filled per AP from the rules view; the migration carries the same
    definitions; the rollback rebuilds the schema-14 materialized view from the
    same rules."""

    def view(self, sql):
        return re.search(r"CREATE OR REPLACE VIEW ap_config_changes_v AS\n.*?new_val <> val;", sql, re.S).group(0)

    def test_table_view_and_functions_equal_the_migration(self):
        self.assertEqual(table_body(MIGRATION15, "ap_config_changes_t"), table_body(SCHEMA, "ap_config_changes"))
        self.assertEqual(self.view(MIGRATION15), self.view(SCHEMA))
        for name in ("refresh_config_changes", "refresh_security_periods", "refresh_rollups"):
            self.assertIsNotNone(function(SCHEMA, name), name)
            self.assertEqual(function(MIGRATION15, name), function(SCHEMA, name), name)

    def test_no_materialized_view_left(self):
        code = re.sub(r"--[^\n]*", "", SCHEMA)
        self.assertNotIn("MATERIALIZED VIEW", code)
        self.assertNotIn("REFRESH MATERIALIZED", code)
        self.assertIn("DROP MATERIALIZED VIEW ap_config_changes;", MIGRATION15)
        self.assertIn("CREATE MATERIALIZED VIEW ap_config_changes AS", ROLLBACK15)

    def test_rules_are_the_same_as_schema_14s(self):
        rules = self.view(SCHEMA).split("AS\n", 1)[1].rstrip(";")
        self.assertIn(rules, ROLLBACK15)

    def test_refresh_is_incremental(self):
        rr = function(SCHEMA, "refresh_rollups")
        self.assertIn("PERFORM refresh_config_changes(p_sensor_id, ARRAY(", rr)
        self.assertIn("WHERE h.sensor_id = p_sensor_id AND h.ts >= v_from));", rr)   # APs with new history only
        self.assertIn("o.device_key = ANY (v_new_keys)", rr)                          # new APs by key
        self.assertIn("WHERE o.sensor_id = p_sensor_id AND o.ts >= v_from", rr)       # one constant bound
        self.assertNotIn("o.ts >= a.since", rr)
        for name in ("refresh_rollups", "refresh_security_periods"):
            body = function(SCHEMA, name)
            # the security functions only inside the per-distinct-input CTE
            self.assertEqual(body.count("security_level("), 1, name)
            self.assertIn("sec AS MATERIALIZED", body, name)

    def test_fixture_tests_build_the_table(self):
        for t in ("ap_config_changes_test", "ap_channel_at_test", "events_test", "refresh_rollups_test"):
            text = open(os.path.join(HERE, "sql", t + ".sql"), encoding="utf-8").read()
            self.assertNotIn("REFRESH MATERIALIZED", text, t)
            self.assertIn("pg_get_viewdef('public.ap_config_changes_v'::regclass)", text, t)


def stage_tables(fn):
    """{name: [columns]} of the CREATE TEMP TABLE statements of ingest_stage_create()."""
    out = {}
    for name, body in re.findall(r"CREATE TEMP TABLE IF NOT EXISTS stage_(\w+) \((.*?)\) ON COMMIT", fn, re.S):
        out[name] = [c.strip().split()[0] for c in body.split(",") if c.strip()]
    return out


if __name__ == "__main__":
    unittest.main()
