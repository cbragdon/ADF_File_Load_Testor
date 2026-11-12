# Handoff: Run Collector (`collect_run_metrics.py`)

For anyone picking up the run collector in ADF_Performance_Toolkit, including
AI coding assistants such as GitHub Copilot.

This file covers the run collector only. The operator guide,
[`collect_run_metrics.md`](collect_run_metrics.md), explains how to run it and
how each command works step by step. This handoff adds what a developer needs
to change it safely: the design constraints, a map of the code, how to extend
it, and what is still unverified.

## Using this file with GitHub Copilot

- **Automatically:** this repository's `.github/copilot-instructions.md`
  carries the rules below, and Copilot reads it in every chat here.
- **In Copilot Chat:** for deeper work, start a session with
  `#file:scripts/collect_run_metrics_handoff.md`,
  `#file:scripts/collect_run_metrics.py`, and
  `#file:scripts/collect_run_metrics.md`.

## What it is

A command-line script that records one Azure Data Factory (ADF) load test as a
**benchmark record**, the evidence the tuning runbook asks for in section 11
(`docs/ADF_Copy_Activity_Performance_Tuning_Runbook.md`).

| Command | When | What it does |
| --- | --- | --- |
| `start` | Immediately before the ADF pipeline | Takes a "before" snapshot of the SQL sink and opens the test |
| `stop` | Immediately after the pipeline finishes | Takes the "after" snapshot, calculates the differences, reads the Copy activity result, writes the record |
| `list` | Any time | Rebuilds the index of all records and prints one line per test |

Two commands are needed because SQL counters are cumulative: only the
difference between a reading before and a reading after describes one load.

**Why it exists.** No benchmark records existed. The records it produces are
the evidence for refining copy settings, both by hand with the runbook and
later in a planned companion tool, adf-kickstart, that sizes new pipelines.
Neither reads anything from this script at run time; they share only the
record format.

## Status

| Item | State |
| --- | --- |
| `scripts/collect_run_metrics.py` | Complete |
| `scripts/collect_run_metrics.md` | Complete operator guide, including a first-run checklist and a "How it works" section |
| `tests/test_collect_run_metrics.py` | 31 tests, all passing; the toolkit's existing 14 also pass |
| Embedded T-SQL | Parses cleanly with `sqlfluff --dialect tsql` |
| Live verification | **Not done.** Never run against a real SQL sink or data factory |
| Links to the collector | Added to the toolkit README, runbook section 11, the monitoring guide, and the tests guide |

## Files

| File | Purpose |
| --- | --- |
| `scripts/collect_run_metrics.py` | The collector (about 1,360 lines, heavily commented) |
| `scripts/collect_run_metrics.md` | Operator guide |
| `scripts/collect_run_metrics_handoff.md` | This file |
| `tests/test_collect_run_metrics.py` | Unit and end-to-end tests |
| `scripts/generate_delimited_file.py` | Existing toolkit file; the collector imports its `connect_sql_server` |

## Design constraints

Open an issue before changing any of these. Each one protects how far a record
can be trusted.

- **Read-only against SQL.** Only `SELECT` from system views. A test asserts
  every query starts with `SELECT` and contains no write keywords.
- **Credential-free.** SQL and Azure use the signed-in Windows or Microsoft
  Entra identity. No password, key, or token is accepted on the command line or
  written to disk. `before.json` stores connection details only.
- **One sign-in implementation.** SQL connections go through
  `connect_sql_server` in `generate_delimited_file.py`. That is why the
  collector must stay in the toolkit's `scripts` folder.
- **Same wait types as the manual script.** `TRACKED_WAIT_TYPES` matches
  `sql/monitoring/07_capture_load_wait_stats.sql`, so automatic and manual
  captures are comparable. Change both together.
- **Field names follow runbook section 11**, and ADF property names follow
  Microsoft's "Monitor copy activity" documentation.
- **Missing is never guessed.** A value the source does not provide is
  recorded as `None` (shown as "not recorded"), never estimated.
- **Counter resets are reported**, never turned into negative numbers.
- **Never overwrite silently.** `start` and `stop` refuse to replace results
  without `--force`; `start --force` deletes the earlier attempt's results.
- **The verdict is mandatory.** `--result` and `--next-action-reason` are
  required (or asked for). The runbook treats the reason for the next action as
  the point of the record.
- **Standard library first.** Only `pyodbc` (SQL) and `azure-identity`
  (Azure sign-in) are third-party, and both are imported only when needed, so
  `--no-sql` with a saved ADF file needs neither.
- **Python 3.9 or later.** The file uses `from __future__ import annotations`
  so the `X | None` annotations work on 3.9.

## Code map

The script reads top to bottom in the same order as a test run.

