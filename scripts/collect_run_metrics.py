#!/usr/bin/env python3
"""Collect one benchmark record for an Azure Data Factory (ADF) load test.

Why this script exists
----------------------
The tuning runbook (docs/ADF_Copy_Activity_Performance_Tuning_Runbook.md,
section 11) defines the *benchmark record*: the evidence that must be kept for
every meaningful test. Until now that record was assembled by hand from the
ADF monitoring view and several SQL monitoring scripts. Hand-assembled records
are slow to produce, easy to get wrong, and hard to compare.

This script produces the same record automatically, in a consistent shape, so
that test runs can be compared with each other and used later to refine copy
settings.

How one test is recorded
------------------------
A test is recorded with two commands, one on each side of the ADF run:

1. ``start``  - run immediately BEFORE the ADF pipeline is started. It takes
   a "before" snapshot of the SQL sink (wait statistics, file I/O counters,
   and transaction-log usage) and saves it in the test folder.
2. ``stop``   - run AFTER the ADF pipeline finishes. It takes the "after"
   snapshot, calculates what changed during the load, reads the Copy activity
   result from ADF, and writes the benchmark record.

   With ``--wait``, ``stop`` can be run straight after ``start`` instead. It
   checks the pipeline run's status in ADF at a fixed interval and takes the
   "after" snapshot the moment the run finishes, however long that takes.
   While it waits it reads nothing from the SQL sink, so waiting adds no load
   to the system being measured.

Two commands are needed because the SQL counters are cumulative. A single
reading says nothing about one load; only the difference between a reading
taken before and a reading taken after describes the load.

Where the ADF result comes from
-------------------------------
``stop`` accepts the Copy activity result in either of two ways:

* ``--adf-output-file``: a JSON file saved from the ADF Studio monitoring view
  (the activity "Output", or the whole activity run). No Azure access needed.
* ``--subscription-id/--resource-group/--factory/--pipeline-run-id``: the
  script reads the activity run from the Azure management REST API using the
  identity of the person running the script.

What is written
---------------
Everything is written under ``<output-dir>/<test-id>/`` (default
``output/benchmarks``, which the repository excludes from Git):

* ``before.json`` / ``after.json``   - the raw SQL snapshots
* ``adf_activity.json``              - the Copy activity result as received
* ``benchmark_record.json``          - the benchmark record (machine-readable)
* ``benchmark_record.md``            - the same record for people to read
* ``collector.log``                  - what the script did, and when

``<output-dir>/benchmark_index.csv`` holds one row per recorded test so that a
series of tests can be compared in a spreadsheet.

Safety
------
* Read-only against SQL: the script only selects from system views.
* Credential-free: SQL and Azure access use the Windows or Microsoft Entra
  identity of the person running the script. No password, key, or token is
  accepted on the command line or written to any file.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import platform
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

LOG = logging.getLogger("collect_run_metrics")

# Version of the benchmark record layout. Increase it whenever a field is
# renamed or removed so that older records can still be read correctly.
RECORD_SCHEMA_VERSION = 1

BYTES_PER_GIB = 1073741824
BYTES_PER_MIB = 1048576

# SERVERPROPERTY('EngineEdition') value for Azure SQL Database. That platform
# does not expose the instance-wide wait view to database users, so the
# database-scoped view is used instead.
ENGINE_EDITION_AZURE_SQL_DATABASE = 5

# The wait types the toolkit tracks. This is the same list used by
# sql/monitoring/07_capture_load_wait_stats.sql so that a record produced by
# this script can be compared with one captured by hand.
TRACKED_WAIT_TYPES: tuple[str, ...] = (
    "WRITELOG",
    "INSTANCE_LOG_RATE_GOVERNOR",
    "PAGEIOLATCH_EX",
    "PAGEIOLATCH_SH",
    "IO_COMPLETION",
    "ASYNC_IO_COMPLETION",
    "ASYNC_NETWORK_IO",
    "LCK_M_X",
    "LCK_M_IX",
    "LCK_M_SCH_M",
)
LOCK_WAIT_TYPES: tuple[str, ...] = ("LCK_M_X", "LCK_M_IX", "LCK_M_SCH_M")
PAGEIOLATCH_WAIT_TYPES: tuple[str, ...] = ("PAGEIOLATCH_EX", "PAGEIOLATCH_SH")

RESULT_CHOICES: tuple[str, ...] = ("PASS", "FAIL", "INVESTIGATE")

# A test ID becomes a folder name, so it is restricted to characters that are
# safe on Windows, Linux, and macOS file systems.
TEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MANAGEMENT_ENDPOINT = "https://management.azure.com"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"
DATA_FACTORY_API_VERSION = "2018-06-01"

# Pipeline run statuses. ADF reports Queued, InProgress, Canceling, and the
# three final statuses below. Waiting ends only on a final status; Canceling
# is not final, because the run may still be writing to the sink.
FINAL_PIPELINE_STATUSES: tuple[str, ...] = ("Succeeded", "Failed", "Cancelled")

# Waiting defaults, and the shortest check interval allowed. Checking more
# often than every 10 seconds gains nothing and adds calls to the ADF API.
DEFAULT_POLL_SECONDS = 30
MINIMUM_POLL_SECONDS = 10
DEFAULT_TIMEOUT_HOURS = 12.0

# A pipeline run found by name must have started after 'start', allowing this
# much difference between the local clock and Azure's.
CLOCK_TOLERANCE_SECONDS = 120

# Stop waiting after this many failed status checks in a row. Single failures
# (a dropped connection, throttling) are expected over a long wait.
MAX_CONSECUTIVE_CHECK_FAILURES = 10

# While waiting, write a "still waiting" line at most this often, so a
# twelve-hour wait does not fill the log with identical lines.
WAIT_HEARTBEAT_SECONDS = 600

# HTTP status codes worth retrying while waiting: throttling and server faults.
RETRYABLE_HTTP_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

# Files that belong to a finished or half-finished test. 'start --force'
# removes them so a restarted test cannot inherit results from the old one.
STALE_ON_RESTART: tuple[str, ...] = (
    "after.json", "pipeline_run.json", "adf_activity.json", "benchmark_record.json", "benchmark_record.md",
)

# Columns of benchmark_index.csv, in order. Each value is a path into the
# benchmark record, written as "section.field".
INDEX_COLUMNS: tuple[tuple[str, str], ...] = (
    ("TestID", "test.test_id"),
    ("Environment", "test.environment"),
    ("StartedAtUtc", "test.started_at_utc"),
    ("StoppedAtUtc", "test.stopped_at_utc"),
    ("Result", "outcome.result"),
    ("PipelineName", "test.pipeline_name"),
    ("PipelineRunId", "test.pipeline_run_id"),
    ("PipelineStatus", "test.pipeline_status"),
    ("ChangeUnderTest", "outcome.change_under_test"),
    ("SourceGiB", "source.gib"),
    ("SourceRowCount", "source.row_count"),
    ("SourceFileCount", "source.file_count"),
    ("ConfiguredDIU", "settings.configured_diu"),
    ("ActualDIU", "settings.actual_diu"),
    ("ConfiguredParallelCopies", "settings.configured_parallel_copies"),
    ("ActualParallelCopies", "settings.actual_parallel_copies"),
    ("SinkPeakConnections", "settings.sink_peak_connections"),
    ("WriteBatchSize", "settings.write_batch_size"),
    ("TableLock", "settings.table_lock"),
    ("TargetTableDesign", "settings.target_table_design"),
    ("CopyDurationSeconds", "result.copy_duration_seconds"),
    ("SnapshotLagSeconds", "test.snapshot_lag_seconds"),
    ("ThroughputMiBPerSecond", "result.throughput_mib_per_second"),
    ("RowsPerSecond", "result.rows_per_second"),
    ("RowsCopied", "result.rows_copied"),
    ("RowsRejected", "result.rows_rejected"),
    ("LogUsedGiBBefore", "sink.log_usage.used_gib_before"),
    ("LogUsedGiBAfter", "sink.log_usage.used_gib_after"),
    ("LogGiBWritten", "sink.log_io.gib_written"),
    ("LogWriteMiBPerSecond", "sink.log_io.write_mib_per_second"),
    ("LogWriteLatencyMs", "sink.log_io.write_latency_ms"),
    ("DataWriteLatencyMs", "sink.data_io.write_latency_ms"),
    ("DataIops", "sink.data_io.iops"),
    ("WritelogWaitMs", "sink.waits.WRITELOG.wait_time_ms"),
    ("LogRateGovernorWaitMs", "sink.waits.INSTANCE_LOG_RATE_GOVERNOR.wait_time_ms"),
    ("PageIoLatchWaitMs", "sink.pageiolatch_wait_ms"),
    ("LockWaitMs", "sink.lock_wait_ms"),
    ("CostPerLoad", "cost.cost_per_load"),
    ("CostPerGiB", "cost.cost_per_gib"),
    ("Correctness", "correctness.tester_result"),
    ("ReasonForNextAction", "outcome.reason_for_next_action"),
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """Return the current time in UTC. Isolated so tests can replace it."""
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Format a time as an ISO-8601 UTC string, for example 2026-11-11T21:30:00Z."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    """Parse a string produced by :func:`to_iso`."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_adf_time(text: str | None) -> datetime | None:
    """Parse a time from the ADF API, for example 2026-11-11T21:01:35.1234567Z.

    ADF can return up to seven digits of fractional seconds, more than Python
    reads directly, so the fraction is cut to six digits before parsing.
    Returns None for a missing or unreadable value.
    """
    if not text:
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$", str(text))
    if not match:
        return None
    base, fraction, zone = match.groups()
    moment = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    if fraction:
        moment = moment.replace(microsecond=int(fraction[:6].ljust(6, "0")))
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        offset = timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
        return (moment - sign * offset).replace(tzinfo=timezone.utc)
    return moment.replace(tzinfo=timezone.utc)


