"""Static checks of schema.sql and the schema-7/8 migrations (standard
library, no database):

  cd server && python3 -m unittest discover -s tests -p 'test_*.py'

- the functions and tables the migrations create are the ones schema.sql
  defines (schema 7: the band functions; schema 8: ap_load_hourly,
  ap_channel_at(), load_ap_load_samples());
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
            self.assertEqual(function(MIGRATION8, name), function(SCHEMA, name), name)
        self.assertIsNotNone(table_body(SCHEMA, "ap_load_hourly"))
        self.assertEqual(table_body(MIGRATION8, "ap_load_hourly"), table_body(SCHEMA, "ap_load_hourly"))
        self.assertIn("CREATE INDEX ap_load_hourly_hour ON ap_load_hourly (sensor_id, hour);", MIGRATION8)
        self.assertIn("ap_load_hourly_hour ON ap_load_hourly (sensor_id, hour);", SCHEMA)

    def test_version(self):
        def version(sql):
            return re.search(r"VALUES \('schema_version', '(\d+)'\)", sql).group(1)
        self.assertEqual((version(SCHEMA), version(MIGRATION8), version(MIGRATION)), ("8", "8", "7"))

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
