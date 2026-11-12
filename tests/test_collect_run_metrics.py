"""Tests for scripts/collect_run_metrics.py.

These tests need no database, no Azure access, and no third-party packages.
SQL is replaced by a small fake connection that returns prepared rows, and the
Azure REST call is replaced by a fake that returns prepared pages. Everything
is written to temporary directories.
"""
import csv
import io
import json
import logging
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import collect_run_metrics as c

GIB = c.BYTES_PER_GIB


def setUpModule():
    # The commands log progress and expected errors to the console. Silence
    # that during tests so the test report stays readable.
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


CONTEXT_COLUMNS = ["engine_edition", "product_version", "database_name", "recovery_model",
                   "log_reuse_wait", "sql_captured_at"]
WAIT_COLUMNS = ["wait_type", "waiting_tasks_count", "wait_time_ms", "signal_wait_time_ms"]
FILE_COLUMNS = ["file_id", "logical_file_name", "file_type", "num_of_reads", "num_of_writes",
                "num_of_bytes_read", "num_of_bytes_written", "io_stall_read_ms", "io_stall_write_ms"]
LOG_COLUMNS = ["total_log_size_in_bytes", "used_log_space_in_bytes", "used_log_space_in_percent"]


class FakeCursor:
    """Returns prepared rows for whichever system view the query reads."""

    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self._rows = []

    def execute(self, sql):
        self.connection.executed.append(sql)
        data = self.connection.data
        if "SERVERPROPERTY" in sql:
            columns, rows = CONTEXT_COLUMNS, data["context"]
        elif "wait_stats" in sql:
            columns, rows = WAIT_COLUMNS, data["waits"]
        elif "dm_io_virtual_file_stats" in sql:
            columns, rows = FILE_COLUMNS, data["files"]
        elif "dm_db_log_space_usage" in sql:
            columns, rows = LOG_COLUMNS, data["log"]
        else:  # an unexpected query is a test failure, not a silent empty result
            raise AssertionError(f"Unexpected query: {sql}")
        self.description = [(name,) for name in columns]
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, data):
        self.data = data
        self.executed = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def sql_data(engine_edition=8, writelog_ms=1000, log_writes=100, log_bytes=1 * GIB, log_stall=500,
             data_writes=200, used_log_bytes=GIB // 4, total_log_bytes=8 * GIB, reuse_wait="NOTHING"):
    """Rows a fake connection returns for one snapshot."""
    return {
        "context": [(engine_edition, "16.0.1000.6", "LoadTest", "FULL", reuse_wait, "2026-11-11T21:00:00+00:00")],
        "waits": [
            ("WRITELOG", 10, writelog_ms, 5),
            ("INSTANCE_LOG_RATE_GOVERNOR", 0, 0, 0),
            ("LCK_M_X", 1, 100, 0),
            ("LCK_M_IX", 1, 50, 0),
            ("PAGEIOLATCH_EX", 2, 40, 0),
        ],
        "files": [
            (1, "LoadTest", "ROWS", 1000, data_writes, 2 * GIB, 4 * GIB, 900, 800),
            (2, "LoadTest_log", "LOG", 10, log_writes, 0, log_bytes, 0, log_stall),
        ],
        "log": [(total_log_bytes, used_log_bytes, 100.0 * used_log_bytes / total_log_bytes)],
    }


def at(hour, minute, second=0):
    return lambda: datetime(2026, 11, 11, hour, minute, second, tzinfo=timezone.utc)


def snapshot_pair():
    """A 'before' and 'after' snapshot 100 seconds apart with known differences."""
    before = c.capture_sql_snapshot(FakeConnection(sql_data()), now=at(21, 0, 0))
    after = c.capture_sql_snapshot(
        FakeConnection(sql_data(writelog_ms=4000, log_writes=1100, log_bytes=3 * GIB, log_stall=2500,
                                data_writes=700, used_log_bytes=GIB, reuse_wait="LOG_BACKUP")),
        now=at(21, 1, 40),
    )
    return before, after


# A Copy activity run in the shape the Azure REST API returns.
ACTIVITY_RUN = {
    "activityName": "CopyOrders",
    "activityType": "Copy",
    "status": "Succeeded",
    "activityRunStart": "2026-11-11T21:00:05Z",
    "activityRunEnd": "2026-11-11T21:01:35Z",
    "input": {
        "source": {"type": "DelimitedTextSource"},
        "sink": {"type": "SqlMISink", "writeBatchSize": 100000, "sqlWriterUseTableLock": True},
        "dataIntegrationUnits": 8,
        "parallelCopies": 4,
    },
    "output": {
        "dataRead": 2 * GIB,
        "dataWritten": 2 * GIB,
        "filesRead": 4,
        "rowsRead": 9000000,
        "rowsCopied": 9000000,
        "copyDuration": 90,
        "throughput": 23301.69,
        "sourcePeakConnections": 4,
        "sinkPeakConnections": 4,
        "usedDataIntegrationUnits": 8,
        "usedParallelCopies": 4,
        "effectiveIntegrationRuntime": "AutoResolveIntegrationRuntime (East US)",
        "billingReference": "{\"activityType\":\"DataMovement\",\"billableDuration\":[{\"meterType\":\"AzureIR\",\"duration\":0.2,\"unit\":\"DIUHours\"}]}",
        "errors": [],
        "executionDetails": [{
            "source": {"type": "AzureBlobStorage"},
            "sink": {"type": "AzureSqlMI"},
            "status": "Succeeded",
            "start": "2026-11-11T21:00:05Z",
            "profile": {"transfer": {"details": {
                "readingFromSource": {"workingDuration": 30},
                "writingToSink": {"workingDuration": 80},
            }}},
            "detailedDurations": {"queuingDuration": 3, "timeToFirstByte": 1, "transferDuration": 86},
        }],
        "perfRecommendation": [{"Tip": "Sink throughput is the limiting factor."}],
    },
}

NOTES = {
    "change_under_test": "DIU 4 -> 8",
    "target_table_design": "heap, no indexes",
    "cost_per_load": 1.5,
    "correctness": "row counts match",
    "result": "PASS",
    "reason_for_next_action": "Throughput rose 30% with no log governor waits; test 16 DIU next.",
}
TEST_IDENTITY = {"test_id": "DIU-08", "environment": "dev", "started_at_utc": "2026-11-11T21:00:00Z",
                 "stopped_at_utc": "2026-11-11T21:01:40Z", "recorded_by": "tester", "host": "laptop"}


class HelperTests(unittest.TestCase):
    def test_test_id_must_be_safe_as_a_folder_name(self):
        self.assertEqual(c.validate_test_id("DIU-08_run.1"), "DIU-08_run.1")
        for bad in ["", "../escape", "has space", "-leading-dash", "x" * 65, "a/b"]:
            with self.assertRaises(ValueError):
                c.validate_test_id(bad)

    def test_safe_divide_never_raises(self):
        self.assertIsNone(c.safe_divide(10, 0))
        self.assertIsNone(c.safe_divide(None, 5))
        self.assertEqual(c.safe_divide(10, 4), 2.5)

    def test_dig_returns_none_for_missing_paths(self):
        record = {"sink": {"waits": {"WRITELOG": {"wait_time_ms": 7}}}}
        self.assertEqual(c.dig(record, "sink.waits.WRITELOG.wait_time_ms"), 7)
        self.assertIsNone(c.dig(record, "sink.log_io.gib_written"))


class SqlSnapshotTests(unittest.TestCase):
    def test_instance_platforms_use_the_instance_wait_view(self):
        connection = FakeConnection(sql_data(engine_edition=8))
        snapshot = c.capture_sql_snapshot(connection, now=at(21, 0))
        self.assertEqual(snapshot["wait_stats_scope"], "instance")
        self.assertTrue(any("sys.dm_os_wait_stats" in sql for sql in connection.executed))
        self.assertEqual(snapshot["wait_stats"]["WRITELOG"]["wait_time_ms"], 1000)
        self.assertEqual(snapshot["captured_at_utc"], "2026-11-11T21:00:00Z")
        self.assertEqual([f["file_type"] for f in snapshot["files"]], ["ROWS", "LOG"])

    def test_azure_sql_database_uses_the_database_wait_view(self):
        connection = FakeConnection(sql_data(engine_edition=c.ENGINE_EDITION_AZURE_SQL_DATABASE))
        snapshot = c.capture_sql_snapshot(connection, now=at(21, 0))
        self.assertEqual(snapshot["wait_stats_scope"], "database")
        self.assertTrue(any("sys.dm_db_wait_stats" in sql for sql in connection.executed))
        self.assertFalse(any("sys.dm_os_wait_stats" in sql for sql in connection.executed))

    def test_every_query_is_a_select(self):
        connection = FakeConnection(sql_data())
        c.capture_sql_snapshot(connection, now=at(21, 0))
        for sql in connection.executed:
            self.assertTrue(sql.strip().upper().startswith("SELECT"), sql)
            for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "EXEC "):
                self.assertNotIn(forbidden, sql.upper())


