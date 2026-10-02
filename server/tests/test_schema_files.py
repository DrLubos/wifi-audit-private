"""Static checks of schema.sql and the schema-7 migration (standard library,
no database):

  cd server && python3 -m unittest discover -s tests -p 'test_*.py'

- the band functions the migration creates are the ones schema.sql defines;
- schema.sql is the complete definition, without upgrade-only statements
  (ADD COLUMN IF NOT EXISTS, DROP INDEX IF EXISTS, view-to-matview DO block);
- the migration sets the version schema.sql declares.
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = open(os.path.join(HERE, "..", "schema.sql"), encoding="utf-8").read()
MIGRATION = open(os.path.join(HERE, "..", "ops", "migrate_schema7.sql"), encoding="utf-8").read()


def function(sql, name):
    m = re.search(r"CREATE OR REPLACE FUNCTION %s\(.*?\n\$\$;" % name, sql, re.S)
    return m.group(0) if m else None


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

    def test_version(self):
        declared = re.search(r"VALUES \('schema_version', '(\d+)'\)", SCHEMA).group(1)
        migrated = re.search(r"VALUES \('schema_version', '(\d+)'\)", MIGRATION).group(1)
        self.assertEqual((declared, migrated), ("7", "7"))

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
