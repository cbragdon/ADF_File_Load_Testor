import csv
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_delimited_file.py"
spec = importlib.util.spec_from_file_location("generate_delimited_file", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeCursor:
    description = [("order_id",), ("region",), ("created_at_utc",)]

    def __init__(self) -> None:
        self.query = None

    def execute(self, query: str) -> None:
        self.query = query


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


class FakePyodbc:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.connection_string = None

    def connect(self, connection_string: str) -> FakeConnection:
        self.connection_string = connection_string
        return self.connection


class GenerateDelimitedFileTests(unittest.TestCase):
    def test_command_line_parameters_generate_pipe_delimited_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "generated.psv"

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(output),
                    "--rows",
                    "2",
                    "--encoding",
                    "utf-8",
                    "--delimiter",
                    "|",
                    "--quotechar",
                    "'",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            with output.open("r", encoding="utf-8", newline="") as source:
                rows = list(csv.reader(source, delimiter="|", quotechar="'"))

            self.assertEqual(rows[0], mod.DEFAULT_HEADERS)
            self.assertEqual([row[0] for row in rows[1:]], ["1", "2"])

    def test_generates_default_header_and_requested_row_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "generated.csv"

            mod.generate_delimited_file(
                output,
                3,
                "utf-8-sig",
                ",",
                '"',
                mod.DEFAULT_HEADERS,
            )

            with output.open("r", encoding="utf-8-sig", newline="") as source:
                rows = list(csv.reader(source))

            self.assertEqual(rows[0], mod.DEFAULT_HEADERS)
            self.assertEqual(len(rows), 4)
            self.assertEqual([row[0] for row in rows[1:]], ["1", "2", "3"])
            self.assertTrue(all(row[1].startswith("payload-") for row in rows[1:]))

    def test_database_header_query_uses_returned_column_names(self) -> None:
        fake_pyodbc = FakePyodbc()

        with patch.dict(sys.modules, {"pyodbc": fake_pyodbc}):
            headers = mod.load_headers_from_database(
                "Driver={ODBC Driver 18 for SQL Server};Trusted_Connection=Yes;",
                "SELECT TOP (0) order_id, region, created_at_utc FROM dbo.orders",
            )

        self.assertEqual(headers, ["order_id", "region", "created_at_utc"])
        self.assertEqual(
            fake_pyodbc.connection_string,
            "Driver={ODBC Driver 18 for SQL Server};Trusted_Connection=Yes;",
        )
        self.assertEqual(
            fake_pyodbc.connection.cursor_instance.query,
            "SELECT TOP (0) order_id, region, created_at_utc FROM dbo.orders",
        )
        self.assertTrue(fake_pyodbc.connection.closed)

    def test_connection_string_rejects_stored_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a user name or password"):
            mod.validate_connection_string(
                "Driver={ODBC Driver 18 for SQL Server};Server=sqlvm.contoso.com;UID=user;PWD=secret;"
            )

    def test_connection_string_accepts_integrated_authentication(self) -> None:
        mod.validate_connection_string(
            "Driver={ODBC Driver 18 for SQL Server};Server=sqlvm.contoso.com;Trusted_Connection=Yes;"
        )
        mod.validate_connection_string(
            "Driver={ODBC Driver 18 for SQL Server};Server=managed-instance.public.database.windows.net;"
            "Authentication=ActiveDirectoryIntegrated;"
        )


if __name__ == "__main__":
    unittest.main()