class SqlDeltaTests(unittest.TestCase):
    def test_deltas_describe_only_the_window_between_snapshots(self):
        before, after = snapshot_pair()
        deltas = c.compute_sql_deltas(before, after)
        self.assertEqual(deltas["measurement_window_seconds"], 100.0)
        self.assertEqual(deltas["waits"]["WRITELOG"]["wait_time_ms"], 3000)
        # A wait type that never appears in either snapshot counts as zero, not missing.
        self.assertEqual(deltas["waits"]["ASYNC_NETWORK_IO"]["wait_time_ms"], 0)
        self.assertEqual(deltas["lock_wait_ms"], 0)
        # Log: 2 GiB written by 1,000 writes that stalled 2,000 ms in 100 seconds.
        self.assertEqual(deltas["log_io"]["gib_written"], 2.0)
        self.assertEqual(deltas["log_io"]["write_latency_ms"], 2.0)
        self.assertEqual(deltas["log_io"]["write_mib_per_second"], 20.48)
        # Data file: 500 writes and no extra reads in 100 seconds.
        self.assertEqual(deltas["data_io"]["writes"], 500)
        self.assertEqual(deltas["data_io"]["iops"], 5.0)
        self.assertEqual(deltas["log_usage"]["used_gib_before"], 0.25)
        self.assertEqual(deltas["log_usage"]["used_gib_after"], 1.0)
        self.assertEqual(deltas["log_usage"]["log_reuse_wait_after"], "LOG_BACKUP")

    def test_no_writes_gives_no_latency_instead_of_a_division_error(self):
        before, _ = snapshot_pair()
        after = dict(before, captured_at_utc="2026-11-11T21:00:30Z")
        deltas = c.compute_sql_deltas(before, after)
        self.assertIsNone(deltas["log_io"]["write_latency_ms"])
        self.assertEqual(deltas["log_io"]["gib_written"], 0.0)

    def test_counter_reset_is_reported_not_turned_into_a_negative_number(self):
        before, after = snapshot_pair()
        after["wait_stats"]["WRITELOG"]["wait_time_ms"] = 5  # lower than before: counters restarted
        deltas = c.compute_sql_deltas(before, after)
        self.assertIsNone(deltas["waits"]["WRITELOG"]["wait_time_ms"])
        self.assertTrue(deltas["waits"]["WRITELOG"]["counters_reset"])
        self.assertTrue(any("reset" in warning for warning in deltas["warnings"]))

    def test_log_growth_during_the_test_is_flagged(self):
        before, after = snapshot_pair()
        after["log_space"]["total_log_size_in_bytes"] = 16 * GIB
        deltas = c.compute_sql_deltas(before, after)
        self.assertTrue(any("log grew" in warning for warning in deltas["warnings"]))

    def test_instance_scope_is_called_out(self):
        before, after = snapshot_pair()
        deltas = c.compute_sql_deltas(before, after)
        self.assertTrue(any("instance-wide" in warning for warning in deltas["warnings"]))


