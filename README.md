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
  taken before and after the run.
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

Generated files belong in `output\` and are intentionally excluded from Git.
See [CONTRIBUTING.md](CONTRIBUTING.md) for validation expectations and
[SECURITY.md](SECURITY.md) for responsible reporting guidance.
