# Run Collector

`collect_run_metrics.py` records one Azure Data Factory (ADF) load test as a
**benchmark record**: the evidence the
[tuning runbook](../docs/ADF_Copy_Activity_Performance_Tuning_Runbook.md)
asks you to keep for every meaningful test (section 11).

Before this script, the record was assembled by hand from the ADF monitoring
view and several SQL monitoring scripts. The collector produces the same record
automatically and in the same shape every time, so that test runs can be
compared with each other and used later to refine copy settings.

## What it does

A test is recorded with two commands, one on each side of the ADF run.

| Step | Command | When | What it does |
| --- | --- | --- | --- |
| 1 | `start` | Immediately before the ADF pipeline starts | Takes a "before" snapshot of the SQL sink and opens the test |
| 2 | (run the ADF pipeline) | | |
| 3 | `stop` | Immediately after the ADF pipeline finishes | Takes the "after" snapshot, calculates what changed, reads the Copy activity result, and writes the record |

Two commands are needed because the SQL counters are cumulative. One reading
says nothing about one load. Only the difference between a reading taken before
the load and a reading taken after it describes the load.

The script is read-only against SQL. It selects from system views and changes
nothing.

## Requirements

- Python 3.9 or later
- For SQL measurements: `pyodbc` and Microsoft ODBC Driver 18 for SQL Server
- `azure-identity`, for two things: reading the ADF result directly from
  Azure, and signing in to SQL with `--auth entra-default`. It is not needed
  when you save the ADF result to a file and sign in to SQL with `windows` or
  `entra-interactive`.

```powershell
python -m pip install pyodbc azure-identity
```

### Permissions

| Where | What you need | Why |
| --- | --- | --- |
| SQL Server and SQL Managed Instance | Permission to view server state | Wait statistics and file I/O counters are server-level views |
| Azure SQL Database | Permission to view database state | The database-scoped wait and I/O views are used |
| Data factory (Azure option only) | Read access to the factory's pipeline runs | To read the Copy activity run |

These are the same permissions the scripts in `sql\monitoring\` need. The
collector accepts no password, key, or token. SQL and Azure access use your
Windows or Microsoft Entra sign-in.

## First run in your environment

Do these steps once on each machine, before recording the first test.

### 1. Use the repository's virtual environment

The repository keeps its Python packages in a `.venv` folder so they do not
mix with the system Python. Activate it before installing anything, and again
in every new PowerShell window before running the collector.

```powershell
cd <path-to-your-clone>\ADF_Performance_Toolkit
.\.venv\Scripts\Activate.ps1
python -m pip install pyodbc azure-identity
```

If `.venv` does not exist yet, create it first with `python -m venv .venv`.
If PowerShell refuses to run `Activate.ps1`, allow local scripts for this
window only with
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`.

`requirements.txt` lists only `pyodbc`, so `azure-identity` must be installed
as shown.

### 2. Confirm the ODBC driver

The collector connects with Microsoft ODBC Driver 18 for SQL Server by
default. Check that it is installed:

```powershell
Get-OdbcDriver -Name "ODBC Driver 18 for SQL Server"
```

If it returns nothing, install the driver, or name the one you have with
`--driver` on the `start` command.

### 3. Sign in to the right tenant

Two parts of the collector use your existing Azure sign-in: reading the ADF
result from Azure, and `--auth entra-default` for SQL. They pick up the
sign-in from the Azure CLI, Azure PowerShell, or Visual Studio Code.

If your account can reach more than one Microsoft Entra tenant, sign in to the
tenant that holds the subscription you test in. Otherwise Azure returns HTTP
401 or 403 even though the subscription ID you pass is correct.

```powershell
az login --tenant <tenant-id>
az account set --subscription <test-subscription-id>
```

or, with Azure PowerShell:

```powershell
Connect-AzAccount -Tenant <tenant-id> -Subscription <test-subscription-id>
```

### 4. Check that the sink is reachable

`start` connects to the sink straight away, so a connection problem shows up
before the load begins. If the sink sits behind a private endpoint, connect
the VPN or work from a machine inside the network first.

### 5. Run the tests

```powershell
python -m unittest discover -s tests -v
```

All tests should pass. They need no database or Azure access, so a failure
here points to the Python setup, not to your environment.

