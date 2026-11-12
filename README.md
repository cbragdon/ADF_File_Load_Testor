# ADF Performance Toolkit

Tools and guidance for generating, splitting, loading, and measuring delimited
test data for Azure Data Factory performance testing with SQL Server and Azure
SQL Managed Instance.

## What is included

- `scripts\generate_delimited_file.py` creates standalone, schema-aware, or
  optional SDV synthetic source files.
- `scripts\split_delimited_file.py` partitions large delimited files into
  balanced load-test inputs.
- `scripts\collect_run_metrics.py` records each load test as a benchmark
  record: the ADF Copy activity result together with SQL sink measurements
  taken before and after the run. On SQL Managed Instance it also shows how
  close the load came to the instance's published limits.
- `sql\monitoring\` contains SQL Managed Instance diagnostics for transaction
  log, I/O, waits, and active load behavior.
- `docs\` contains the ADF performance-tuning runbook and generator design
  notes.

## Start here

1. Read the [Delimited File Generator guide](scripts/generate_delimited_file.md)
   to create test input.
2. Read the [Delimited File Split Utility guide](scripts/README.md) to create
   balanced file partitions.
3. Use the [SQL monitoring guide](sql/monitoring/README.md) while evaluating
   ADF load performance.
4. Read the [Run Collector guide](scripts/collect_run_metrics.md) to record
   each test as a benchmark record that can be compared with other runs.

## Which files do I run?

The repository has two kinds of Python files, and each has its own job.

| Folder | What the files are | When to run them |
| --- | --- | --- |
| `scripts\` | The tools themselves. Run these to create test files, split them, and record load tests. | Whenever you do the work. Each tool's guide, linked above, gives the commands. |
| `tests\` | Automated checks of the tools. They use fake SQL results and fake Azure responses, so they never connect to a database or to Azure, and they change nothing. | Once after you copy or update the repository, and again after any change to a tool. |

Run them in that order: the checks first, then the tools.

**1. Check that the tools work on your machine.** From the repository root:

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

Every check should end in `ok`, with `OK` on the last line. A failure here
means the Python setup or a file copy is wrong, not your database or Azure
environment, so fix it before going on. The [tests guide](tests/README.md)
lists what each check covers.

**2. Use the tools.** Follow the guide for the tool you need. For example, to
record a load test, follow "Record a test" in the
[Run Collector guide](scripts/collect_run_metrics.md).

Some files in `scripts\` are helpers that other tools use, not tools you run
yourself. `mi_capacity.py` is one: the run collector uses it on SQL Managed
Instance.

Generated files belong in `output\` and are intentionally excluded from Git.
See [CONTRIBUTING.md](CONTRIBUTING.md) for validation expectations and
[SECURITY.md](SECURITY.md) for responsible reporting guidance.