class CopyActivityTests(unittest.TestCase):
    def test_whole_activity_run_gives_configured_and_actual_settings(self):
        copy = c.summarise_copy_activity(ACTIVITY_RUN)
        self.assertTrue(copy["input_supplied"])
        self.assertEqual(copy["configured_diu"], 8)
        self.assertEqual(copy["used_diu"], 8)
        self.assertEqual(copy["configured_parallel_copies"], 4)
        self.assertEqual(copy["write_batch_size"], 100000)
        self.assertTrue(copy["table_lock"])
        self.assertEqual(copy["source_gib"], 2.0)
        self.assertEqual(copy["files_read"], 4)
        self.assertEqual(copy["rows_per_second"], 100000.0)
        self.assertEqual(copy["throughput_mib_per_second"], 22.756)
        self.assertEqual(copy["queue_seconds"], 3)
        self.assertEqual(copy["writing_to_sink_seconds"], 80)
        self.assertEqual(copy["source_type"], "AzureBlobStorage")
        self.assertEqual(copy["billing"]["activityType"], "DataMovement")
        self.assertEqual(copy["performance_tips"], ["Sink throughput is the limiting factor."])

    def test_output_only_records_configured_settings_as_unknown(self):
        copy = c.summarise_copy_activity(ACTIVITY_RUN["output"])
        self.assertFalse(copy["input_supplied"])
        self.assertIsNone(copy["configured_diu"])
        self.assertEqual(copy["used_diu"], 8)
        self.assertEqual(copy["rows_copied"], 9000000)

    def test_missing_execution_details_do_not_break_the_summary(self):
        output = {"dataRead": GIB, "copyDuration": 10, "rowsCopied": 100}
        copy = c.summarise_copy_activity(output)
        self.assertIsNone(copy["queue_seconds"])
        self.assertEqual(copy["rows_per_second"], 10.0)

    def test_unrelated_json_is_rejected_with_a_clear_message(self):
        with self.assertRaises(ValueError):
            c.summarise_copy_activity({"name": "not a copy result"})

    def test_single_copy_activity_is_selected(self):
        lookup = {"activityName": "GetFiles", "activityType": "GetMetadata"}
        self.assertIs(c.select_copy_activity([lookup, ACTIVITY_RUN], None), ACTIVITY_RUN)

    def test_two_copy_activities_need_a_name(self):
        other = dict(ACTIVITY_RUN, activityName="CopyCustomers")
        with self.assertRaises(RuntimeError) as raised:
            c.select_copy_activity([ACTIVITY_RUN, other], None)
        self.assertIn("--activity-name", str(raised.exception))
        self.assertIs(c.select_copy_activity([ACTIVITY_RUN, other], "CopyCustomers"), other)

    def test_no_copy_activity_is_an_error(self):
        with self.assertRaises(RuntimeError):
            c.select_copy_activity([{"activityName": "GetFiles", "activityType": "GetMetadata"}], None)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AzureQueryTests(unittest.TestCase):
    def test_paging_continues_until_no_token_is_returned(self):
        pages = [
            # ADF can return an empty page with a token; reading must continue.
            {"value": [], "continuationToken": "page-2"},
            {"value": [ACTIVITY_RUN]},
        ]
        requests = []

        def opener(request, timeout=None):
            requests.append(request)
            return FakeResponse(pages[len(requests) - 1])

        runs = c.query_activity_runs(
            "sub-id", "rg-dev", "adf-dev", "run-123",
            datetime(2026, 11, 10, tzinfo=timezone.utc), datetime(2026, 11, 12, tzinfo=timezone.utc),
            "token-value", opener=opener,
        )
        self.assertEqual(runs, [ACTIVITY_RUN])
        self.assertEqual(len(requests), 2)
        self.assertIn("/factories/adf-dev/pipelineruns/run-123/queryActivityruns", requests[0].full_url)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer token-value")
        self.assertNotIn("continuationToken", json.loads(requests[0].data))
        self.assertEqual(json.loads(requests[1].data)["continuationToken"], "page-2")