### 6. Record one trial test and check it by hand

For the first real test, record the run with the collector and, around the
same load, run scripts 02, 06, and 07 from `sql\monitoring\` by hand. Then
compare the two, as described under [Verification status](#verification-status).
Use the saved-file option for the ADF result on this first run: it needs no
Azure sign-in, so any difference you find comes from the SQL side.

## Record a test

Run the commands from the repository root.

### 1. Start, immediately before the load

```powershell
python .\scripts\collect_run_metrics.py start `
    --test-id DIU-08-run1 `
    --server sqlmi-dev.example.database.windows.net `
    --database LoadTest `
    --auth entra-default
```

The script prints where the "before" snapshot was saved. Start the ADF pipeline
as soon as it returns.

`--auth` accepts `windows` (default), `entra-default`, or `entra-interactive`,
exactly as the file generator does.

### 2. Run the ADF pipeline

Let the pipeline finish. Keep other work off the sink while it runs (see
[Limits](#limits)).

### 3. Stop, immediately after the load

Choose one of two ways to give the script the Copy activity result.

**Option A: read the run from Azure.** Copy the pipeline run ID from the ADF
Studio Monitor tab.

```powershell
python .\scripts\collect_run_metrics.py stop `
    --test-id DIU-08-run1 `
    --subscription-id 00000000-0000-0000-0000-000000000000 `
    --resource-group rg-data-dev `
    --factory adf-data-dev `
    --pipeline-run-id 11111111-2222-3333-4444-555555555555 `
    --change "DIU 4 -> 8" `
    --target-table-design "heap, no indexes" `
    --correctness "row counts match" `
    --result PASS `
    --next-action-reason "Throughput rose 30% with no log governor waits; test 16 DIU next."
```

**Option B: use a file saved from ADF Studio.** In the Monitor tab, open the
pipeline run, select the Copy activity's **Output** link, and save the JSON to
a file.

```powershell
python .\scripts\collect_run_metrics.py stop `
    --test-id DIU-08-run1 `
    --adf-output-file .\output\adf\DIU-08-run1.json `
    --change "DIU 4 -> 8" `
    --result PASS `
    --next-action-reason "Throughput rose 30% with no log governor waits; test 16 DIU next."
```

Option A records the *configured* DIUs, parallel copies, and write batch size
as well as the actual ones, because it reads the activity's input. The Output
link alone does not contain the configured values, so with Option B they are
recorded as "not recorded" and the record carries a warning. To get them with
Option B, save a file containing both sections:

```json
{ "input": { ...the activity's Input JSON... }, "output": { ...the activity's Output JSON... } }
```

`--result` and `--next-action-reason` are required. If you leave them out, the
script asks for them. The runbook treats the reason for the next action as the
point of the record: it stops tuning from becoming a sequence of unexplained
setting changes.

### 4. Review

```powershell
python .\scripts\collect_run_metrics.py list
```

This prints one line per recorded test and rebuilds the index file.

## What is written

