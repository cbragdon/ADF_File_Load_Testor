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

A test takes two snapshots of the SQL sink: one before the load and one after
it. Each is taken once, by its own command.

| Order | What you do | What it captures |
| --- | --- | --- |
| 1 | Run `start` | The "before" snapshot |
| 2 | Start the ADF pipeline | Nothing; this is the load being measured |
| 3 | Run `stop` | The "after" snapshot, then the Copy activity result, then the record |

Two snapshots are needed because the SQL counters are cumulative. One reading
says nothing about one load. Only the difference between a reading taken before
the load and a reading taken after it describes the load.

**You don't have to time `stop` yourself.** Add `--wait` and you can run `stop`
as soon as the pipeline is started. It sits and checks the run's status in ADF
every 30 seconds, and takes the "after" snapshot the moment the run finishes,
whether that takes five minutes or five hours. This is the normal way to
finish a test. Other ways are described under
[Other ways to finish a test](#other-ways-to-finish-a-test).

**On Azure SQL Managed Instance, the record also says how close the load came
to the instance's limits.** It compares the load's log write rate and I/O with
the limits Microsoft publishes for the instance's tier and size, shows which
databases did the I/O, and gives a one-word classification with the evidence
behind it. See
[How the Managed Instance check works](#how-the-managed-instance-check-works).

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
| SQL Server and SQL Managed Instance | Permission to view server state | Wait statistics and file I/O counters are server-level views. On Managed Instance the same permission also covers the instance description and the file list for every database. If it is missing for those two, the record says "not collected" and the rest of the test is unaffected. |
| Azure SQL Database | Permission to view database state | The database-scoped wait and I/O views are used |
| Data factory | Read access to the factory's pipeline runs | To check the run's status and read the Copy activity result. Not needed only when you [stop by hand](#stop-by-hand-without-waiting) with a saved file |

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

These are checks of the collector, not the collector itself. Passing them
doesn't record anything. The collector is `scripts\collect_run_metrics.py`,
which you run in [Record a test](#record-a-test). You never run
`scripts\mi_capacity.py` directly; the collector uses it.

### 6. Record one trial test and check it by hand

For the first real test, record the run with the steps in
[Record a test](#record-a-test) and, around the same load, run scripts 02, 06,
and 07 from `sql\monitoring\` by hand. Then compare the two, as described
under [Verification status](#verification-status).

## Record a test

Run the commands from the repository root. Each test runs `start` once and
`stop` once.

The examples below use made-up values. Under each command is a table that
says what to put in each parameter and where to find it. Replace every example
value with your own before you run the command.

The examples are split over several lines to make them readable. In
PowerShell, the backtick (`` ` ``) at the end of a line means "the command
continues on the next line". Keep the backticks, and make sure nothing follows
them on the line, not even a space. You can also type the whole command on one
line without them.

### 1. Run `start` to take the "before" snapshot

Do this immediately before you start the pipeline.

```powershell
python .\scripts\collect_run_metrics.py start `
    --test-id DIU-08-run1 `
    --environment sandbox `
    --server sqlmi-dev.example.database.windows.net `
    --database LoadTest `
    --auth entra-default `
    --mi-gp-storage classic
```

| Parameter | What to put in it | Where to find it |
| --- | --- | --- |
| `--test-id` | A short name you choose for this test. Use a new one for every test, and the same one again in `stop`. Letters, digits, `.`, `_`, and `-` only, up to 64 characters. | You make it up. A name that says what changed works well, for example `DIU-08-run1` for the first run at 8 DIUs. |
| `--environment` | A label for where the test runs, for example `sandbox` or `dev`. It is stored in the record so you can tell runs apart later. Optional; the default is `dev`. | You choose it. |
| `--server` | The SQL server the pipeline loads into (the sink). | **SQL Managed Instance:** Azure portal, open the managed instance, **Overview**, **Host**. **Azure SQL Database:** Azure portal, open the database, **Overview**, **Server name**. **SQL Server on a VM:** the VM's host name or DNS name. Add `,port` at the end if SQL Server doesn't listen on 1433, for example `sqlvm01.contoso.local,14330`. |
| `--database` | The name of the database the pipeline loads into. | The sink dataset or linked service in ADF Studio, or the database list in SSMS. |
| `--auth` | How you sign in to SQL. `entra-default` uses your existing Azure sign-in with no prompt. `entra-interactive` opens a browser sign-in. `windows` uses your Windows sign-in. The default is `windows`. | Use `entra-default` or `entra-interactive` for Managed Instance and Azure SQL Database. Use `windows` for a SQL Server VM joined to your domain. |
| `--mi-gp-storage` | **General Purpose Managed Instance only.** Which kind of General Purpose storage the instance uses: `classic` or `next-gen`. They have different limits, and SQL can't tell them apart reliably, so the collector asks rather than guesses. Without it, the record still shows the waits, the log write rate, and the I/O by database, but storage limits aren't judged. Leave it out for Business Critical and for other platforms. | Azure portal, open the managed instance, **Compute + storage**. If **Next-gen General Purpose** shows as enabled, use `next-gen`; otherwise `classic`. Or run `az sql mi show --name <instance> --resource-group <group> --query isGeneralPurposeV2`: `true` means `next-gen`. |
| `--mi-iops` | **Next-gen General Purpose only, and only if extra IOPS were bought.** The instance's total IOPS. Without it, the collector assumes the included IOPS (3 per GB of reserved storage) and says so in the record. | Azure portal, the managed instance, **Compute + storage**, the IOPS setting. |

Only `--test-id` is always needed. `--server` and `--database` are needed
unless you add `--no-sql`, which skips SQL measurements. Use that only when
the sink isn't a SQL database.

The script prints where the "before" snapshot was saved. Go straight on to
step 2.

### 2. Start the ADF pipeline

Start it however you normally do: from ADF Studio, a trigger, or a script.
The collector does not start it. Keep other work off the sink while it runs
(see [Limits](#limits)).

### 3. Run `stop --wait`, and leave it

Run this as soon as the pipeline is started. It does not take the "after"
snapshot straight away. It waits until the pipeline finishes, then takes the
snapshot, reads the Copy activity result from Azure, and writes the record.

```powershell
python .\scripts\collect_run_metrics.py stop `
    --test-id DIU-08-run1 `
    --wait `
    --pipeline-name PL_Blob_to_SqlMi_Orders `
    --subscription-id 00000000-0000-0000-0000-000000000000 `
    --resource-group rg-data-dev `
    --factory adf-data-dev `
    --change "DIU 4 -> 8" `
    --target-table-design "heap, no indexes" `
    --correctness "row counts match" `
    --result PASS `
    --next-action-reason "Throughput rose 30% with no log governor waits; test 16 DIU next."
```

| Parameter | What to put in it | Where to find it |
| --- | --- | --- |
| `--test-id` | The same test ID you gave `start`. | Your `start` command. |
| `--wait` | Type it as shown. It tells `stop` to wait for the pipeline. | |
| `--pipeline-name` | The name of the pipeline you started in step 2. Type it exactly as ADF Studio shows it. | ADF Studio, **Author**, **Pipelines**. |
| `--subscription-id` | The ID of the Azure subscription that holds the data factory. It looks like `00000000-0000-0000-0000-000000000000`. | Azure portal, open the data factory, **Overview**, **Subscription ID**. If your Azure CLI is already set to that subscription, `az account show --query id` prints it too. |
| `--resource-group` | The resource group the data factory is in. | Azure portal, open the data factory, **Overview**, **Resource group**. |
| `--factory` | The data factory's name. | Azure portal, the name at the top of the data factory's page. ADF Studio also shows it at the top of the screen. |
| `--change` | What is different about this test, in a few words, for example `DIU 4 -> 8` or `baseline`. Optional, but it makes tests easy to compare later. | You describe it. |
| `--target-table-design` | How the sink table is built, for example `heap, no indexes` or `clustered index + 2 nonclustered`. Optional. | The table's definition in SSMS. |
| `--correctness` | Whether the data arrived correctly, for example `row counts match`. Optional. | Your own check after the load. |
| `--result` | Your verdict: `PASS`, `FAIL`, or `INVESTIGATE`. | You decide. |
| `--next-action-reason` | One sentence on what this result tells you to do next, and why. Put it in quotes. | You decide. |

`--result` and `--next-action-reason` are required. If you leave them out, the
script asks for them once the pipeline has finished, so you can decide after
you have seen the run. The runbook treats the reason for the next action as the
point of the record: it stops tuning from becoming a sequence of unexplained
setting changes.

Optional parameters for waiting:

| Parameter | What it does | Default |
| --- | --- | --- |
| `--pipeline-run-id` | Follows one specific run instead of finding it by `--pipeline-name`. The ID is in ADF Studio, **Monitor**, **Pipeline runs**, **Run ID** column. | Not used |
| `--poll-seconds` | How often, in seconds, it checks the run's status. The lowest allowed is 10. | 30 |
| `--timeout-hours` | How long it waits before giving up. Raise it for loads that may run longer. | 12 |
| `--activity-name` | The Copy activity to record, when the pipeline has more than one. Use the activity's name as shown in the pipeline in ADF Studio. | Not used |

While it waits, it prints each status change (Queued, InProgress, then
Succeeded) and a short "still waiting" line every ten minutes.

Things to know:

- **It finds the run for you.** It follows the run of `--pipeline-name` that
  started after `start`. If you already have the run ID, use
  `--pipeline-run-id` instead. If two runs of the pipeline started after
  `start`, it stops and lists them so you can pick one.
- **It doesn't touch the sink while it waits.** It only asks ADF for the run's
  status. The sink is read twice in the whole test: at `start`, and when the
  run finishes. Waiting adds no load to what you're measuring.
- **It waits for a final status.** Succeeded, Failed, and Cancelled all end
  the wait. A failed or cancelled run is still recorded, with a warning, so you
  can see what happened.
- **Change the timing if you need to.** `--poll-seconds` sets how often it
  checks (default 30, minimum 10). `--timeout-hours` sets how long it waits
  before giving up (default 12).
- **Ctrl+C is safe.** It stops waiting and keeps the "before" snapshot. Run
  the same command again to carry on.
- **Keep the computer awake.** If it sleeps, the checks pause and the "after"
  snapshot is taken late. The record warns you when that happens.

### 4. Review

```powershell
python .\scripts\collect_run_metrics.py list
```

This prints one line per recorded test and rebuilds the index file.

Open `benchmark_record.md` in the test folder to read the whole record. On a
Managed Instance, read its **Managed Instance capacity** section and its
warnings before comparing the run with others.

## Other ways to finish a test

The steps above cover most tests. Use one of these only when they don't suit
you. Each one still takes the "after" snapshot once.

### Finish from a file saved in ADF Studio

Use this when you want the record built from a file you saved, rather than
from Azure. `stop` then runs in two parts, and the snapshot is still taken only
once.

1. **Wait and take the snapshot.** Run step 3's command with
   `--snapshot-only` added. Fill in `--test-id`, `--pipeline-name`,
   `--subscription-id`, `--resource-group`, and `--factory` as described in
   step 3. You can leave out `--result`, `--next-action-reason`, and the other
   notes for now. It waits for the pipeline, takes the "after" snapshot when it
   finishes, and stops.
2. **Save the Copy activity result.** In ADF Studio, open the pipeline run on
   the Monitor tab, select the Copy activity's **Output** link, and save the
   JSON to a file.
3. **Finish the record.** This does not take another snapshot. It uses the one
   taken in part 1 and adds the ADF result.

```powershell
python .\scripts\collect_run_metrics.py stop `
    --test-id DIU-08-run1 `
    --adf-output-file .\output\adf\DIU-08-run1.json `
    --change "DIU 4 -> 8" `
    --result PASS `
    --next-action-reason "Throughput rose 30% with no log governor waits; test 16 DIU next."
```

| Parameter | What to put in it |
| --- | --- |
| `--test-id` | The same test ID you gave `start`. |
| `--adf-output-file` | The path to the file you saved in part 2. A path starting with `.\` is relative to the repository root. Put the path in quotes if it contains spaces. |
| `--change`, `--target-table-design`, `--correctness` | As described in step 3. All optional. |
| `--result`, `--next-action-reason` | As described in step 3. Required. |

The **Output** link does not contain the *configured* DIUs, parallel copies,
and write batch size, so those are recorded as "not recorded" and the record
carries a warning. To keep them, save one file containing both the activity's
**Input** and **Output**:

```json
{ "input": { ...the activity's Input JSON... }, "output": { ...the activity's Output JSON... } }
```

### Stop by hand, without waiting

Use this only when the collector can't check the run's status, for example
because it can't reach the ADF management API from where you run it.

Watch the pipeline in ADF Studio, and run `stop` without `--wait` as soon as
it finishes. It takes the "after" snapshot immediately, so run it promptly:
any delay is added to the measurement window.

Give it the Copy activity result in either of two ways.

**From Azure,** using the run ID from the ADF Studio Monitor tab:

```powershell
python .\scripts\collect_run_metrics.py stop `
    --test-id DIU-08-run1 `
    --subscription-id 00000000-0000-0000-0000-000000000000 `
    --resource-group rg-data-dev `
    --factory adf-data-dev `
    --pipeline-run-id 11111111-2222-3333-4444-555555555555 `
    --change "DIU 4 -> 8" `
    --result PASS `
    --next-action-reason "Throughput rose 30% with no log governor waits; test 16 DIU next."
```

| Parameter | What to put in it |
| --- | --- |
| `--test-id` | The same test ID you gave `start`. |
| `--subscription-id`, `--resource-group`, `--factory` | As described in step 3. |
| `--pipeline-run-id` | The run's ID: ADF Studio, **Monitor**, **Pipeline runs**, **Run ID** column. |
| `--change`, `--result`, `--next-action-reason`, and the other notes | As described in step 3. |

**From a saved file,** using the same command as in
[Finish from a file saved in ADF Studio](#finish-from-a-file-saved-in-adf-studio),
part 3. If the collector can't reach Azure at all, this is the way to use.

## What is written

Everything is written under `output\benchmarks\`, which the repository
excludes from Git. Use `--output-dir` to write somewhere else.

| File | Contents |
| --- | --- |
| `<test-id>\before.json` | The "before" SQL snapshot and how to reconnect for `stop` (no secrets) |
| `<test-id>\after.json` | The "after" SQL snapshot |
| `<test-id>\pipeline_run.json` | The pipeline run `--wait` followed: name, run ID, final status, start and end times |
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
   `after.json`, `pipeline_run.json`, `adf_activity.json`, and both record
   files, so results from two attempts are never mixed.
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
   driver, and encryption settings. It also keeps what you stated with
   `--mi-gp-storage` and `--mi-iops`. It holds no password, key, or token,
   because the collector never receives one.

### What `stop` does

1. **Checks that `start` ran.** Without a `before.json` for the test ID, the
   command stops.
2. **Refuses to overwrite** an existing benchmark record unless `--force` is
   given.
3. **Checks its options first.** With `--wait`, it checks that the factory and
   the pipeline are named and that the timing values make sense. When no
   terminal is attached, for example in a scheduled job, `--result` and
   `--next-action-reason` must be on the command line (except with
   `--snapshot-only`, which writes no record). These checks run before
   anything is measured, so a mistake can't leave a half-finished test.
4. **Waits for the pipeline,** with `--wait` (see
   [How waiting works](#how-waiting-works)). If an `after.json` already
   exists, the load has already been measured, so there is nothing to wait
   for and this step is skipped.
5. **Takes the "after" snapshot before anything slower.** Reading ADF from
   Azure can take several seconds, so the snapshot comes first. That keeps the
   SQL measurement window as close to the end of the load as possible.
   - If an `after.json` already exists, because an earlier `stop` failed
     part-way or `--snapshot-only` was used, it is reused. Taking a new
     snapshot later would stretch the window beyond the load.
6. **Calculates the differences** between the two snapshots (see
   [How the differences are calculated](#how-the-differences-are-calculated)).
   On a Managed Instance it then runs the capacity check (see
   [How the Managed Instance check works](#how-the-managed-instance-check-works)).
   With `--snapshot-only`, the command ends here.
7. **Obtains the Copy activity result,** from the file or from Azure (see
   [How the ADF result is read](#how-the-adf-result-is-read)), and saves it
   unchanged as `adf_activity.json`. When `--wait` found the run, its run ID
   is used, so you never have to copy it.
8. **Extracts the benchmark values** from the result, using the property
   names in Microsoft's "Monitor copy activity" documentation. Any value the
   result does not contain is recorded as "not recorded", never estimated.
9. **Asks for the verdict and reason** if they were not on the command line.
10. **Builds the record**, adds its warnings, and writes
    `benchmark_record.json` and `benchmark_record.md`.
11. **Rebuilds `benchmark_index.csv`** from every record in the output
    folder, sorted by start time.

### How waiting works

`--wait` calls only the ADF management API. It never connects to the sink
while it waits, so the measurement isn't disturbed by the measuring.

1. **Find the run.** With `--pipeline-name`, it lists that pipeline's recent
   runs and picks the one that started after `start`. It allows two minutes'
   difference between your computer's clock and Azure's. If no run has started
   yet, it keeps checking. With `--pipeline-run-id`, it reads that run
   directly.
2. **Follow that run.** Once a run is found, every later check reads it by its
   run ID, so a second run started later can't be mistaken for it.
3. **Check at a fixed interval.** Each check reads the run's status. Queued,
   InProgress, and Canceling mean keep waiting. Succeeded, Failed, and
   Cancelled end the wait.
4. **Ride out short faults.** A dropped connection, throttling, or a server
   error is logged and retried at the next check. Ten failures in a row stop
   the wait. A fault that retrying can't fix, such as a wrong factory name or
   missing access, stops it at once.
5. **Stay signed in.** The Azure sign-in is renewed automatically as it
   nears expiry, so a long wait doesn't need you to sign in again.
6. **Record what it saw.** When the run finishes, its name, run ID, status,
   and start and end times are saved in `pipeline_run.json` and copied into
   the record.

The record also shows how long after the run ended the "after" snapshot was
taken. Normally that is under one check interval. A much longer gap means the
checks stopped for a while, usually because the computer went to sleep.

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
| Instance (Managed Instance only) | `sys.server_resource_stats`, newest row | Service tier, hardware, vCores, reserved storage, and storage used |
| All files (Managed Instance only) | `sys.dm_io_virtual_file_stats(NULL, NULL)` joined to `sys.master_files` | For every file of every database: its size and the same I/O counters as above. One query; no database is switched into. |

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

### How the Managed Instance check works

This runs only when the sink is an Azure SQL Managed Instance. It adds nothing
to the load: it uses the two snapshots already taken, with two extra queries in
each (see [What a snapshot reads](#what-a-snapshot-reads)). It reads nothing
while the load runs, and it never suggests changes. Its job is to give a DBA
enough evidence to make the call.

It answers three questions:

1. **Did the load reach a limit?** The proof is a wait. If
   `INSTANCE_LOG_RATE_GOVERNOR` time grew during the test, the engine held log
   writes back because the instance's log limit was reached. That counts as
   reached, however small the amount; the record shows the figure so you can
   judge how much it mattered.
2. **How close did it come?** The load's average log write rate, IOPS, and
   throughput are compared with the published limits for this instance. These
   are averages over the whole window, so they show how close the load ran,
   not its peaks. A short burst can hit a limit while the average stays well
   below it.
3. **Was the instance shared?** I/O is broken down by database. If another
   database did more than 25% of the instance's I/O during the test, the
   record warns that the load didn't have the instance to itself.

**Which limits apply.** The numbers come from Microsoft's
[resource limits page](https://learn.microsoft.com/azure/azure-sql/managed-instance/resource-limits),
read on 12 November 2026. Every record names that version, so you can tell
which numbers it was judged against. They are kept in `scripts\mi_capacity.py`
and should be updated there when Microsoft changes the page.

| Instance | Log write limit (instance) | I/O limit | Throughput limit |
| --- | --- | --- | --- |
| General Purpose, classic | 4.5 MiB/s per vCore, at most 120 MiB/s | Per file, by file size: 500 IOPS up to 129 GiB, 2,300 up to 513 GiB, 5,000 up to 1,025 GiB, 7,500 above | Per file: 100, 150, 200, then 250 MiB/s for the same sizes |
| General Purpose, Next-gen | 4.5 MiB/s per vCore, at most 120 MiB/s (standard-series) or 192 MiB/s (premium-series) | One pool for the instance: 3 IOPS per GB of reserved storage (at least 300), or the total set with `--mi-iops`, up to a cap for the vCore count | IOPS divided by 30, in MB/s (at least 75), up to a cap for the vCore count |
| Business Critical | 16 MiB/s per vCore, at most 160 MiB/s (standard-series) or 192 MiB/s (premium-series) | 4,000 IOPS per vCore, at most 320,000 | Not limited |

In General Purpose, `tempdb` sits on local disk outside these limits, so its
I/O is left out of the instance totals and has no file limit. In Business
Critical it shares the instance's IOPS, so it is included.

When the collector can't work out a limit, it says "unknown" and gives the
reason under **Notes on the limits**. It never fills the gap with a guess.
This happens when `--mi-gp-storage` wasn't given for a General Purpose
instance, or when the hardware isn't recognised and the limit depends on it.

**The classification.** It uses the same names and thresholds as
`sql\monitoring\azure_sql_mi_file_diagnostic_v2-3.sql`: a value at 70% or more
of its limit is "near the limit", and I/O slower than 15 ms is "slow".

| Classification | What it means |
| --- | --- |
| `INSTANCE_LEVEL` | Log governor waits during the test, or an instance-wide average at 70% or more of its limit |
| `FILE_LEVEL` | One or more files at 70% or more of their own limit and slower than 15 ms, while the instance was not under pressure (classic General Purpose only) |
| `MIXED` | Both of the above |
| `NOT_PROVEN` | Some I/O was slower than 15 ms, but nothing was near a known limit. Look elsewhere before blaming storage limits. |
| `HEALTHY` | Nothing near a limit and no slow I/O |
| `NO_ACTIVITY` | No file I/O in the window |
| `NOT_ASSESSED` | Not enough was collected or stated to judge, for example a General Purpose instance without `--mi-gp-storage` and no waits to go on |

Under the classification, the record lists the evidence behind it: the wait
times, each average next to its limit, and each file that was near its limit
or slow. It also lists the I/O by database and the ten busiest files, each
with its size, IOPS, MiB/s, file limit, and latency.

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
- with `--wait`, the pipeline run ended with a status other than Succeeded
- with `--wait`, the "after" snapshot was taken more than two check intervals
  plus 60 seconds after the run ended
- on Managed Instance: another database did more than 25% of the instance's
  I/O, the instance's tier, vCores, or storage changed during the test, or a
  file's counters were reset

## How the record maps to the runbook

| Runbook section 11 field | Where the value comes from |
| --- | --- |
| Test ID, environment, timestamp | `start` arguments and the snapshot times; with `--wait`, also the pipeline name, run ID, and final status |
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
| SQL measurements cover more time than the copy ran | `start` or `stop` was run too far from the load | Run `start` just before the pipeline, and use `stop --wait` |
| The pipeline run finished with a status other than Succeeded | The run failed or was cancelled | Read the run's error in ADF Studio before using the numbers |
| The "after" snapshot was taken long after the pipeline finished | The checks paused, usually because the computer slept | Keep the computer awake during the test, or repeat it |
| Counters were reset between the snapshots | The instance restarted or failed over during the test | Repeat the test |
| The transaction log grew during the test | Log growth happened inside the run | Do not compare this run with runs that started with space already allocated |
| Rows read do not equal rows copied | Rows were skipped or the copy failed | Treat the run as a correctness failure until explained |
| Configured values are unknown | Only the activity output was supplied | Let `stop --wait` read the result from Azure, or save input and output together |
| Another database did more than 25% of the instance's I/O | Something else was busy on the Managed Instance during the test | Find out what it was, and repeat the test when the instance is quiet |
| The instance's tier, hardware, vCores, or reserved storage changed | The Managed Instance was scaled during the test | Repeat the test; a scaling operation also fails the instance over |

## Safe to repeat

- `start` refuses to replace an existing "before" snapshot. Pass `--force` to
  begin the test again; the earlier attempt's results are removed so the two
  attempts are never mixed.
- `stop` refuses to replace an existing record. Pass `--force` to replace it.
- If `stop` fails after it has taken the "after" snapshot (for example because
  the ADF file name was mistyped), run it again. It reuses the snapshot it
  already took. Taking a new one later would stretch the measurement window
  beyond the load.
- If `stop --wait` is stopped with Ctrl+C, times out, or loses Azure for too
  long, nothing is lost. Run the same command again and it carries on waiting.

## How it relates to the monitoring scripts

The collector automates the before-and-after captures of scripts 02, 06, and
07. Those scripts remain useful for looking at one moment by hand.

Scripts 01, 03, 04, 05, and 08 look at conditions *during* a load or at file
configuration. The collector does not replace them. Use them while a long load
is running, as the monitoring guide describes.

`azure_sql_mi_file_diagnostic_v2-3.sql` and the collector's Managed Instance
check ask the same question in different ways. The script takes a live
five-second sample, so use it while a load is running. The collector covers the
whole load, from `start` to `stop`. The collector also differs in three ways:
it uses the current published limits (some of the script's file limits are
older values), it doesn't infer Next-gen General Purpose from the storage size
but asks you with `--mi-gp-storage`, and it records evidence without
suggesting changes. If the two disagree, the collector's averages may be
hiding a burst that the script's sample caught.

## Limits

- **One Copy activity per record.** If the pipeline has several, name the one
  to record with `--activity-name`.
- **Instance-wide waits.** On SQL Server and SQL MI, wait statistics include
  every workload on the instance during the test window.
- **The window is start-to-stop.** SQL numbers cover the time between the
  "before" and "after" snapshots, which includes the whole pipeline, not only
  the Copy activity. The record shows both durations and warns when they
  differ by much.
- **Waiting needs Azure access.** `--wait` checks the run's status through the
  ADF management API, so it needs read access to the factory's pipeline runs.
- **Waiting needs the computer awake.** The checks run on your computer. If it
  sleeps or shuts down, the wait pauses or stops.
- **Copy activity loads only.** Loads that do not use a Copy activity are not
  covered.
- **Sinks other than SQL.** Use `start --no-sql`. The record then holds the ADF
  result and the tester's notes, and states that SQL was not measured.
- **Managed Instance: averages, not peaks.** Usage is averaged over the whole
  window. Waits show whether a limit was reached; averages only show how close
  the load ran.
- **Managed Instance: the per-database log limit isn't judged.** Classic
  General Purpose also limits each database's log rate (22 to 65 MiB/s,
  depending on the log file's size), but Microsoft doesn't publish the value
  for each size. Only the instance limit is judged.
- **Managed Instance: published limits are approximate.** Microsoft describes
  them as approximate, and automatic backups use some of the IOPS. A value just
  under a limit may already be at it.
- **Managed Instance: file sizes come from `sys.master_files`.** On classic
  General Purpose a file's limit depends on its size, so a file that grew
  during the test is judged by its size at `stop`.

## Verification status

The script is covered by `tests\test_collect_run_metrics.py`, which runs
without a database or Azure access by simulating SQL results and Azure
responses.

The Managed Instance check is covered by `tests\test_mi_capacity.py` in the
same way.

It has not yet been run against a live SQL sink or a live data factory. On
first use, check one record by hand:

1. Run scripts 02, 06, and 07 by hand around the same test, as the monitoring
   guide describes.
2. Compare their deltas with the `sink` section of `benchmark_record.json`.
3. Compare the ADF values with the Copy activity's monitoring view.
4. On a Managed Instance, run `azure_sql_mi_file_diagnostic_v2-3.sql` during
   the load, and check that the tier, vCores, storage, and busiest files match
   the record's **Managed Instance capacity** section. Expect the script's
   file limits to differ where it uses older values.

## Troubleshooting

| Message | Cause | Fix |
| --- | --- | --- |
| `--server and --database are required` | `start` was run without a sink | Supply both, or use `--no-sql` |
| `already has a 'before' snapshot` | The test ID was used before | Choose a new test ID, or pass `--force` |
| `No 'before' snapshot found` | `stop` was run without `start`, or with a different `--output-dir` | Run `start` first; use the same `--output-dir` for both |
| `No Copy activity ... was found` | The run ID belongs to a pipeline with no Copy activity, or the name is wrong | Check the run ID and `--activity-name` |
| `more than one matching Copy activity run` | Several Copy activities, or a retried activity | Use `--activity-name`, or save the attempt you want and use `--adf-output-file` |
| `Azure returned HTTP 401` or `403` | Your sign-in can't read the factory's runs | Sign in with `az login` or `Connect-AzAccount`, and check your access to the factory |
| `Azure returned HTTP 404` | The subscription, resource group, factory, or run ID is wrong | Check each value in the Azure portal |
| `requires azure-identity` | The Azure option or `--wait` was used without the package | Install it |
| `--wait needs --pipeline-name` | `--wait` doesn't know which run to follow | Add `--pipeline-name`, or `--pipeline-run-id` |
| `More than one run of the pipeline started after 'start'` | The pipeline was started twice, or a trigger also ran it | Use `--pipeline-run-id` with the run that belongs to the test |
| `did not finish within ... hours` | The run is still going when the timeout ends | Run the same command again, or raise `--timeout-hours` |
| `failed status checks in a row` | Azure couldn't be reached for about five minutes | Check the network or VPN, then run the same command again |
| Status stays `not started yet` | The pipeline wasn't started, or `--pipeline-name` is wrong | Start the pipeline, or check the name in ADF Studio |
| `Managed Instance instance was not collected` or `files was not collected` | Your sign-in can't view server state | Ask for the permission; the rest of the record is still valid |
| Classification `NOT_ASSESSED` on General Purpose | `--mi-gp-storage` wasn't given on `start` | Give it on the next test's `start`; see step 1 |