class BenchmarkRecordTests(unittest.TestCase):
    def record(self, activity=ACTIVITY_RUN, notes=NOTES):
        before, after = snapshot_pair()
        return c.build_benchmark_record(
            TEST_IDENTITY, notes, c.summarise_copy_activity(activity), c.compute_sql_deltas(before, after)
        )

    def test_record_holds_every_runbook_section(self):
        record = self.record()
        self.assertEqual(record["schema_version"], c.RECORD_SCHEMA_VERSION)
        self.assertEqual(record["source"], {"type": "AzureBlobStorage", "gib": 2.0, "row_count": 9000000, "file_count": 4})
        self.assertEqual(record["settings"]["configured_diu"], 8)
        self.assertEqual(record["settings"]["target_table_design"], "heap, no indexes")
        self.assertEqual(record["result"]["copy_duration_seconds"], 90)
        self.assertEqual(record["sink"]["waits"]["WRITELOG"]["wait_time_ms"], 3000)
        self.assertEqual(record["cost"]["cost_per_gib"], 0.75)
        self.assertTrue(record["correctness"]["rows_read_equal_rows_copied"])
        self.assertEqual(record["outcome"]["result"], "PASS")
        self.assertIn("Throughput rose 30%", record["outcome"]["reason_for_next_action"])

    def test_row_mismatch_and_failure_are_warned_about(self):
        activity = json.loads(json.dumps(ACTIVITY_RUN))
        activity["status"] = "Failed"
        activity["output"]["rowsCopied"] = 8999990
        activity["output"]["rowsSkipped"] = 10
        record = self.record(activity)
        text = " | ".join(record["warnings"])
        self.assertIn("not 'Succeeded'", text)
        self.assertIn("do not equal rows copied", text)
        self.assertIn("incompatible rows were skipped", text)
        self.assertFalse(record["correctness"]["rows_read_equal_rows_copied"])

    def test_long_measurement_window_is_warned_about(self):
        before, after = snapshot_pair()
        after["captured_at_utc"] = "2026-11-11T21:30:00Z"  # 30 minutes for a 90-second copy
        record = c.build_benchmark_record(
            TEST_IDENTITY, NOTES, c.summarise_copy_activity(ACTIVITY_RUN), c.compute_sql_deltas(before, after)
        )
        self.assertTrue(any("SQL measurements cover" in warning for warning in record["warnings"]))

    def test_record_without_sql_says_so(self):
        record = c.build_benchmark_record(TEST_IDENTITY, NOTES, c.summarise_copy_activity(ACTIVITY_RUN), None)
        self.assertEqual(record["sink"], {"collected": False})
        self.assertIn("SQL measurements were not collected", c.render_markdown(record))

    def test_markdown_uses_the_runbook_field_names_and_shows_missing_values(self):
        record = self.record(ACTIVITY_RUN["output"])  # output only: configured values unknown
        markdown = c.render_markdown(record)
        for label in ["Test ID", "Source size", "Source row count", "Source file count", "Configured DIUs",
                      "Actual DIUs", "Write batch size", "Target table design", "Copy duration", "Rows rejected",
                      "Log used before", "WRITELOG wait", "INSTANCE_LOG_RATE_GOVERNOR wait", "Log write latency",
                      "Lock wait", "Cost per GiB", "PASS / FAIL / INVESTIGATE", "Reason for the next action"]:
            self.assertIn(label, markdown)
        self.assertIn("| Configured DIUs | not recorded |", markdown)
        self.assertIn("| Actual DIUs | 8 |", markdown)


