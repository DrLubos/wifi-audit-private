"""Static checks of schema.sql and the schema-7/8/9/10 migrations (standard
library, no database):

  cd server && python3 -m unittest discover -s tests -p 'test_*.py'

- the functions and tables the migrations create are the ones schema.sql
  defines (schema 7: the band functions; schema 8: ap_load_hourly,
  ap_channel_at(), load_ap_load_samples(); schema 9: the new tables;
  schema 10: devices.ever_ap, its index and trigger, nothing reads the
  device type for the AP set any more);
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
        self.assertEqual((version(SCHEMA), version(MIGRATION10), version(MIGRATION9), version(MIGRATION8),
                          version(MIGRATION)), ("10", "10", "9", "8", "7"))

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
        self.assertIn("NEW.type = 'ap'", function(SCHEMA, "devices_ever_ap"))

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

if __name__ == "__main__":
    unittest.main()