| Area | Functions and constants | Responsibility |
| --- | --- | --- |
| Constants | `RECORD_SCHEMA_VERSION`, `TRACKED_WAIT_TYPES`, `LOCK_WAIT_TYPES`, `PAGEIOLATCH_WAIT_TYPES`, `TEST_ID_PATTERN`, `DATA_FACTORY_API_VERSION`, `STALE_ON_RESTART`, `INDEX_COLUMNS` | Everything a maintainer is likely to change |
| Helpers | `utc_now`, `to_iso`, `from_iso`, `validate_test_id`, `safe_divide`, `rounded`, `as_int`, `write_json`, `read_json`, `dig` | Time, safe arithmetic, JSON, nested lookup |
| SQL snapshot | `SQL_CONTEXT`, `SQL_WAITS_TEMPLATE`, `SQL_FILE_IO`, `SQL_LOG_SPACE`, `_fetch_dicts`, `capture_sql_snapshot` | Read the cumulative counters; pick the wait view by engine edition |
| SQL differences | `_counter_delta`, `_summarise_file_group`, `compute_sql_deltas` | After minus before; latency and rates; reset, growth, and scope warnings |
| ADF result | `split_activity_payload`, `_parse_billing`, `summarise_copy_activity` | Accept output-only or whole-run JSON; extract benchmark values |
| Azure REST | `acquire_management_token`, `query_activity_runs`, `select_copy_activity` | Token from the user's sign-in; paged activity-run query; choose exactly one Copy activity |
| Record | `build_benchmark_record`, `_show`, `render_markdown`, `rebuild_index` | Assemble the record and its warnings; Markdown copy; CSV index |
| SQL connection | `open_sql_connection` | Import and call the toolkit's `connect_sql_server` |
| Commands | `command_start`, `_load_copy_payload`, `command_stop`, `command_list`, `_ask`, `_attach_file_log`, `_test_dir` | The three commands |
| Command line | `build_parser`, `main` | Arguments; one error message and exit code 2 on any failure |

### Test layout

`tests/test_collect_run_metrics.py` uses fakes, so it needs no database, Azure
access, or third-party package.

- `FakeConnection` and `FakeCursor` return prepared rows chosen by which
  system view the query names. An unexpected query fails the test.
- `FakeResponse` and an `opener` argument replace the Azure HTTP call.
- `sql_data()` builds one snapshot's rows; `snapshot_pair()` builds a before
  and after pair 100 seconds apart with known differences.
- `ACTIVITY_RUN` is a full Copy activity run in the REST API's shape.
- `CommandLineTests` runs `main()` end to end in a temporary folder.
- Logging is disabled for the test module so the report stays readable.

Run them from the repository root:

```powershell
python -m unittest discover -s tests -v
```

## How to extend it

| Change | What to do |
| --- | --- |
| Track another wait type | Add it to `TRACKED_WAIT_TYPES` and to script 07; add it to `LOCK_WAIT_TYPES` or `PAGEIOLATCH_WAIT_TYPES` if it belongs in a total; extend `sql_data()` in the tests |
| Add a record field | Add it in `build_benchmark_record` and `render_markdown`; add an `INDEX_COLUMNS` entry if it belongs in the CSV; add a test; update the guide's mapping table |
| Rename or remove a record field | Do the above **and** increase `RECORD_SCHEMA_VERSION`, because `rebuild_index` reads every older record |
| Read another ADF output property | Extract it in `summarise_copy_activity` with `.get()` so a missing property becomes `None`; do not parse `executionDetails` beyond what is already there, because Microsoft says its layout may change |
| Add a record warning | Append to `warnings` in `compute_sql_deltas` (SQL) or `build_benchmark_record` (everything else); add a row to the guide's warnings table |
| Change the long-window warning | It fires when the SQL window exceeds 1.5 × copy duration + 60 seconds, in `build_benchmark_record` |
| Support another sink type | Not SQL: use `--no-sql`. Another SQL platform: check its engine edition and which wait view it permits in `capture_sql_snapshot` |

## Known limits

- **One Copy activity per record.** Pipelines with several need
  `--activity-name`; retried activities need a saved file of the chosen attempt.
- **Instance-wide waits** on SQL Server and SQL Managed Instance include all
  workloads on the instance during the window.
- **The window is start-to-stop**, measured by the clock of the machine running
  the collector, not only the copy's duration.
- **Copy activity loads only.**
- **The Azure query window** runs from one day before `start` to one day after
  `stop`. ADF requires a window; the pipeline run ID is what selects the run.

## Verification still needed

Before the records are relied on, do one live run and check it by hand:

1. In a test environment, record a test with `start` and `stop`.
2. Around the same load, run `sql/monitoring` scripts 02, 06, and 07 manually.
3. Compare their deltas with the `sink` section of `benchmark_record.json`.
4. Compare the ADF values with the Copy activity's monitoring view.
5. Confirm on Azure SQL Database that `sys.dm_db_wait_stats` and
   `sys.dm_io_virtual_file_stats` are readable with the granted permission.
6. Confirm the Azure option reads the run with the team's sign-in, including
   the tenant your test subscription belongs to.

Record any difference found as a bug, with the record and the manual output
attached.

## Possible next work

Not started. Open an issue to discuss either one before building it.

- Apply the runbook's baseline routing table (section 2) to a record and
  propose the smallest next test, with its reason.
- Compare two records directly (for example `compare <test-a> <test-b>`) and
  report the change in throughput and in the main waits against the runbook's
  10% improvement threshold.

## Rules for changing the collector

- Keep every SQL statement a read-only `SELECT` against system views.
- Never accept, store, or log a credential or token.
- Keep the wait-type list identical to `sql/monitoring/07_capture_load_wait_stats.sql`.
- Record a missing value as `None`; never estimate one.
- Never overwrite a result without `--force`.
- Increase `RECORD_SCHEMA_VERSION` when a record field is renamed or removed.
- Keep tests free of databases, network access, and third-party packages.
- Update `collect_run_metrics.md` whenever behaviour, arguments, output, or
  permissions change, written for operators: what it does, why it matters, and
  how to verify it.