def validate_test_id(test_id: str) -> str:
    """Reject a test ID that could not be used safely as a folder name."""
    if not TEST_ID_PATTERN.match(test_id or ""):
        raise ValueError(
            "Test ID must start with a letter or digit and contain only letters, "
            "digits, '.', '_' or '-' (64 characters at most). Received: "
            f"{test_id!r}"
        )
    return test_id


def safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    """Divide, returning None instead of failing when a value is missing or zero."""
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def rounded(value: float | None, digits: int = 3) -> float | None:
    """Round a value for reporting, leaving a missing value as None."""
    return None if value is None else round(value, digits)


def as_int(value: Any) -> int | None:
    """Convert a database or JSON number to int, leaving a missing value as None."""
    return None if value is None else int(value)


def write_json(path: Path, payload: Any) -> None:
    """Write JSON with a stable layout so that files compare cleanly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    # utf-8-sig accepts files with or without a byte-order mark. Files saved
    # from a Windows editor or from ADF Studio often carry one.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dig(record: dict[str, Any], dotted_path: str) -> Any:
    """Read ``section.field`` from a nested dictionary, returning None when absent."""
    current: Any = record
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


# ---------------------------------------------------------------------------
# SQL sink snapshot
# ---------------------------------------------------------------------------

def _wait_type_list_sql() -> str:
    """Render the tracked wait types as a T-SQL IN list.

    The names come from a constant in this file, never from user input, so
    building the list as text is safe.
    """
    return ", ".join(f"N'{name}'" for name in TRACKED_WAIT_TYPES)


SQL_CONTEXT = """
SELECT
    CAST(SERVERPROPERTY('EngineEdition') AS int)            AS engine_edition,
    CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS product_version,
    DB_NAME()                                               AS database_name,
    d.recovery_model_desc                                   AS recovery_model,
    d.log_reuse_wait_desc                                   AS log_reuse_wait,
    CONVERT(varchar(40), SYSDATETIMEOFFSET(), 127)          AS sql_captured_at
FROM sys.databases AS d
WHERE d.database_id = DB_ID();
"""

SQL_WAITS_TEMPLATE = """
SELECT wait_type, waiting_tasks_count, wait_time_ms, signal_wait_time_ms
FROM {view}
WHERE wait_type IN ({wait_types});
"""

SQL_FILE_IO = """
SELECT
    mf.file_id,
    mf.name      AS logical_file_name,
    mf.type_desc AS file_type,
    vfs.num_of_reads,
    vfs.num_of_writes,
    vfs.num_of_bytes_read,
    vfs.num_of_bytes_written,
    vfs.io_stall_read_ms,
    vfs.io_stall_write_ms
FROM sys.dm_io_virtual_file_stats(DB_ID(), NULL) AS vfs
INNER JOIN sys.database_files AS mf
    ON mf.file_id = vfs.file_id