Everything is written under `output\benchmarks\`, which the repository
excludes from Git. Use `--output-dir` to write somewhere else.

| File | Contents |
| --- | --- |
| `<test-id>\before.json` | The "before" SQL snapshot and how to reconnect for `stop` (no secrets) |
| `<test-id>\after.json` | The "after" SQL snapshot |
| `<test-id>\adf_activity.json` | The Copy activity result exactly as received |
| `<test-id>\benchmark_record.json` | The benchmark record, for programs |
| `<test-id>\benchmark_record.md` | The same record, for people |
| `<test-id>\collector.log` | What the script did and when, in UTC |
| `benchmark_index.csv` | One row per recorded test, for comparing a series in a spreadsheet |

The index is rebuilt from the records every time. The records are the source of
truth, so deleting a test folder and running `list` removes it from the index.

Records name your SQL server and database, so treat them as internal
information. They stay out of Git because `output\` is excluded; review a
record before sharing it outside your organization.

`--output-dir` is relative to the folder you run the command from. Run all
three commands from the repository root, or pass the same absolute
`--output-dir` to each, so that `stop` finds what `start` wrote.

## How it works

This section describes exactly what each command does, so the collector can
be checked and changed without reading the code first.

### What `start` does

1. **Checks the test ID.** The ID becomes a folder name, so it must start with
   a letter or digit and contain only letters, digits, `.`, `_`, or `-`, up to
   64 characters.
2. **Refuses to overwrite.** If the test folder already holds a `before.json`,
   the command stops. With `--force` it begins the test again and first deletes
   `after.json`, `adf_activity.json`, and both record files, so results from
   two attempts are never mixed.
3. **Opens `collector.log`** in the test folder. Every later step is logged
   there with a UTC time.
4. **Records who and where:** the test ID, the environment label, the UTC
   start time, the Windows user name, and the computer name.
5. **Connects to the sink**, unless `--no-sql` was given, using the
   toolkit's existing connection code in `generate_delimited_file.py`. There is
   one sign-in implementation for the whole toolkit.
6. **Takes the "before" snapshot** (see
   [What a snapshot reads](#what-a-snapshot-reads)) and closes the connection.
   No connection is held open during the load.
7. **Writes `before.json`.** It holds the snapshot and the connection details
   `stop` needs to reach the same database: server, database, sign-in mode,
   driver, and encryption settings. It holds no password, key, or token,
   because the collector never receives one.

### What `stop` does

1. **Checks that `start` ran.** Without a `before.json` for the test ID, the
   command stops.
2. **Refuses to overwrite** an existing benchmark record unless `--force` is
   given.
3. **Checks for the verdict first.** When no terminal is attached, for
   example in a scheduled job, `--result` and `--next-action-reason` must be on
   the command line. This check runs before anything is measured, so a missing
   value cannot leave a half-finished test behind.
4. **Takes the "after" snapshot before anything slower.** Reading ADF from
   Azure can take several seconds, so the snapshot comes first. That keeps the
   SQL measurement window as close to the end of the load as possible.
   - If an `after.json` already exists because an earlier `stop` failed
     part-way, it is reused. Taking a new snapshot later would stretch the
     window beyond the load.
5. **Calculates the differences** between the two snapshots (see
   [How the differences are calculated](#how-the-differences-are-calculated)).
6. **Obtains the Copy activity result,** from the file or from Azure (see
   [How the ADF result is read](#how-the-adf-result-is-read)), and saves it
   unchanged as `adf_activity.json`.
7. **Extracts the benchmark values** from the result, using the property
   names in Microsoft's "Monitor copy activity" documentation. Any value the
   result does not contain is recorded as "not recorded", never estimated.
8. **Asks for the verdict and reason** if they were not on the command line.
9. **Builds the record**, adds its warnings, and writes
   `benchmark_record.json` and `benchmark_record.md`.
10. **Rebuilds `benchmark_index.csv`** from every record in the output
    folder, sorted by start time.

### What `list` does

It rebuilds `benchmark_index.csv` from the records and prints one line per
test: ID, result, copy duration, throughput, DIUs used, parallel copies used,
and the change under test. It connects to nothing.

### What a snapshot reads

Every query is a `SELECT` against a system view. Nothing is changed.

| Query | System view | What is kept |
| --- | --- | --- |
| Context | `SERVERPROPERTY`, `sys.databases` | Engine edition, product version, database name, recovery model, log-reuse wait, and the server's own time |
| Waits | `sys.dm_os_wait_stats`, or `sys.dm_db_wait_stats` on Azure SQL Database | Waiting tasks, wait time, and signal wait time for the ten tracked wait types |
| File I/O | `sys.dm_io_virtual_file_stats` joined to `sys.database_files` | Per file: reads, writes, bytes read and written, and read and write stall time |
| Log space | `sys.dm_db_log_space_usage` | Total log size, used log space, and percentage used |

The wait view is chosen from the engine edition. Azure SQL Database (engine
edition 5) does not let database users read the instance-wide view, so the
database-scoped view is used there. SQL Server and SQL Managed Instance use the
instance-wide view, which is why their records carry the "instance-wide"
warning.

The ten tracked wait types are the same as in
`sql\monitoring\07_capture_load_wait_stats.sql`: `WRITELOG`,
`INSTANCE_LOG_RATE_GOVERNOR`, `PAGEIOLATCH_EX`, `PAGEIOLATCH_SH`,
`IO_COMPLETION`, `ASYNC_IO_COMPLETION`, `ASYNC_NETWORK_IO`, `LCK_M_X`,
`LCK_M_IX`, and `LCK_M_SCH_M`.

### How the differences are calculated

- **Window.** The measurement window is the time between the two snapshots,
  taken from the computer running the collector. All rates are divided by it.
- **Counters.** Each figure is the "after" value minus the "before" value. A
  wait type absent from both snapshots counts as zero.
- **Resets.** A cumulative counter can only grow. If an "after" value is
  smaller, the counter was reset by a restart or failover. That figure is
  recorded as "not recorded" with a warning, never as a negative number.
- **Files.** Data files and log files are summarised separately. A file
  added during the test is left out, with a warning.
- **Latency.** Stall time during the test divided by the operations during
  the test, as described for script 06 in the monitoring guide.
- **Log usage.** Recorded from both snapshots. If the log's total size grew,
  the record warns that the run is hard to compare with others.

### How the ADF result is read

**From a file (`--adf-output-file`).** Four shapes are accepted, so whatever
was saved can be used:

| Shape | Typical source |
| --- | --- |
| The Copy activity's output section only | The **Output** link in ADF Studio |
| A whole activity run, with `input` and `output` | Input and output saved together, or one activity from the REST API |
| A list of activity runs | Several activities saved together |
| A REST response with a `value` list | A saved response from the activity-runs API |

For a list or a REST response, the collector picks the Copy activity as
described below.

**From Azure.**

1. **Gets a token** for the Azure management endpoint from your existing
   sign-in. Environment-variable credentials and managed identity are
   deliberately excluded, so the token always belongs to the person running
   the command.
2. **Calls the activity-runs query** for the pipeline run (API version
   2018-06-01). The API requires a time window, so the collector asks for one
   day before `start` until one day after now. The pipeline run ID is what
   selects the run.
3. **Follows paging.** ADF can return an empty page together with a
   continuation token, so the collector keeps reading until no token comes
   back, up to 100 pages.

**Choosing the Copy activity.** From the runs returned, the collector keeps
those whose type is Copy, narrows them by `--activity-name` when given, and
records the result only if exactly one remains. If none or several remain, it
stops and lists the Copy activity names it found. It never chooses between
candidates itself.

### Record warnings added by `stop`

Besides the SQL warnings above, the record warns when:

- the Copy activity status is not `Succeeded`, or it reported errors
- rows read do not equal rows copied, or rows were skipped
- only the output section was supplied, so configured values are unknown
- the SQL window is longer than one and a half times the copy duration plus
  60 seconds, which means `start` or `stop` was run too far from the load

## How the record maps to the runbook

| Runbook section 11 field | Where the value comes from |
| --- | --- |
| Test ID, environment, timestamp | `start` arguments and the snapshot times |
| Source GiB and row count | Copy activity `dataRead` and `rowsRead` |
| Source file count | Copy activity `filesRead` |
| Configured and actual DIUs | Activity input `dataIntegrationUnits`; output `usedDataIntegrationUnits` |
| Parallel copies and connections | Input `parallelCopies`; output `usedParallelCopies` and the peak connection counts |
| Write batch size | Activity input, sink `writeBatchSize` |
| Target table design | `--target-table-design` (only the tester knows this) |
| Copy duration and throughput | Output `copyDuration`; throughput is `dataRead` divided by `copyDuration`, in MiB/s |
| Rows copied and rejected | Output `rowsCopied` and `rowsSkipped` |
| Pre and post log usage | `sys.dm_db_log_space_usage` in each snapshot |
| `WRITELOG` and `INSTANCE_LOG_RATE_GOVERNOR` wait deltas | Wait statistics, after minus before |
| Observed log write MiB/s and write latency | Log file I/O counters, after minus before |
| Data-file IOPS and latency | Data file I/O counters, after minus before |
| Lock waits | Sum of `LCK_M_X`, `LCK_M_IX`, and `LCK_M_SCH_M` wait deltas |
| Cost per load and per GiB | `--cost-per-load`; cost per GiB is calculated |
| Correctness result | `--correctness`, plus an automatic check that rows read equal rows copied |
| PASS / FAIL / INVESTIGATE | `--result` |
| Reason for the next action | `--next-action-reason` |

### How the SQL numbers are calculated

- **Wait time during the test** is the wait time in the "after" snapshot minus
  the wait time in the "before" snapshot, for the same ten wait types as
  `sql\monitoring\07_capture_load_wait_stats.sql`.
- **Write latency** is stall time accumulated during the test divided by the
  writes performed during the test. This is the load-specific calculation
  described for script 06 in the [monitoring guide](../sql/monitoring/README.md).
- **Observed log write rate** is log bytes written during the test divided by
  the seconds between the two snapshots.

## Warnings in the record

The record lists anything that makes the numbers less trustworthy. Read these
before comparing runs.

| Warning | What it means | What to do |
| --- | --- | --- |
| Wait statistics are instance-wide | On SQL Server and SQL MI the wait view covers the whole instance | Run tests on an otherwise quiet instance |
| SQL measurements cover more time than the copy ran | `start` or `stop` was run too far from the load | Run them immediately before and after the load |
| Counters were reset between the snapshots | The instance restarted or failed over during the test | Repeat the test |
| The transaction log grew during the test | Log growth happened inside the run | Do not compare this run with runs that started with space already allocated |
| Rows read do not equal rows copied | Rows were skipped or the copy failed | Treat the run as a correctness failure until explained |
| Configured values are unknown | Only the activity output was supplied | Use Option A, or save input and output together |

## Safe to repeat

- `start` refuses to replace an existing "before" snapshot. Pass `--force` to
  begin the test again; the earlier attempt's results are removed so the two
  attempts are never mixed.
- `stop` refuses to replace an existing record. Pass `--force` to replace it.
- If `stop` fails after it has taken the "after" snapshot (for example because
  the ADF file name was mistyped), run it again. It reuses the snapshot it
  already took. Taking a new one later would stretch the measurement window
  beyond the load.

## How it relates to the monitoring scripts

The collector automates the before-and-after captures of scripts 02, 06, and
07. Those scripts remain useful for looking at one moment by hand.

Scripts 01, 03, 04, 05, and 08 look at conditions *during* a load or at file
configuration. The collector does not replace them. Use them while a long load
is running, as the monitoring guide describes.

## Limits

- **One Copy activity per record.** If the pipeline has several, name the one
  to record with `--activity-name`.
- **Instance-wide waits.** On SQL Server and SQL MI, wait statistics include
  every workload on the instance during the test window.
- **The window is start-to-stop.** SQL numbers cover the time between the two
  commands, not only the copy. The record shows both durations and warns when
  they differ by much.
- **Copy activity loads only.** Loads that do not use a Copy activity are not
  covered.
- **Sinks other than SQL.** Use `start --no-sql`. The record then holds the ADF
  result and the tester's notes, and states that SQL was not measured.

## Verification status

The script is covered by `tests\test_collect_run_metrics.py`, which runs
without a database or Azure access by simulating SQL results and Azure
responses.

It has not yet been run against a live SQL sink or a live data factory. On
first use, check one record by hand:

1. Run scripts 02, 06, and 07 by hand around the same test, as the monitoring
   guide describes.
2. Compare their deltas with the `sink` section of `benchmark_record.json`.
3. Compare the ADF values with the Copy activity's monitoring view.

## Troubleshooting

| Message | Cause | Fix |
| --- | --- | --- |
| `--server and --database are required` | `start` was run without a sink | Supply both, or use `--no-sql` |
| `already has a 'before' snapshot` | The test ID was used before | Choose a new test ID, or pass `--force` |
| `No 'before' snapshot found` | `stop` was run without `start`, or with a different `--output-dir` | Run `start` first; use the same `--output-dir` for both |
| `No Copy activity ... was found` | The run ID belongs to a pipeline with no Copy activity, or the name is wrong | Check the run ID and `--activity-name` |
| `more than one matching Copy activity run` | Several Copy activities, or a retried activity | Use `--activity-name`, or save the attempt you want and use `--adf-output-file` |
| `Azure returned HTTP 401` or `403` | Your sign-in cannot read the factory's runs | Sign in with `az login` or `Connect-AzAccount`, and check your access to the factory |
| `requires azure-identity` | The Azure option was used without the package | Install it, or use `--adf-output-file` |