class CommandLineTests(unittest.TestCase):
    """Run the commands end to end, the way an operator would."""

    def run_main(self, *argv):
        captured = io.StringIO()
        with redirect_stdout(captured):
            code = c.main(list(argv))
        return code, captured.getvalue()

    def stop_args(self, out, adf_file, test_id="T01"):
        return ["stop", "--test-id", test_id, "--output-dir", str(out), "--adf-output-file", str(adf_file),
                "--change", "DIU 4 -> 8", "--target-table-design", "heap", "--correctness", "row counts match",
                "--result", "PASS", "--next-action-reason", "Faster with a healthy sink; try 16 next."]

    def test_full_flow_without_sql(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "benchmarks"
            adf_file = Path(td) / "activity.json"
            adf_file.write_text(json.dumps(ACTIVITY_RUN), encoding="utf-8")

            code, _ = self.run_main("start", "--test-id", "T01", "--output-dir", str(out), "--no-sql")
            self.assertEqual(code, 0)
            code, printed = self.run_main(*self.stop_args(out, adf_file))
            self.assertEqual(code, 0, printed)

            record = json.loads((out / "T01" / "benchmark_record.json").read_text(encoding="utf-8"))
            self.assertEqual(record["sink"], {"collected": False})
            self.assertEqual(record["settings"]["actual_diu"], 8)
            for name in ["before.json", "adf_activity.json", "benchmark_record.md", "collector.log"]:
                self.assertTrue((out / "T01" / name).exists(), name)

            with (out / "benchmark_index.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["TestID"], "T01")
            self.assertEqual(rows[0]["ActualDIU"], "8")
            self.assertEqual(rows[0]["Result"], "PASS")

            code, printed = self.run_main("list", "--output-dir", str(out))
            self.assertEqual(code, 0)
            self.assertIn("T01", printed)

    def test_full_flow_with_sql_and_resumable_stop(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "benchmarks"
            adf_file = Path(td) / "activity.json"
            adf_file.write_text(json.dumps(ACTIVITY_RUN), encoding="utf-8")
            connections = [
                FakeConnection(sql_data()),
                FakeConnection(sql_data(writelog_ms=4000, log_writes=1100, log_bytes=3 * GIB, log_stall=2500)),
            ]
            opened = []

            def fake_open(**kwargs):
                opened.append(kwargs)
                return connections[len(opened) - 1]

            with patch.object(c, "open_sql_connection", side_effect=fake_open):
                code, _ = self.run_main("start", "--test-id", "T01", "--output-dir", str(out),
                                        "--server", "sqlmi-dev.example", "--database", "LoadTest",
                                        "--auth", "entra-default")
                self.assertEqual(code, 0)

                # First 'stop' fails after the snapshot because the ADF file name is wrong.
                code, _ = self.run_main(*self.stop_args(out, Path(td) / "missing.json"))
                self.assertEqual(code, 2)
                self.assertTrue((out / "T01" / "after.json").exists())
                self.assertFalse((out / "T01" / "benchmark_record.json").exists())

                # Second 'stop' succeeds and must reuse the snapshot, not take a later one.
                code, printed = self.run_main(*self.stop_args(out, adf_file))
                self.assertEqual(code, 0, printed)

            self.assertEqual(len(opened), 2, "one connection for 'start' and one for the first 'stop'")
            self.assertTrue(all(connection.closed for connection in connections))
            self.assertEqual(opened[0]["auth"], "entra-default")
            self.assertEqual(opened[1]["server"], "sqlmi-dev.example")

            before = json.loads((out / "T01" / "before.json").read_text(encoding="utf-8"))
            self.assertNotIn("password", json.dumps(before).lower())
            record = json.loads((out / "T01" / "benchmark_record.json").read_text(encoding="utf-8"))
            self.assertTrue(record["sink"]["collected"])
            self.assertEqual(record["sink"]["waits"]["WRITELOG"]["wait_time_ms"], 3000)
            self.assertEqual(record["sink"]["log_io"]["gib_written"], 2.0)
            self.assertEqual(record["test"]["sql_server"], "sqlmi-dev.example")

    def test_existing_results_are_never_replaced_silently(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "benchmarks"
            adf_file = Path(td) / "activity.json"
            adf_file.write_text(json.dumps(ACTIVITY_RUN), encoding="utf-8")
            self.assertEqual(self.run_main("start", "--test-id", "T01", "--output-dir", str(out), "--no-sql")[0], 0)
            self.assertEqual(self.run_main(*self.stop_args(out, adf_file))[0], 0)

            # Neither command overwrites without --force.
            self.assertEqual(self.run_main("start", "--test-id", "T01", "--output-dir", str(out), "--no-sql")[0], 2)
            self.assertEqual(self.run_main(*self.stop_args(out, adf_file))[0], 2)
            self.assertEqual(self.run_main(*self.stop_args(out, adf_file), "--force")[0], 0)

            # 'start --force' begins again and removes the earlier attempt's results.
            code, _ = self.run_main("start", "--test-id", "T01", "--output-dir", str(out), "--no-sql", "--force")
            self.assertEqual(code, 0)
            self.assertFalse((out / "T01" / "benchmark_record.json").exists())
            self.assertFalse((out / "T01" / "adf_activity.json").exists())

    def test_stop_requires_start(self):
        with tempfile.TemporaryDirectory() as td:
            code, _ = self.run_main(*self.stop_args(Path(td) / "benchmarks", Path(td) / "activity.json"))
            self.assertEqual(code, 2)

    def test_verdict_is_required_before_anything_is_measured(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "benchmarks"
            self.assertEqual(self.run_main("start", "--test-id", "T01", "--output-dir", str(out), "--no-sql")[0], 0)
            # No --result and no terminal to ask at: the command stops immediately.
            code, _ = self.run_main("stop", "--test-id", "T01", "--output-dir", str(out),
                                    "--adf-output-file", str(Path(td) / "activity.json"))
            self.assertEqual(code, 2)
            self.assertFalse((out / "T01" / "adf_activity.json").exists())

    def test_start_needs_a_server_unless_sql_is_switched_off(self):
        with tempfile.TemporaryDirectory() as td:
            code, _ = self.run_main("start", "--test-id", "T01", "--output-dir", str(Path(td) / "b"))
            self.assertEqual(code, 2)

    def test_saved_rest_response_is_accepted_as_the_adf_file(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "benchmarks"
            adf_file = Path(td) / "response.json"
            lookup = {"activityName": "GetFiles", "activityType": "GetMetadata"}
            adf_file.write_text(json.dumps({"value": [lookup, ACTIVITY_RUN]}), encoding="utf-8")
            self.assertEqual(self.run_main("start", "--test-id", "T01", "--output-dir", str(out), "--no-sql")[0], 0)
            self.assertEqual(self.run_main(*self.stop_args(out, adf_file))[0], 0)
            saved = json.loads((out / "T01" / "adf_activity.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["activityName"], "CopyOrders")


if __name__ == "__main__":
    unittest.main()