ORDER BY mf.file_id;
"""

SQL_LOG_SPACE = """
SELECT total_log_size_in_bytes, used_log_space_in_bytes, used_log_space_in_percent
FROM sys.dm_db_log_space_usage;
"""


def _fetch_dicts(connection: Any, sql: str) -> list[dict[str, Any]]:
    """Run a read-only query and return each row as a dictionary keyed by column name."""
    cursor = connection.cursor()
    cursor.execute(sql)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def capture_sql_snapshot(connection: Any, now: Callable[[], datetime] = utc_now) -> dict[str, Any]:
    """Read the cumulative SQL counters that describe load pressure on the sink.

    The values are cumulative since the instance or database last started, so
    one snapshot is meaningless alone. Two snapshots, taken before and after a
    load, are compared by :func:`compute_sql_deltas`.

    Captured:
    * wait statistics for the tracked wait types
    * read/write counts, bytes, and stall time for every database file
    * transaction-log size and usage
    * recovery model and the current log-reuse wait reason
    """
    context_rows = _fetch_dicts(connection, SQL_CONTEXT)
    if not context_rows:
        raise RuntimeError("Could not read database context. Is the connection pointed at a user database?")
    context = context_rows[0]
    engine_edition = as_int(context.get("engine_edition"))

    if engine_edition == ENGINE_EDITION_AZURE_SQL_DATABASE:
        wait_view, wait_scope = "sys.dm_db_wait_stats", "database"
    else:
        wait_view, wait_scope = "sys.dm_os_wait_stats", "instance"
    wait_rows = _fetch_dicts(
        connection,
        SQL_WAITS_TEMPLATE.format(view=wait_view, wait_types=_wait_type_list_sql()),
    )
    waits = {
        str(row["wait_type"]): {
            "waiting_tasks_count": int(row["waiting_tasks_count"]),
            "wait_time_ms": int(row["wait_time_ms"]),
            "signal_wait_time_ms": int(row["signal_wait_time_ms"]),
        }
        for row in wait_rows
    }

    files = [
        {
            "file_id": int(row["file_id"]),
            "logical_file_name": str(row["logical_file_name"]),
            "file_type": str(row["file_type"]),
            "num_of_reads": int(row["num_of_reads"]),
            "num_of_writes": int(row["num_of_writes"]),
            "num_of_bytes_read": int(row["num_of_bytes_read"]),
            "num_of_bytes_written": int(row["num_of_bytes_written"]),
            "io_stall_read_ms": int(row["io_stall_read_ms"]),
            "io_stall_write_ms": int(row["io_stall_write_ms"]),
        }
        for row in _fetch_dicts(connection, SQL_FILE_IO)
    ]

    log_rows = _fetch_dicts(connection, SQL_LOG_SPACE)
    log_space = None
    if log_rows:
        log_space = {
            "total_log_size_in_bytes": int(log_rows[0]["total_log_size_in_bytes"]),
            "used_log_space_in_bytes": int(log_rows[0]["used_log_space_in_bytes"]),
            "used_log_space_in_percent": float(log_rows[0]["used_log_space_in_percent"]),
        }

    return {
        "captured_at_utc": to_iso(now()),
        "sql_captured_at": str(context.get("sql_captured_at")),
        "engine_edition": engine_edition,
        "product_version": str(context.get("product_version")),
        "database_name": str(context.get("database_name")),
        "recovery_model": str(context.get("recovery_model")),
        "log_reuse_wait": str(context.get("log_reuse_wait")),
        "wait_stats_scope": wait_scope,
        "wait_stats": waits,
        "files": files,
        "log_space": log_space,
    }


def _counter_delta(before: int | None, after: int | None) -> tuple[int | None, bool]:
    """Subtract two cumulative counters.

    Returns ``(delta, reset)``. A cumulative counter can only grow. If the
    "after" value is smaller, the counter was reset between the snapshots
    (for example by a failover or restart), and no trustworthy delta exists.
    """
    before = before or 0
    after = after or 0
    if after < before:
        return None, True
    return after - before, False


def _summarise_file_group(deltas: Sequence[dict[str, Any]], elapsed_seconds: float | None) -> dict[str, Any]:
    """Combine per-file deltas into one summary for the data files or the log files."""
    usable = [d for d in deltas if not d["counters_reset"]]
    reads = sum(d["reads"] for d in usable)
    writes = sum(d["writes"] for d in usable)
    bytes_read = sum(d["bytes_read"] for d in usable)
    bytes_written = sum(d["bytes_written"] for d in usable)
    stall_read = sum(d["read_stall_ms"] for d in usable)
    stall_write = sum(d["write_stall_ms"] for d in usable)
    return {
        "file_count": len(deltas),
        "reads": reads,
        "writes": writes,
        "gib_read": rounded(bytes_read / BYTES_PER_GIB),
        "gib_written": rounded(bytes_written / BYTES_PER_GIB),
        # Load-specific latency: stall time accumulated during the load divided
        # by the I/O operations performed during the load. This is the
        # calculation described for script 06 in sql/monitoring/README.md.
        "read_latency_ms": rounded(safe_divide(stall_read, reads)),
        "write_latency_ms": rounded(safe_divide(stall_write, writes)),
        "write_mib_per_second": rounded(safe_divide(bytes_written / BYTES_PER_MIB, elapsed_seconds)),
        "iops": rounded(safe_divide(reads + writes, elapsed_seconds), 1),
    }


def compute_sql_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Work out what happened on the SQL sink between two snapshots.

    The result holds only differences attributable to the window between the
    snapshots. Anything else running on the instance in that window is
    included, which is why a test should run on an otherwise quiet sink and
    why the snapshots should be taken as close to the load as possible.
    """
    warnings: list[str] = []
    elapsed_seconds = (from_iso(after["captured_at_utc"]) - from_iso(before["captured_at_utc"])).total_seconds()
    if elapsed_seconds <= 0:
        warnings.append("The 'after' snapshot is not later than the 'before' snapshot; rates cannot be calculated.")
        elapsed_for_rates: float | None = None
    else:
        elapsed_for_rates = elapsed_seconds

    # --- waits -------------------------------------------------------------
    waits: dict[str, dict[str, Any]] = {}
    for wait_type in TRACKED_WAIT_TYPES:
        b = before.get("wait_stats", {}).get(wait_type, {})
        a = after.get("wait_stats", {}).get(wait_type, {})
        tasks, reset_tasks = _counter_delta(b.get("waiting_tasks_count"), a.get("waiting_tasks_count"))
        wait_ms, reset_wait = _counter_delta(b.get("wait_time_ms"), a.get("wait_time_ms"))
        signal_ms, reset_signal = _counter_delta(b.get("signal_wait_time_ms"), a.get("signal_wait_time_ms"))
        reset = reset_tasks or reset_wait or reset_signal
        waits[wait_type] = {
            "waiting_tasks": None if reset else tasks,
            "wait_time_ms": None if reset else wait_ms,
            "signal_wait_time_ms": None if reset else signal_ms,
            "counters_reset": reset,
        }
    if any(w["counters_reset"] for w in waits.values()):
        warnings.append(
            "Wait counters were reset between the snapshots (restart or failover?). "
            "Wait deltas for the affected types are not available."
        )

    def total_wait(wait_types: Sequence[str]) -> int | None:
        values = [waits[name]["wait_time_ms"] for name in wait_types]
        return None if any(v is None for v in values) else sum(values)

    # --- file I/O ----------------------------------------------------------
    before_files = {f["file_id"]: f for f in before.get("files", [])}
    file_deltas: list[dict[str, Any]] = []
    for a in after.get("files", []):
        b = before_files.get(a["file_id"])
        if b is None:
            warnings.append(f"File {a['logical_file_name']} was added during the test and is excluded from I/O deltas.")
            continue
        parts = {
            "reads": _counter_delta(b["num_of_reads"], a["num_of_reads"]),
            "writes": _counter_delta(b["num_of_writes"], a["num_of_writes"]),
            "bytes_read": _counter_delta(b["num_of_bytes_read"], a["num_of_bytes_read"]),
            "bytes_written": _counter_delta(b["num_of_bytes_written"], a["num_of_bytes_written"]),
            "read_stall_ms": _counter_delta(b["io_stall_read_ms"], a["io_stall_read_ms"]),
            "write_stall_ms": _counter_delta(b["io_stall_write_ms"], a["io_stall_write_ms"]),
        }
        reset = any(was_reset for _, was_reset in parts.values())
        delta = {name: (0 if reset else value) for name, (value, _) in parts.items()}
        delta.update({
            "file_id": a["file_id"],
            "logical_file_name": a["logical_file_name"],
            "file_type": a["file_type"],
            "counters_reset": reset,
            "write_latency_ms": None if reset else rounded(safe_divide(delta["write_stall_ms"], delta["writes"])),
            "read_latency_ms": None if reset else rounded(safe_divide(delta["read_stall_ms"], delta["reads"])),
        })
        file_deltas.append(delta)
    if any(f["counters_reset"] for f in file_deltas):
        warnings.append("File I/O counters were reset between the snapshots; affected files are excluded from totals.")

    log_files = [f for f in file_deltas if f["file_type"].upper() == "LOG"]
    data_files = [f for f in file_deltas if f["file_type"].upper() != "LOG"]

    # --- log usage ---------------------------------------------------------
    def log_usage(snapshot: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
        space = snapshot.get("log_space")
        if not space:
            return None, None, None
        return (
            rounded(space["used_log_space_in_bytes"] / BYTES_PER_GIB),
            rounded(space["used_log_space_in_percent"], 2),
            rounded(space["total_log_size_in_bytes"] / BYTES_PER_GIB),
        )

    used_before, pct_before, total_before = log_usage(before)
    used_after, pct_after, total_after = log_usage(after)
    if total_before is not None and total_after is not None and total_after > total_before:
        warnings.append(
            f"The transaction log grew during the test ({total_before} GiB to {total_after} GiB). "
            "Log growth makes this run hard to compare with runs that started with space already allocated."
        )

    if before.get("wait_stats_scope") == "instance":
        warnings.append(
            "Wait statistics are instance-wide. Any other workload on the instance during the test is included."
        )

    return {
        "collected": True,
        "database_name": after.get("database_name"),
        "engine_edition": after.get("engine_edition"),
        "product_version": after.get("product_version"),
        "recovery_model": after.get("recovery_model"),
        "measurement_window_seconds": rounded(elapsed_seconds, 1),
        "wait_stats_scope": before.get("wait_stats_scope"),
        "waits": waits,
        "lock_wait_ms": total_wait(LOCK_WAIT_TYPES),
        "pageiolatch_wait_ms": total_wait(PAGEIOLATCH_WAIT_TYPES),
        "log_io": _summarise_file_group(log_files, elapsed_for_rates),
        "data_io": _summarise_file_group(data_files, elapsed_for_rates),
        "files": file_deltas,
        "log_usage": {
            "used_gib_before": used_before,
            "used_gib_after": used_after,
            "used_percent_before": pct_before,
            "used_percent_after": pct_after,
            "total_gib_before": total_before,
            "total_gib_after": total_after,
            "log_reuse_wait_before": before.get("log_reuse_wait"),
            "log_reuse_wait_after": after.get("log_reuse_wait"),
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# ADF Copy activity result
# ---------------------------------------------------------------------------

def split_activity_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return ``(input, output, envelope)`` for either accepted JSON shape.

    Two shapes are accepted because both are easy to obtain:

    * the whole activity run, which has ``input`` and ``output`` sections
      (this is what the REST API returns);
    * the ``output`` section alone, which is what the "Output" link in the ADF
      Studio monitoring view shows.

    Only the whole activity run carries the *configured* settings, because
    they live in ``input``. With the output alone, configured values are
    recorded as unknown.
    """
    if not isinstance(payload, dict):
        raise ValueError("The ADF result must be a JSON object.")
    if isinstance(payload.get("output"), dict):
        activity_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        return activity_input, payload["output"], payload
    if "copyDuration" in payload or "dataRead" in payload or "rowsCopied" in payload:
        return {}, payload, {}
    raise ValueError(
        "The JSON does not look like a Copy activity result. Expected an activity run with an "
        "'output' section, or an output section containing 'copyDuration', 'dataRead' or 'rowsCopied'."
    )


def _parse_billing(reference: Any) -> Any:
    """ADF returns billingReference as a JSON string; decode it when possible."""
    if isinstance(reference, str):
        try:
            return json.loads(reference)
        except json.JSONDecodeError:
            return reference
    return reference


def summarise_copy_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the benchmark-relevant values from a Copy activity result.

    Property names follow Microsoft's "Monitor copy activity" documentation.
    ``executionDetails`` is read defensively because Microsoft notes that its
    layout may change; a missing value is recorded as None, never guessed.
    """
    activity_input, output, envelope = split_activity_payload(payload)
    details = output.get("executionDetails") or []
    first_detail = details[0] if details and isinstance(details[0], dict) else {}
    durations = first_detail.get("detailedDurations") or {}
    transfer_details = ((first_detail.get("profile") or {}).get("transfer") or {}).get("details") or {}
    sink_settings = activity_input.get("sink") if isinstance(activity_input.get("sink"), dict) else {}
    source_settings = activity_input.get("source") if isinstance(activity_input.get("source"), dict) else {}

    data_read = as_int(output.get("dataRead"))
    copy_duration = output.get("copyDuration")
    rows_copied = as_int(output.get("rowsCopied"))

    return {
        "activity_name": envelope.get("activityName"),
        "status": envelope.get("status") or first_detail.get("status"),
        "run_start": envelope.get("activityRunStart") or first_detail.get("start"),
        "run_end": envelope.get("activityRunEnd"),
        "source_type": (first_detail.get("source") or {}).get("type") or source_settings.get("type"),
        "sink_type": (first_detail.get("sink") or {}).get("type") or sink_settings.get("type"),
        "data_read_bytes": data_read,
        "data_written_bytes": as_int(output.get("dataWritten")),
        "source_gib": rounded(safe_divide(data_read, BYTES_PER_GIB)),
        "files_read": as_int(output.get("filesRead")),
        "files_written": as_int(output.get("filesWritten")),
        "rows_read": as_int(output.get("rowsRead")),
        "rows_copied": rows_copied,
        "rows_skipped": as_int(output.get("rowsSkipped")),
        "copy_duration_seconds": copy_duration,
        "throughput_kbps": output.get("throughput"),
        "throughput_mib_per_second": rounded(safe_divide(safe_divide(data_read, BYTES_PER_MIB), copy_duration)),
        "rows_per_second": rounded(safe_divide(rows_copied, copy_duration), 1),
        # Configured values come from the activity input. None means the value
        # was not set (ADF chose it) or the input section was not supplied.
        "configured_diu": activity_input.get("dataIntegrationUnits"),
        "used_diu": output.get("usedDataIntegrationUnits"),
        "configured_parallel_copies": activity_input.get("parallelCopies"),
        "used_parallel_copies": output.get("usedParallelCopies"),
        "source_peak_connections": output.get("sourcePeakConnections"),
        "sink_peak_connections": output.get("sinkPeakConnections"),
        "write_batch_size": sink_settings.get("writeBatchSize"),
        "table_lock": sink_settings.get("sqlWriterUseTableLock"),
        "partition_option": source_settings.get("partitionOption"),
        "effective_integration_runtime": output.get("effectiveIntegrationRuntime"),
        "queue_seconds": durations.get("queuingDuration"),
        "time_to_first_byte_seconds": durations.get("timeToFirstByte"),
        "transfer_seconds": durations.get("transferDuration"),
        "reading_from_source_seconds": (transfer_details.get("readingFromSource") or {}).get("workingDuration"),
        "writing_to_sink_seconds": (transfer_details.get("writingToSink") or {}).get("workingDuration"),
        "billing": _parse_billing(output.get("billingReference")),
        "errors": output.get("errors") or [],
        "performance_tips": [
            tip.get("Tip") for tip in (output.get("perfRecommendation") or []) if isinstance(tip, dict) and tip.get("Tip")
        ],
        "input_supplied": bool(activity_input),
    }


_CREDENTIAL: Any = None  # created on first use, then reused for the life of the process


def acquire_management_token() -> str:
    """Get an Azure management token for the person running the script.

    Uses the same credential-free approach as the rest of the toolkit: the
    token comes from the user's existing sign-in (Azure CLI, Azure PowerShell,
    Visual Studio Code, or an interactive browser), never from a stored secret.
    """
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Reading the ADF run from Azure requires azure-identity. Install it with: "
            "python -m pip install azure-identity   (or save the activity output from ADF Studio "
            "and pass it with --adf-output-file instead)."
        ) from exc
    global _CREDENTIAL
    if _CREDENTIAL is None:
        _CREDENTIAL = DefaultAzureCredential(
            exclude_environment_credential=True,
            exclude_managed_identity_credential=True,
        )
    # The credential object caches the token and renews it when it nears
    # expiry, so calling this before every status check keeps a long wait
    # signed in without asking the person to sign in again.
    return _CREDENTIAL.get_token(MANAGEMENT_SCOPE).token


def query_activity_runs(
    subscription_id: str,
    resource_group: str,
    factory: str,
    pipeline_run_id: str,
    window_start: datetime,
    window_end: datetime,
    token: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> list[dict[str, Any]]:
    """Return every activity run of one pipeline run, following result paging.

    ADF may return an empty page together with a continuation token, so the
    loop continues until no token is returned (see "REST continuation token
    NULL error" in Microsoft's Data Factory troubleshooting guide).
    """
    url = (
        f"{MANAGEMENT_ENDPOINT}/subscriptions/{urllib.parse.quote(subscription_id, safe='')}"
        f"/resourceGroups/{urllib.parse.quote(resource_group, safe='')}"
        f"/providers/Microsoft.DataFactory/factories/{urllib.parse.quote(factory, safe='')}"
        f"/pipelineruns/{urllib.parse.quote(pipeline_run_id, safe='')}"
        f"/queryActivityruns?api-version={DATA_FACTORY_API_VERSION}"
    )
    runs: list[dict[str, Any]] = []
    continuation: str | None = None
    for _ in range(100):  # hard stop so a service fault can never loop forever
        body: dict[str, Any] = {
            "lastUpdatedAfter": to_iso(window_start),
            "lastUpdatedBefore": to_iso(window_end),
        }
        if continuation:
            body["continuationToken"] = continuation
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with opener(request, timeout=60) as response:
                page = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"Azure returned HTTP {exc.code} when reading the pipeline run. "
                f"Check the subscription, resource group, factory name, run ID, and your access. Detail: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Azure management endpoint: {exc.reason}") from exc
        runs.extend(page.get("value") or [])
        continuation = page.get("continuationToken")
        if not continuation:
            return runs
    raise RuntimeError("Stopped reading activity runs after 100 pages; the result is incomplete.")


def select_copy_activity(runs: Sequence[dict[str, Any]], activity_name: str | None) -> dict[str, Any]:
    """Pick the one Copy activity run to record, or explain why that is not possible."""
    copies = [run for run in runs if str(run.get("activityType", "")).lower() == "copy"]
    if activity_name:
        copies = [run for run in copies if run.get("activityName") == activity_name]
    if len(copies) == 1:
        return copies[0]
    names = sorted({str(run.get("activityName")) for run in runs if str(run.get("activityType", "")).lower() == "copy"})
    if not copies:
        wanted = f" named {activity_name!r}" if activity_name else ""
        raise RuntimeError(
            f"No Copy activity{wanted} was found in the pipeline run. Copy activities present: {names or 'none'}."
        )
    raise RuntimeError(
        "The pipeline run contains more than one matching Copy activity run "
        f"({names}). Use --activity-name to choose one. If the same activity was retried, "
        "save the attempt you want from ADF Studio and pass it with --adf-output-file."
    )


# ---------------------------------------------------------------------------
# Waiting for the pipeline run to finish
# ---------------------------------------------------------------------------

class AzureCallError(RuntimeError):
    """A call to the Azure management API failed.

    ``retryable`` is True for faults that usually clear on their own, such as
    throttling, a server error, or a dropped connection. While waiting, those
    are retried; anything else (a wrong name, no access) stops at once.
    """

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _factory_url(subscription_id: str, resource_group: str, factory: str) -> str:
    return (
        f"{MANAGEMENT_ENDPOINT}/subscriptions/{urllib.parse.quote(subscription_id, safe='')}"
        f"/resourceGroups/{urllib.parse.quote(resource_group, safe='')}"
        f"/providers/Microsoft.DataFactory/factories/{urllib.parse.quote(factory, safe='')}"
    )


def _call_management_api(url: str, body: dict[str, Any] | None, token: str,
                         opener: Callable[..., Any]) -> dict[str, Any]:
    """Send one GET (no body) or POST (with a body) to the management API and return its JSON."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="GET" if body is None else "POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AzureCallError(
            f"Azure returned HTTP {exc.code}. Check the subscription, resource group, factory, "
            f"pipeline name or run ID, and your access. Detail: {detail}",
            retryable=exc.code in RETRYABLE_HTTP_CODES,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AzureCallError(f"Could not reach the Azure management endpoint: {reason}", retryable=True) from exc


def get_pipeline_run(subscription_id: str, resource_group: str, factory: str, run_id: str, token: str,
                     opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    """Read one pipeline run, including its current status, by run ID."""
    url = (
        f"{_factory_url(subscription_id, resource_group, factory)}"
        f"/pipelineruns/{urllib.parse.quote(run_id, safe='')}?api-version={DATA_FACTORY_API_VERSION}"
    )
    return _call_management_api(url, None, token, opener)


def find_pipeline_runs(subscription_id: str, resource_group: str, factory: str, pipeline_name: str,
                       window_start: datetime, window_end: datetime, token: str,
                       opener: Callable[..., Any] = urllib.request.urlopen) -> list[dict[str, Any]]:
    """List the runs of one pipeline updated within a time window, following result paging."""
    url = (
        f"{_factory_url(subscription_id, resource_group, factory)}"
        f"/queryPipelineRuns?api-version={DATA_FACTORY_API_VERSION}"
    )
    runs: list[dict[str, Any]] = []
    continuation: str | None = None
    for _ in range(100):  # hard stop so a service fault can never loop forever
        body: dict[str, Any] = {
            "lastUpdatedAfter": to_iso(window_start),
            "lastUpdatedBefore": to_iso(window_end),
            "filters": [{"operand": "PipelineName", "operator": "Equals", "values": [pipeline_name]}],
        }
        if continuation:
            body["continuationToken"] = continuation
        page = _call_management_api(url, body, token, opener)
        runs.extend(page.get("value") or [])
        continuation = page.get("continuationToken")
        if not continuation:
            return runs
    raise AzureCallError("Stopped reading pipeline runs after 100 pages; the result is incomplete.", retryable=False)


def choose_pipeline_run(runs: Sequence[dict[str, Any]], started_at: datetime) -> dict[str, Any] | None:
    """Pick the run that belongs to this test: the one that started after 'start'.

    Returns None while no such run exists yet, for example because the
    pipeline has not been started. If more than one run started after
    'start', the test is ambiguous, so the person must name the run with
    --pipeline-run-id.
    """
    earliest = started_at - timedelta(seconds=CLOCK_TOLERANCE_SECONDS)
    candidates = []
    for run in runs:
        run_start = parse_adf_time(run.get("runStart"))
        if run_start is not None and run_start >= earliest:
            candidates.append(run)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    listed = ", ".join(f"{run.get('runId')} (started {run.get('runStart')})" for run in candidates)
    raise AzureCallError(
        f"More than one run of the pipeline started after 'start': {listed}. "
        "Use --pipeline-run-id to name the run for this test.",
        retryable=False,
    )


def wait_for_pipeline_run(
    check: Callable[[], dict[str, Any] | None],
    poll_seconds: float,
    timeout_seconds: float,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Call ``check`` every ``poll_seconds`` until the pipeline run reaches a final status.

    ``check`` returns the pipeline run, or None while the run has not
    appeared yet. Each status change is logged when it happens, with a short
    "still waiting" line every ten minutes in between.

    Raises TimeoutError when ``timeout_seconds`` pass first, and
    AzureCallError when a check fails in a way that retrying will not fix, or
    fails too many times in a row.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    started = clock()
    deadline = started + timeout_seconds
    last_status: str | None = None
    last_heartbeat = started
    failures = 0
    while True:
        try:
            run = check()
            failures = 0
        except AzureCallError as exc:
            if not exc.retryable:
                raise
            failures += 1
            LOG.warning("Status check failed (%d in a row): %s", failures, exc)
            if failures >= MAX_CONSECUTIVE_CHECK_FAILURES:
                raise AzureCallError(
                    f"Stopped waiting after {failures} failed status checks in a row. "
                    "The 'before' snapshot is kept; run 'stop --wait' again when Azure is reachable.",
                    retryable=False,
                ) from exc
        else:
            status = "not started yet" if run is None else str(run.get("status"))
            if status != last_status:
                where = "" if run is None else f" (run {run.get('runId')})"
                LOG.info("Pipeline status: %s%s", status, where)
                last_status, last_heartbeat = status, clock()
            elif clock() - last_heartbeat >= WAIT_HEARTBEAT_SECONDS:
                LOG.info("Still waiting; status %s after %.0f minutes.", status, (clock() - started) / 60)
                last_heartbeat = clock()
            if run is not None and run.get("status") in FINAL_PIPELINE_STATUSES:
                return run
        if clock() >= deadline:
            raise TimeoutError(
                f"The pipeline did not finish within {timeout_seconds / 3600:g} hours "
                f"(last status: {last_status}). The 'before' snapshot is kept; run 'stop --wait' "
                "again to keep waiting, or raise --timeout-hours."
            )
        sleep(poll_seconds)


# ---------------------------------------------------------------------------
# Benchmark record
# ---------------------------------------------------------------------------

def build_benchmark_record(
    test: dict[str, Any],
    notes: dict[str, Any],
    copy: dict[str, Any],
    sink: dict[str, Any] | None,
    waited: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the benchmark record in the order of runbook section 11.

    ``test``   - identity of the test: ID, environment, times, who recorded it
    ``notes``  - what only the tester knows: what changed, the target table
                 design, cost, the correctness result, the verdict, and the
                 reason for the next action
    ``copy``   - output of :func:`summarise_copy_activity`
    ``sink``   - output of :func:`compute_sql_deltas`, or None when the sink is
                 not a SQL database or SQL collection was switched off
    ``waited`` - contents of pipeline_run.json when 'stop --wait' found the
                 run, or None when waiting was not used
    """
    warnings: list[str] = []

    if waited:
        pipeline_status = waited.get("status")
        if pipeline_status and pipeline_status != "Succeeded":
            warnings.append(f"The pipeline run finished with status {pipeline_status!r}, not 'Succeeded'.")
        lag = waited.get("snapshot_lag_seconds")
        poll = waited.get("poll_seconds") or DEFAULT_POLL_SECONDS
        # A lag much longer than one check interval means the waiting stopped
        # for a while, most often because the computer went to sleep.
        if lag is not None and lag > poll * 2 + 60:
            warnings.append(
                f"The 'after' snapshot was taken {lag:,.0f} s after the pipeline finished, much longer than "
                f"the {poll:g} s check interval. Was the computer asleep? Activity after the load may be included."
            )

    status = copy.get("status")
    if status and str(status).lower() != "succeeded":
        warnings.append(f"The Copy activity status is {status!r}, not 'Succeeded'.")
    if copy.get("errors"):
        warnings.append("The Copy activity reported errors; see adf_activity.json.")

    rows_read, rows_copied = copy.get("rows_read"), copy.get("rows_copied")
    rows_match = None if rows_read is None or rows_copied is None else rows_read == rows_copied
    if rows_match is False:
        warnings.append(f"Rows read ({rows_read:,}) do not equal rows copied ({rows_copied:,}).")
    if copy.get("rows_skipped"):
        warnings.append(f"{copy['rows_skipped']:,} incompatible rows were skipped.")
    if not copy.get("input_supplied"):
        warnings.append(
            "Configured DIU, parallel copies, and write batch size are unknown because only the activity "
            "output was supplied. Supply the whole activity run, or use the Azure options, to record them."
        )

    sink_section: dict[str, Any]
    if sink is None:
        sink_section = {"collected": False}
    else:
        sink_section = {key: value for key, value in sink.items() if key != "warnings"}
        warnings.extend(sink.get("warnings", []))
        window = sink.get("measurement_window_seconds")
        duration = copy.get("copy_duration_seconds")
        # The SQL numbers cover the whole window between 'start' and 'stop'. If
        # that window is much longer than the copy, unrelated activity may be
        # included. Sixty seconds of slack allows for normal operator delay.
        if window and duration and window > duration * 1.5 + 60:
            warnings.append(
                f"SQL measurements cover {window:,.0f} s but the copy ran {duration:,.0f} s. "
                "Run 'start' and 'stop' closer to the load so that other activity is not included."
            )

    cost_per_load = notes.get("cost_per_load")
    source_gib = copy.get("source_gib")

    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "test": test,
        "source": {
            "type": copy.get("source_type"),
            "gib": source_gib,
            "row_count": rows_read,
            "file_count": copy.get("files_read"),
        },
        "settings": {
            "configured_diu": copy.get("configured_diu"),
            "actual_diu": copy.get("used_diu"),
            "configured_parallel_copies": copy.get("configured_parallel_copies"),
            "actual_parallel_copies": copy.get("used_parallel_copies"),
            "source_peak_connections": copy.get("source_peak_connections"),
            "sink_peak_connections": copy.get("sink_peak_connections"),
            "write_batch_size": copy.get("write_batch_size"),
            "table_lock": copy.get("table_lock"),
            "partition_option": copy.get("partition_option"),
            "target_table_design": notes.get("target_table_design"),
            "sink_type": copy.get("sink_type"),
            "integration_runtime": copy.get("effective_integration_runtime"),
        },
        "result": {
            "status": status,
            "copy_duration_seconds": copy.get("copy_duration_seconds"),
            "throughput_mib_per_second": copy.get("throughput_mib_per_second"),
            "throughput_kbps": copy.get("throughput_kbps"),
            "rows_per_second": copy.get("rows_per_second"),
            "rows_copied": rows_copied,
            "rows_rejected": copy.get("rows_skipped"),
            "queue_seconds": copy.get("queue_seconds"),
            "time_to_first_byte_seconds": copy.get("time_to_first_byte_seconds"),
            "transfer_seconds": copy.get("transfer_seconds"),
            "reading_from_source_seconds": copy.get("reading_from_source_seconds"),
            "writing_to_sink_seconds": copy.get("writing_to_sink_seconds"),
            "adf_performance_tips": copy.get("performance_tips") or [],
        },
        "sink": sink_section,
        "cost": {
            "cost_per_load": cost_per_load,
            "cost_per_gib": rounded(safe_divide(cost_per_load, source_gib), 4),
            "adf_billing": copy.get("billing"),
        },
        "correctness": {
            "rows_read_equal_rows_copied": rows_match,
            "tester_result": notes.get("correctness"),
        },
        "outcome": {
            "result": notes.get("result"),
            "change_under_test": notes.get("change_under_test"),
            "reason_for_next_action": notes.get("reason_for_next_action"),
        },
        "warnings": warnings,
    }


def _show(value: Any, unit: str = "") -> str:
    """Format a value for the Markdown record. A missing value is shown, not hidden."""
    if value is None or value == "":
        return "not recorded"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        text = f"{value:,}"
    else:
        text = str(value)
    return f"{text} {unit}".strip()


def render_markdown(record: dict[str, Any]) -> str:
    """Render the record for people, using the field names of runbook section 11."""
    test, source, settings = record["test"], record["source"], record["settings"]
    result, sink, cost = record["result"], record["sink"], record["cost"]
    correctness, outcome = record["correctness"], record["outcome"]

    lines = [
        f"# Benchmark record: {test['test_id']}",
        "",
        f"**Result: {_show(outcome.get('result'))}**",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Test ID | {test['test_id']} |",
        f"| Environment | {_show(test.get('environment'))} |",
        f"| Started (UTC) | {_show(test.get('started_at_utc'))} |",
        f"| Stopped (UTC) | {_show(test.get('stopped_at_utc'))} |",
        f"| Recorded by | {_show(test.get('recorded_by'))} |",
        f"| Pipeline | {_show(test.get('pipeline_name'))} |",
        f"| Pipeline run ID | {_show(test.get('pipeline_run_id'))} |",
        f"| Pipeline status | {_show(test.get('pipeline_status'))} |",
        f"| Snapshot taken after the pipeline finished | {_show(test.get('snapshot_lag_seconds'), 's')} |",
        f"| Change under test | {_show(outcome.get('change_under_test'))} |",
        "",
        "## Source",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Source type | {_show(source.get('type'))} |",
        f"| Source size | {_show(source.get('gib'), 'GiB')} |",
        f"| Source row count | {_show(source.get('row_count'))} |",
        f"| Source file count | {_show(source.get('file_count'))} |",
        "",
        "## Settings",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Configured DIUs | {_show(settings.get('configured_diu'))} |",
        f"| Actual DIUs | {_show(settings.get('actual_diu'))} |",
        f"| Configured parallel copies | {_show(settings.get('configured_parallel_copies'))} |",
        f"| Actual parallel copies | {_show(settings.get('actual_parallel_copies'))} |",
        f"| Source peak connections | {_show(settings.get('source_peak_connections'))} |",
        f"| Sink peak connections | {_show(settings.get('sink_peak_connections'))} |",
        f"| Write batch size | {_show(settings.get('write_batch_size'))} |",
        f"| Table lock | {_show(settings.get('table_lock'))} |",
        f"| Partition option | {_show(settings.get('partition_option'))} |",
        f"| Target table design | {_show(settings.get('target_table_design'))} |",
        f"| Sink type | {_show(settings.get('sink_type'))} |",
        f"| Integration runtime | {_show(settings.get('integration_runtime'))} |",
        "",
        "## Copy result",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Status | {_show(result.get('status'))} |",
        f"| Copy duration | {_show(result.get('copy_duration_seconds'), 's')} |",
        f"| Throughput | {_show(result.get('throughput_mib_per_second'), 'MiB/s')} |",
        f"| Rows per second | {_show(result.get('rows_per_second'))} |",
        f"| Rows copied | {_show(result.get('rows_copied'))} |",
        f"| Rows rejected | {_show(result.get('rows_rejected'))} |",
        f"| Time in queue | {_show(result.get('queue_seconds'), 's')} |",
        f"| Time to first byte | {_show(result.get('time_to_first_byte_seconds'), 's')} |",
        f"| Transfer time | {_show(result.get('transfer_seconds'), 's')} |",
        f"| Reading from source | {_show(result.get('reading_from_source_seconds'), 's')} |",
        f"| Writing to sink | {_show(result.get('writing_to_sink_seconds'), 's')} |",
        "",
        "## SQL sink during the load",
        "",
    ]

    if not sink.get("collected"):
        lines += ["SQL measurements were not collected for this test.", ""]
    else:
        usage, log_io, data_io, waits = sink["log_usage"], sink["log_io"], sink["data_io"], sink["waits"]
        lines += [
            "| Field | Value |",
            "| --- | --- |",
            f"| Database | {_show(sink.get('database_name'))} |",
            f"| Measurement window | {_show(sink.get('measurement_window_seconds'), 's')} |",
            f"| Log used before | {_show(usage.get('used_gib_before'), 'GiB')} ({_show(usage.get('used_percent_before'), '%')}) |",
            f"| Log used after | {_show(usage.get('used_gib_after'), 'GiB')} ({_show(usage.get('used_percent_after'), '%')}) |",
            f"| Log reuse wait before / after | {_show(usage.get('log_reuse_wait_before'))} / {_show(usage.get('log_reuse_wait_after'))} |",
            f"| Log written | {_show(log_io.get('gib_written'), 'GiB')} |",
            f"| Observed log write rate | {_show(log_io.get('write_mib_per_second'), 'MiB/s')} |",
            f"| Log write latency | {_show(log_io.get('write_latency_ms'), 'ms per write')} |",
            f"| Data file IOPS | {_show(data_io.get('iops'))} |",
            f"| Data file write latency | {_show(data_io.get('write_latency_ms'), 'ms per write')} |",
            f"| Data file read latency | {_show(data_io.get('read_latency_ms'), 'ms per read')} |",
            f"| WRITELOG wait | {_show(waits['WRITELOG'].get('wait_time_ms'), 'ms')} |",
            f"| INSTANCE_LOG_RATE_GOVERNOR wait | {_show(waits['INSTANCE_LOG_RATE_GOVERNOR'].get('wait_time_ms'), 'ms')} |",
            f"| PAGEIOLATCH wait | {_show(sink.get('pageiolatch_wait_ms'), 'ms')} |",
            f"| Lock wait (LCK_M_X, LCK_M_IX, LCK_M_SCH_M) | {_show(sink.get('lock_wait_ms'), 'ms')} |",
            f"| Wait statistics scope | {_show(sink.get('wait_stats_scope'))} |",
            "",
            "### All tracked waits",
            "",
            "| Wait type | Waiting tasks | Wait time (ms) | Signal wait (ms) |",
            "| --- | --- | --- | --- |",
        ]
        for wait_type in TRACKED_WAIT_TYPES:
            w = waits[wait_type]
            lines.append(
                f"| {wait_type} | {_show(w.get('waiting_tasks'))} | {_show(w.get('wait_time_ms'))} | "
                f"{_show(w.get('signal_wait_time_ms'))} |"
            )
        lines.append("")

    lines += [
        "## Cost",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Cost per load | {_show(cost.get('cost_per_load'))} |",
        f"| Cost per GiB | {_show(cost.get('cost_per_gib'))} |",
        "",
        "## Correctness and outcome",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Rows read equal rows copied | {_show(correctness.get('rows_read_equal_rows_copied'))} |",
        f"| Correctness result | {_show(correctness.get('tester_result'))} |",
        f"| PASS / FAIL / INVESTIGATE | {_show(outcome.get('result'))} |",
        f"| Reason for the next action | {_show(outcome.get('reason_for_next_action'))} |",
        "",
    ]

    tips = result.get("adf_performance_tips") or []
    if tips:
        lines += ["## ADF performance tips", ""] + [f"- {tip}" for tip in tips] + [""]
    if record.get("warnings"):
        lines += ["## Warnings", ""] + [f"- {warning}" for warning in record["warnings"]] + [""]
    return "\n".join(lines)


def rebuild_index(output_dir: Path) -> Path:
    """Rewrite benchmark_index.csv from every benchmark record under ``output_dir``.

    The index is rebuilt from the records every time, never appended to. That
    keeps it correct when a test is recorded again or a test folder is
    deleted: the records are the source of truth and the index is a view.
    """
    records = []
    for record_path in sorted(output_dir.glob("*/benchmark_record.json")):
        try:
            records.append(read_json(record_path))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Skipping unreadable record %s: %s", record_path, exc)
    records.sort(key=lambda r: (str(dig(r, "test.started_at_utc") or ""), str(dig(r, "test.test_id") or "")))
    index_path = output_dir / "benchmark_index.csv"
    # utf-8-sig lets Excel on Windows open the file with the right encoding.
    with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([heading for heading, _ in INDEX_COLUMNS])
        for record in records:
            writer.writerow(["" if dig(record, path) is None else dig(record, path) for _, path in INDEX_COLUMNS])
    return index_path


# ---------------------------------------------------------------------------
# SQL connection
# ---------------------------------------------------------------------------

def open_sql_connection(server: str, database: str, auth: str, driver: str, encrypt: bool,
                        trust_server_certificate: bool) -> Any:
    """Connect to the sink using the toolkit's existing credential-free connection code.

    The connection logic lives in generate_delimited_file.py. Reusing it keeps
    one implementation of sign-in for the whole toolkit.
    """
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from generate_delimited_file import connect_sql_server

    return connect_sql_server(server, database, auth, driver, encrypt, trust_server_certificate)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _test_dir(args: argparse.Namespace) -> Path:
    return Path(args.output_dir) / validate_test_id(args.test_id)


def _attach_file_log(test_dir: Path) -> None:
    """Also write log messages to collector.log in the test folder."""
    test_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(test_dir / "collector.log", encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime  # log times in UTC, the same as the record
    handler.setFormatter(formatter)
    LOG.addHandler(handler)


def _ask(value: str | None, question: str, choices: Sequence[str] | None = None) -> str:
    """Return ``value``, or ask the person at the keyboard when it was not supplied.

    When the script is not attached to a terminal (for example in a scheduled
    job) it cannot ask, so a missing value is an error with a clear message.
    """
    if value:
        return value
    if not sys.stdin.isatty():
        raise ValueError(f"A required value was not supplied and no terminal is available to ask for it: {question}")
    while True:
        answer = input(f"{question}: ").strip()
        if not answer:
            continue
        if choices and answer.upper() not in choices:
            print(f"Please answer one of: {', '.join(choices)}")
            continue
        return answer.upper() if choices else answer


def command_start(args: argparse.Namespace) -> int:
    """Take the 'before' snapshot and open the test."""
    test_dir = _test_dir(args)
    before_path = test_dir / "before.json"
    if before_path.exists() and not args.force:
        raise FileExistsError(
            f"Test {args.test_id!r} already has a 'before' snapshot ({before_path}). "
            "Choose a new test ID, or pass --force to discard the earlier snapshot."
        )
    # --force starts the test again from nothing. Files left by the earlier
    # attempt are removed so that results from two attempts are never mixed.
    for stale in STALE_ON_RESTART:
        (test_dir / stale).unlink(missing_ok=True)
    _attach_file_log(test_dir)

    session: dict[str, Any] = {
        "test_id": args.test_id,
        "environment": args.environment,
        "started_at_utc": to_iso(utc_now()),
        "recorded_by": getpass.getuser(),
        "host": platform.node(),
        "sql": None,
        "snapshot": None,
    }

    if args.no_sql:
        LOG.info("SQL collection is switched off (--no-sql); no 'before' snapshot was taken.")
    else:
        if not args.server or not args.database:
            raise ValueError("--server and --database are required unless --no-sql is used.")
        # Connection details are kept so that 'stop' reads the same database.
        # They contain no password, key, or token.
        session["sql"] = {
            "server": args.server,
            "database": args.database,
            "auth": args.auth,
            "driver": args.driver,
            "encrypt": not args.no_encrypt,
            "trust_server_certificate": args.trust_server_certificate,
        }
        connection = open_sql_connection(**session["sql"])
        try:
            session["snapshot"] = capture_sql_snapshot(connection)
        finally:
            connection.close()
        LOG.info(
            "Captured 'before' snapshot of %s on %s (wait statistics scope: %s).",
            session["snapshot"]["database_name"], args.server, session["snapshot"]["wait_stats_scope"],
        )

    write_json(before_path, session)
    LOG.info("Test %s is open. Start the ADF pipeline now, then run 'stop' as soon as it finishes.", args.test_id)
    print(f"Test {args.test_id} started. Before-snapshot: {before_path}")
    return 0


def _load_copy_payload(args: argparse.Namespace, started_at: datetime, run_id: str | None) -> dict[str, Any]:
    """Obtain the Copy activity result from a file or from Azure.

    ``run_id`` is the pipeline run to read from Azure: the one given with
    --pipeline-run-id, or the one 'stop --wait' found.
    """
    if args.adf_output_file:
        payload = read_json(Path(args.adf_output_file))
        if isinstance(payload, dict) and isinstance(payload.get("value"), list):
            # A saved REST response: a list of activity runs.
            payload = select_copy_activity(payload["value"], args.activity_name)
        elif isinstance(payload, list):
            payload = select_copy_activity(payload, args.activity_name)
        LOG.info("Read the Copy activity result from %s.", args.adf_output_file)
        return payload

    if not all((args.subscription_id, args.resource_group, args.factory, run_id)):
        raise ValueError(
            "Supply the ADF result with --adf-output-file, or supply --subscription-id, --resource-group, "
            "--factory and --pipeline-run-id (or --wait with --pipeline-name) to read it from Azure."
        )
    token = acquire_management_token()
    # ADF requires a time window. A day either side of the test comfortably
    # contains the run; the pipeline run ID is what actually selects it.
    runs = query_activity_runs(
        args.subscription_id, args.resource_group, args.factory, run_id,
        started_at - timedelta(days=1), utc_now() + timedelta(days=1), token,
    )
    LOG.info("Read %d activity run(s) for pipeline run %s from Azure.", len(runs), run_id)
    return select_copy_activity(runs, args.activity_name)


def _validate_wait_arguments(args: argparse.Namespace) -> None:
    """Check the --wait options before anything is measured."""
    if not all((args.subscription_id, args.resource_group, args.factory)):
        raise ValueError("--wait needs --subscription-id, --resource-group and --factory to check the run's status.")
    if not (args.pipeline_run_id or args.pipeline_name):
        raise ValueError("--wait needs --pipeline-name (or --pipeline-run-id) to know which run to watch.")
    if args.poll_seconds < MINIMUM_POLL_SECONDS:
        raise ValueError(f"--poll-seconds must be at least {MINIMUM_POLL_SECONDS}.")
    if args.timeout_hours <= 0:
        raise ValueError("--timeout-hours must be greater than zero.")


def _wait_for_completion(args: argparse.Namespace, started_at: datetime) -> dict[str, Any]:
    """Watch the pipeline run in ADF until it finishes, and return it.

    Only the ADF management API is called. Nothing is read from the SQL sink
    while waiting, so the wait adds no load to the system being measured.
    """
    locked_run_id = args.pipeline_run_id

    def check() -> dict[str, Any] | None:
        nonlocal locked_run_id
        token = acquire_management_token()
        if locked_run_id:
            return get_pipeline_run(args.subscription_id, args.resource_group, args.factory, locked_run_id, token)
        runs = find_pipeline_runs(
            args.subscription_id, args.resource_group, args.factory, args.pipeline_name,
            started_at - timedelta(seconds=CLOCK_TOLERANCE_SECONDS), utc_now() + timedelta(hours=1), token,
        )
        run = choose_pipeline_run(runs, started_at)
        if run is not None:
            # From now on, follow this run by its ID, so a second run started
            # later cannot be mistaken for it.
            locked_run_id = str(run.get("runId"))
            LOG.info("Found pipeline run %s of %s, started %s.", locked_run_id, args.pipeline_name, run.get("runStart"))
        return run

    target = args.pipeline_run_id or f"the next run of {args.pipeline_name}"
    LOG.info(
        "Waiting for %s to finish. Checking every %g s, for up to %g hours. Press Ctrl+C to stop waiting; "
        "the 'before' snapshot is kept.", target, args.poll_seconds, args.timeout_hours,
    )
    return wait_for_pipeline_run(check, args.poll_seconds, args.timeout_hours * 3600)


def command_stop(args: argparse.Namespace) -> int:
    """Take the 'after' snapshot, read the ADF result, and write the benchmark record."""
    test_dir = _test_dir(args)
    before_path = test_dir / "before.json"
    record_path = test_dir / "benchmark_record.json"
    after_path = test_dir / "after.json"
    pipeline_run_path = test_dir / "pipeline_run.json"
    if not before_path.exists():
        raise FileNotFoundError(
            f"No 'before' snapshot found for test {args.test_id!r} ({before_path}). Run 'start' before the load."
        )
    if record_path.exists() and not args.force:
        raise FileExistsError(
            f"Test {args.test_id!r} already has a benchmark record ({record_path}). "
            "Pass --force to replace it."
        )
    if args.wait:
        _validate_wait_arguments(args)
    # Fail before touching anything if the verdict cannot be obtained: with no
    # terminal there is nobody to ask, so both values must be on the command line.
    # --snapshot-only does not write the record, so it needs no verdict.
    if not args.snapshot_only and not sys.stdin.isatty() and not (args.result and args.next_action_reason):
        raise ValueError(
            "--result and --next-action-reason are required when no terminal is available to ask for them."
        )
    _attach_file_log(test_dir)
    session = read_json(before_path)
    started_at = from_iso(session["started_at_utc"])
    has_sql = bool(session.get("sql") and session.get("snapshot"))

    # 1. Wait for the pipeline to finish, when asked to. If an 'after'
    #    snapshot already exists, the load has already been measured, so
    #    there is nothing to wait for.
    waited: dict[str, Any] | None = read_json(pipeline_run_path) if pipeline_run_path.exists() else None
    if args.wait:
        if has_sql and after_path.exists():
            LOG.info("An 'after' snapshot already exists, so waiting is skipped.")
        else:
            run = _wait_for_completion(args, started_at)
            finished_at = utc_now()
            waited = {
                "pipeline_name": run.get("pipelineName") or args.pipeline_name,
                "run_id": run.get("runId"),
                "status": run.get("status"),
                "run_start": run.get("runStart"),
                "run_end": run.get("runEnd"),
                "duration_ms": run.get("durationInMs"),
                "message": run.get("message"),
                "seen_finished_at_utc": to_iso(finished_at),
                "poll_seconds": args.poll_seconds,
            }
            write_json(pipeline_run_path, waited)
            if run.get("status") != "Succeeded":
                LOG.warning("The pipeline run finished with status %s: %s", run.get("status"), run.get("message"))

    # 2. Take the 'after' snapshot before anything slower, so the measurement
    #    window ends as close to the end of the load as possible. If one
    #    already exists (an earlier 'stop' failed part-way, or --snapshot-only
    #    was used) it is reused: a new one taken later would stretch the
    #    window beyond the load.
    sink_deltas: dict[str, Any] | None = None
    stopped_at = utc_now()
    if has_sql:
        if after_path.exists():
            after_snapshot = read_json(after_path)
            LOG.info("Reusing the 'after' snapshot taken at %s.", after_snapshot["captured_at_utc"])
        else:
            connection = open_sql_connection(**session["sql"])
            try:
                after_snapshot = capture_sql_snapshot(connection)
            finally:
                connection.close()
            write_json(after_path, after_snapshot)
        sink_deltas = compute_sql_deltas(session["snapshot"], after_snapshot)
        stopped_at = from_iso(after_snapshot["captured_at_utc"])
        LOG.info("'After' snapshot ready; measurement window %.0f s.", sink_deltas["measurement_window_seconds"])
    else:
        LOG.info("No SQL 'before' snapshot exists for this test; SQL measurements are not collected.")

    # How long after the pipeline finished the 'after' snapshot was taken.
    # Without SQL there is no snapshot, so there is no lag to report.
    if waited:
        run_end = parse_adf_time(waited.get("run_end"))
        waited["snapshot_lag_seconds"] = (
            round(max(0.0, (stopped_at - run_end).total_seconds()), 1) if has_sql and run_end else None
        )

    if args.snapshot_only:
        LOG.info("Stopped after the 'after' snapshot (--snapshot-only).")
        print(f"Test {args.test_id}: the 'after' snapshot is saved.")
        print("Save the Copy activity output from ADF Studio, then finish the record with:")
        print(f"  python scripts/collect_run_metrics.py stop --test-id {args.test_id} "
              "--adf-output-file <saved-file> --result <PASS|FAIL|INVESTIGATE> --next-action-reason \"...\"")
        return 0

    # 3. Read the Copy activity result.
    run_id = args.pipeline_run_id or (waited or {}).get("run_id")
    payload = _load_copy_payload(args, started_at, run_id)
    write_json(test_dir / "adf_activity.json", payload)
    copy = summarise_copy_activity(payload)

    # 4. Build and write the record.
    notes = {
        "change_under_test": args.change,
        "target_table_design": args.target_table_design,
        "cost_per_load": args.cost_per_load,
        "correctness": args.correctness,
        # The verdict and the reason for the next action are required because
        # the runbook treats them as the point of the record: they stop tuning
        # from becoming a sequence of unexplained setting changes.
        "result": _ask(args.result, "Result (PASS, FAIL or INVESTIGATE)", RESULT_CHOICES),
        "reason_for_next_action": _ask(args.next_action_reason, "Reason for the next action"),
    }
    test = {
        "test_id": session["test_id"],
        "environment": session.get("environment"),
        "started_at_utc": session["started_at_utc"],
        "stopped_at_utc": to_iso(stopped_at),
        "recorded_by": session.get("recorded_by"),
        "host": session.get("host"),
        "sql_server": (session.get("sql") or {}).get("server"),
        "pipeline_name": (waited or {}).get("pipeline_name"),
        "pipeline_run_id": run_id,
        "pipeline_status": (waited or {}).get("status"),
        "pipeline_run_end": (waited or {}).get("run_end"),
        "snapshot_lag_seconds": (waited or {}).get("snapshot_lag_seconds"),
    }
    record = build_benchmark_record(test, notes, copy, sink_deltas, waited)

    write_json(record_path, record)
    markdown_path = test_dir / "benchmark_record.md"
    markdown_path.write_text(render_markdown(record), encoding="utf-8")
    index_path = rebuild_index(Path(args.output_dir))

    for warning in record["warnings"]:
        LOG.warning(warning)
    LOG.info("Benchmark record written: %s", record_path)
    print(f"Benchmark record : {record_path}")
    print(f"Readable copy    : {markdown_path}")
    print(f"Index of all runs: {index_path}")
    print(f"Result           : {record['outcome']['result']}   Warnings: {len(record['warnings'])}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    """Rebuild the index and print one line per recorded test."""
    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"No benchmark records found under {output_dir}.")
        return 0
    index_path = rebuild_index(output_dir)
    with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print(f"No benchmark records found under {output_dir}.")
        return 0
    print(f"{'TestID':<24} {'Result':<12} {'Seconds':>9} {'MiB/s':>9} {'DIU':>5} {'Copies':>7}  Change under test")
    for row in rows:
        print(
            f"{row['TestID']:<24} {row['Result']:<12} {row['CopyDurationSeconds']:>9} "
            f"{row['ThroughputMiBPerSecond']:>9} {row['ActualDIU']:>5} {row['ActualParallelCopies']:>7}  "
            f"{row['ChangeUnderTest']}"
        )
    print(f"\nIndex file: {index_path}")
    return 0


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record one ADF load test as a benchmark record (runbook section 11).",
        epilog=("Run 'start' immediately before the ADF pipeline. Then either run 'stop' as soon as the "
                "pipeline finishes, or run 'stop --wait' straight away and let it watch the run."),
    )
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--output-dir", default=str(Path("output") / "benchmarks"),
                         help="Folder that holds one sub-folder per test (default: output/benchmarks)")

    start = commands.add_parser("start", help="Take the 'before' snapshot; run just before the ADF pipeline starts")
    add_common(start)
    start.add_argument("--test-id", required=True, help="Short unique name for this test, for example DIU-08-run1")
    start.add_argument("--environment", default="dev", help="Free-text environment label recorded with the test")
    start.add_argument("--server", help="SQL sink host name (SQL Server, SQL MI, or Azure SQL Database)")
    start.add_argument("--database", help="Sink database name")
    start.add_argument("--auth", choices=["windows", "entra-default", "entra-interactive"], default="windows",
                       help="Credential-free authentication mode for the SQL sink")
    start.add_argument("--driver", default="ODBC Driver 18 for SQL Server")
    start.add_argument("--no-encrypt", action="store_true", help="Disable encryption (not recommended)")
    start.add_argument("--trust-server-certificate", action="store_true",
                       help="Trust the presented server certificate; controlled development use only")
    start.add_argument("--no-sql", action="store_true",
                       help="Do not collect SQL measurements (use when the sink is not a SQL database)")
    start.add_argument("--force", action="store_true", help="Replace an existing 'before' snapshot for this test ID")
    start.set_defaults(handler=command_start)

    stop = commands.add_parser("stop", help="Take the 'after' snapshot and write the record; run just after the load")
    add_common(stop)
    stop.add_argument("--test-id", required=True, help="The test ID given to 'start'")
    source = stop.add_argument_group("where the ADF Copy activity result comes from (choose one way)")
    source.add_argument("--adf-output-file", help="JSON saved from ADF Studio: the activity output or the whole activity run")
    source.add_argument("--subscription-id", help="Azure subscription that holds the data factory")
    source.add_argument("--resource-group", help="Resource group of the data factory")
    source.add_argument("--factory", help="Data factory name")
    source.add_argument("--pipeline-run-id", help="Run ID of the pipeline run that performed the load")
    source.add_argument("--activity-name", help="Copy activity name; needed only when the pipeline has more than one")
    waiting = stop.add_argument_group("waiting for the pipeline to finish (checks status in ADF only)")
    waiting.add_argument("--wait", action="store_true",
                         help="Watch the pipeline run in ADF and take the 'after' snapshot the moment it finishes")
    waiting.add_argument("--pipeline-name", help="Pipeline to watch; the run of it that started after 'start' is followed")
    waiting.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS,
                         help=f"Seconds between status checks (default {DEFAULT_POLL_SECONDS}, "
                              f"minimum {MINIMUM_POLL_SECONDS})")
    waiting.add_argument("--timeout-hours", type=float, default=DEFAULT_TIMEOUT_HOURS,
                         help=f"Stop waiting after this many hours (default {DEFAULT_TIMEOUT_HOURS:g})")
    waiting.add_argument("--snapshot-only", action="store_true",
                         help="Stop after the 'after' snapshot; finish later with --adf-output-file")
    notes = stop.add_argument_group("what the tester knows")
    notes.add_argument("--change", help="What was changed for this test, for example 'DIU 4 -> 8'")
    notes.add_argument("--target-table-design", help="For example 'heap, no indexes' or 'clustered index + 2 nonclustered'")
    notes.add_argument("--cost-per-load", type=float, help="Cost of this load, when known")
    notes.add_argument("--correctness", help="Correctness result, for example 'row counts and checksums match'")
    notes.add_argument("--result", choices=RESULT_CHOICES, help="Verdict for this test (asked for if omitted)")
    notes.add_argument("--next-action-reason", help="Why the evidence leads to the next test or action (asked for if omitted)")
    stop.add_argument("--force", action="store_true", help="Replace an existing benchmark record for this test ID")
    stop.set_defaults(handler=command_stop)

    listing = commands.add_parser("list", help="Rebuild the index and list every recorded test")
    add_common(listing)
    listing.set_defaults(handler=command_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(format="%(levelname)s: %(message)s")
    # Set the level explicitly: basicConfig only takes effect the first time it
    # is called in a process, so it cannot be relied on to change the level.
    LOG.setLevel(getattr(logging, args.log_level))
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        # Ctrl+C, usually while waiting. Nothing is lost: the 'before'
        # snapshot stays on disk, so 'stop' can simply be run again.
        LOG.warning("%s was stopped with Ctrl+C. Saved snapshots are kept; run it again to continue.", args.command)
        return 130
    except Exception as exc:  # one clear message, no stack trace, non-zero exit code
        LOG.error("%s failed: %s", args.command, exc)
        return 2
    finally:
        # Release collector.log so the test folder can be moved or deleted.
        for handler in list(LOG.handlers):
            if isinstance(handler, logging.FileHandler):
                handler.close()
                LOG.removeHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())
