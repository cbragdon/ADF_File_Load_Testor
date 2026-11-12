# Split Delimited File Tests

`test_split_delimited_file.py` verifies that
`scripts\split_delimited_file.py` produces complete, balanced output files for
Azure Data Factory load-test preparation. The tests use temporary directories,
so they do not modify repository files or require a source data file.

## Requirements

- Python 3.9 or later
- No third-party packages

## Run the tests

Run this test module from the repository root:

```powershell
python -m unittest tests/test_split_delimited_file.py -v
```

Or discover every test in this directory:

```powershell
python -m unittest discover -s tests -v
```

## What the tests verify

| Test | Verification |
| --- | --- |
| Headered CSV | Creates exactly four `.csv` files, repeats the header in each, preserves records with commas and embedded newlines, and distributes five records as 2/1/1/1. |
| Headered pipe-delimited text | Applies the same checks to `.txt` input using `|` as the delimiter and preserves the extension. |
| Headerless pipe-delimited text | Invokes the command-line script with `--no-header`, confirms all five records are retained, and confirms the 2/1/1/1 distribution without adding a header. |

Each test recombines the generated data records and compares them with the
original source rows. This confirms that every source record is written exactly
once.

# Run Collector Tests

`test_collect_run_metrics.py` verifies `scripts\collect_run_metrics.py`, and
`test_mi_capacity.py` verifies its Managed Instance check in
`scripts\mi_capacity.py`. The tests need no database, no Azure access, and no third-party packages. SQL is
replaced by a fake connection that returns prepared rows, and the Azure REST
call is replaced by a fake that returns prepared pages.

## Run the tests

```powershell
python -m unittest tests/test_collect_run_metrics.py tests/test_mi_capacity.py -v
```

## What the tests verify

| Area | Verification |
| --- | --- |
| SQL snapshot | Uses the instance wait view on SQL Server and SQL MI and the database wait view on Azure SQL Database; every query is a `SELECT`. |
| SQL deltas | Wait, file I/O, latency, and log-usage figures describe only the window between the two snapshots; a counter reset is reported instead of producing a negative number; log growth during a test is flagged. |
| Copy activity result | Reads both accepted JSON shapes; configured settings are recorded as unknown when only the output is supplied; unrelated JSON is rejected. |
| Azure paging | Continues reading while a continuation token is returned, including after an empty page. |
| Benchmark record | Holds every runbook section 11 field; warns about failed copies, row mismatches, skipped rows, and a measurement window much longer than the copy. |
| Commands | `start`, `stop`, and `list` run end to end; existing results are never replaced without `--force`; a failed `stop` reuses its "after" snapshot when run again; the verdict is required before anything is measured; no secret is written to disk. |
| Waiting (`stop --wait`) | Finds the run by pipeline name or ID and follows it through Queued and InProgress to a final status; times out; retries passing faults and stops on lasting ones; reads nothing from the sink while waiting; `--snapshot-only` then a saved file completes the record; Ctrl+C keeps the "before" snapshot. |
| Managed Instance check | The instance description and every database's files are read only on Managed Instance, and a denied permission is recorded instead of failing the snapshot; published limits are applied for classic and Next-gen General Purpose and Business Critical; the General Purpose storage type is never guessed; governor waits, near-limit averages, and busy slow files give the right classification; `tempdb` is handled by tier; another busy database, a scaled instance, and reset counters are warned about; the section appears in the record and the index. |
