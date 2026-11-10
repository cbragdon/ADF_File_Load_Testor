import argparse
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import generate_delimited_file as g


class GenerateDelimitedFileTests(unittest.TestCase):
    def setUp(self):
        self.columns = [
            g.ColumnSpec("ID", "int", nullable=False, is_primary_key=True),
            g.ColumnSpec("EmailAddress", "nvarchar", max_length=40),
            g.ColumnSpec("Amount", "decimal", nullable=False, precision=10, scale=2),
        ]

    def test_schema_rows_are_deterministic(self):
        a = list(g.generate_schema_rows(self.columns, 3, seed=42))
        b = list(g.generate_schema_rows(self.columns, 3, seed=42))
        self.assertEqual(a, b)
        self.assertEqual(a[0]["ID"], 1)

    def test_write_preserves_txt_extension_and_pipe_delimiter(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "sample.txt"
            count = g.write_delimited(output, [c.name for c in self.columns],
                                      g.generate_schema_rows(self.columns, 4, 42), "|", '"', "utf-8", True)
            self.assertEqual(count, 4)
            self.assertEqual(output.suffix, ".txt")
            with output.open(newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f, delimiter="|"))
            self.assertEqual(rows[0], ["ID", "EmailAddress", "Amount"])
            self.assertEqual(len(rows), 5)

    def test_write_uses_requested_quote_character(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "quoted.txt"
            g.write_delimited(
                output,
                ["Value"],
                [{"Value": "contains|delimiter"}],
                "|",
                "'",
                "utf-8",
                True,
            )
            self.assertIn("'contains|delimiter'", output.read_text(encoding="utf-8"))
            with output.open(newline="", encoding="utf-8") as source:
                self.assertEqual(list(csv.reader(source, delimiter="|", quotechar="'"))[1], ["contains|delimiter"])

    def test_sdv_missing_verification_message(self):
        with patch.object(g, "is_package_available", return_value=False):
            ok, message = g.verify_sdv_installation()
        self.assertFalse(ok)
        self.assertIn("python -m pip install sdv", message)

    def test_included_columns_excludes_computed_and_rowversion(self):
        cols = self.columns + [g.ColumnSpec("Calc", "int", is_computed=True), g.ColumnSpec("RV", "rowversion")]
        names = [c.name for c in g.included_columns(cols)]
        self.assertEqual(names, ["ID", "EmailAddress", "Amount"])

    def test_load_standalone_config(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "schema.json"
            p.write_text(json.dumps({"columns": [{"name": "A", "sql_type": "int", "nullable": False}]}))
            cols = g.load_schema_config(p)
        self.assertEqual(cols[0].name, "A")
        self.assertFalse(cols[0].nullable)

    def test_windows_connection_string_uses_integrated_auth(self):
        value = g.build_connection_string("server", "database", "windows")
        self.assertIn("Trusted_Connection=yes", value)
        self.assertNotIn("UID=", value)
        self.assertNotIn("PWD=", value)
        self.assertIn("Encrypt=yes", value)

    def test_entra_interactive_connection_string(self):
        value = g.build_connection_string("mi.database.windows.net", "db", "entra-interactive")
        self.assertIn("Authentication=ActiveDirectoryInteractive", value)
        self.assertNotIn("Trusted_Connection", value)

    def test_entra_default_connection_string_has_no_embedded_auth_secret(self):
        value = g.build_connection_string("mi.database.windows.net", "db", "entra-default")
        self.assertNotIn("UID=", value)
        self.assertNotIn("PWD=", value)
        self.assertNotIn("Authentication=", value)

    def test_verify_sdv_does_not_require_generation_arguments(self):
        with patch.object(g, "verify_sdv_installation", return_value=(True, "SDV is installed")):
            with patch.object(sys, "argv", ["generate_delimited_file.py", "--verify-sdv"]):
                self.assertEqual(g.main(), 0)

    def test_auto_engine_falls_back_after_sdv_generation_failure(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "fallback.csv"
            args = argparse.Namespace(
                engine="auto",
                server="server",
                database="database",
                schema="dbo",
                table="Source",
                auth="windows",
                driver="ODBC Driver 18 for SQL Server",
                no_encrypt=False,
                trust_server_certificate=False,
                include_server_generated=False,
                config=None,
                no_fallback=False,
                rows=2,
                seed=42,
                output=output,
                delimiter=",",
                quotechar='"',
                encoding="utf-8",
                no_header=False,
                sample_rows=2,
            )
            connection = Mock()
            with patch.object(g, "connect_sql_server", return_value=connection), \
                 patch.object(g, "read_table_metadata", return_value=self.columns), \
                 patch.object(g, "verify_sdv_installation", return_value=(True, "SDV is installed")), \
                 patch.object(g, "read_table_sample", return_value=Mock()), \
                 patch.object(g, "synthesize_with_sdv", side_effect=RuntimeError("test failure")):
                result = g.generate(args)

            self.assertEqual(result.engine_used, "schema")
            self.assertEqual(result.row_count, 2)
            self.assertEqual(result.fallback_reason, "SDV failed: test failure")
            self.assertTrue(output.exists())
            connection.close.assert_called_once()



if __name__ == "__main__":
    unittest.main()
