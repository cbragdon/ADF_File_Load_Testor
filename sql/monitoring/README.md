# SQL Managed Instance ADF Load Monitoring Scripts

This folder contains eight read-only T-SQL scripts for observing Azure SQL
Managed Instance (SQL MI) while Azure Data Factory (ADF) loads data into a
database. The goal is to identify whether an ADF tuning change improves
throughput or simply moves the bottleneck to SQL MI transaction logging,
storage I/O, locking, or another resource.

These scripts are intended for the ADF Performance Toolkit learning lab and for carefully controlled production diagnostics. They do not change database configuration, terminate sessions, shrink files, or clear wait statistics.

> **Important:** SQL MI user databases use the FULL recovery model. Treat transaction-log behavior as part of the workload to measure and tune. Do not base the test plan on switching to SIMPLE or BULK_LOGGED recovery.

## Recommended test workflow

For each ADF benchmark:

1. Run `01_check_recovery_and_log_reuse.sql` to establish recovery and log-reuse state.
2. Run `02_monitor_log_space.sql` immediately before the load.
3. Run `03_monitor_log_stats.sql` immediately before the load.
4. Run `04_check_log_file_configuration.sql` before the benchmark series and again if file configuration changes.
5. Run Step 1 of `07_capture_load_wait_stats.sql` immediately before starting ADF. Keep that SQL session open.
6. Start the ADF Copy Activity.
7. During a longer load, periodically run `02_monitor_log_space.sql` and `08_monitor_live_file_iops.sql`; use `05_find_active_log_transactions.sql` if log usage or `ACTIVE_TRANSACTION` becomes important.
8. Capture `06_monitor_file_io_latency.sql` immediately before and after the run so deltas can be calculated.
9. After the ADF activity finishes, run Step 2 of `07_capture_load_wait_stats.sql` in the same SQL session.
10. Capture `02_monitor_log_space.sql` and `03_monitor_log_stats.sql` again.
11. Record the SQL MI results alongside ADF metrics such as copy duration, rows copied, bytes copied, throughput, DIUs used, and parallel copies used.

Do not judge a setting from one metric alone. For example, a faster ADF run accompanied by rapidly increasing `INSTANCE_LOG_RATE_GOVERNOR` waits may indicate that additional ADF concurrency is pushing SQL MI to its log-rate ceiling.

---

## 01_check_recovery_and_log_reuse.sql

### Purpose

Reports the current database recovery model and the reason SQL Server cannot currently reuse transaction-log space.

### Key columns

- `DatabaseName`: Database in which the script is running.
- `RecoveryModel`: Expected to be `FULL` on SQL MI.
- `LogReuseWait`: Current reason inactive log space cannot be reused.
- `AutoShrinkEnabled`: Should normally be disabled for a performance-test database.

### Interpreting `LogReuseWait`

Common values include:

| Value | Interpretation |
|---|---|
| `NOTHING` | Nothing currently prevents log reuse. This is the cleanest baseline state. |
| `LOG_BACKUP` | Log reuse is waiting on the managed log-backup process. Do not assume you should manually take a log backup on SQL MI. |
| `ACTIVE_TRANSACTION` | An open transaction still needs part of the log. Run script 05 to identify transactions consuming or reserving log space. |
| `CHECKPOINT` | Log reuse is waiting for checkpoint-related work. Observe whether the condition is temporary before treating it as a problem. |
| `AVAILABILITY_REPLICA` | Replica-related processing is retaining log records. More ADF DIUs will not correct this condition. |
| `ACTIVE_BACKUP_OR_RESTORE` | Backup or restore activity is involved. Avoid drawing benchmark conclusions until the competing activity is understood. |

### What to look for during ADF tuning

If `LogReuseWait` changes to `ACTIVE_TRANSACTION` when write batch size is increased, the larger ADF batch may be holding more log active for longer. That does not automatically make the setting bad, but the throughput gain should justify the additional log pressure and rollback exposure.

---

## 02_monitor_log_space.sql

### Purpose

Shows current transaction-log allocation and how much of the allocated log is active.

### Key columns

- `TotalLogSizeGiB`: Currently allocated log-file capacity.
- `UsedLogSpaceGiB`: Amount of log currently in use.
- `UsedLogPercent`: Percentage of allocated log currently used.
- `AvailableLogSpaceGiB`: Allocated log space that is currently available.

### How to read the result

Take samples before, during, and after the load.

Example:

```text
Before:  Total 8.000 GiB, Used 0.250 GiB,  3.13%
During:  Total 8.000 GiB, Used 5.800 GiB, 72.50%
After:   Total 8.000 GiB, Used 0.400 GiB,  5.00%
```

This pattern shows substantial log consumption during the load followed by successful reuse after the load.

If `UsedLogPercent` continues rising toward the allocated limit and does not fall, run scripts 01, 03, and 05 to find what is retaining the log.

### Tuning use

Compare this metric while testing ADF write batch size. A larger batch may improve throughput while increasing active log usage. The useful setting is not simply the largest batch; it is the setting that improves elapsed time without creating unacceptable log pressure or recovery behavior.

---

## 03_monitor_log_stats.sql

### Purpose

Returns a deeper transaction-log view, including Virtual Log File (VLF) counts, active log size, truncation holdup, and the most recent managed log-backup time visible to SQL Server.

### Key columns

- `active_vlf_count`: Number of VLFs currently active.
- `total_vlf_count`: Total number of VLFs in the log.
- `TotalLogSizeGiB`: Total allocated log size.
- `ActiveLogSizeGiB`: Portion of the log that is active.
- `log_truncation_holdup_reason`: The condition currently preventing truncation.
- `log_backup_time`: Most recent log-backup time reported by the engine.

### How to decipher the results

Focus first on the relationship between `ActiveLogSizeGiB` and `TotalLogSizeGiB`, then on `log_truncation_holdup_reason`.

A high VLF count is not automatically a performance problem. It becomes more interesting when combined with evidence of repeated log autogrowth, unusual recovery behavior, or poor log performance. Use script 04 to understand the configured growth increment before drawing conclusions.

### Tuning use

Run before and after changes to ADF write batch size or concurrency. If active log size grows significantly as concurrency rises while Copy Activity throughput stops improving, SQL MI is likely becoming the limiting side of the pipeline.

---

## 04_check_log_file_configuration.sql

### Purpose

Reports the current SQL transaction-log file size, maximum size, and autogrowth configuration.

### Key columns

- `AllocatedSizeMB`: Current physical allocation of the log file.
- `MaximumSizeMB`: Configured maximum, or `NULL` when SQL Server reports unlimited growth.
- `UsesPercentageGrowth`: Indicates percentage-based versus fixed growth.
- `GrowthValue`: Configured growth increment.
- `GrowthUnit`: `MB` or `PERCENT`.

### How to decipher the results

For repeatable benchmarks, avoid having one test trigger significant log-file growth while another starts with already-allocated space. That would make the runs incomparable.

A fixed MB growth setting is easier to reason about than percentage growth because each growth event has a predictable size.

### Benchmark warning

Do **not** shrink the log between ADF test runs to make the environment look identical. Shrinking and regrowing the log changes the physical and VLF state and creates an artificial workload. Preserve the allocation and record it as part of the test conditions.

---

## 05_find_active_log_transactions.sql

### Purpose

Identifies active transactions in the current database and shows how much transaction-log space each is using or reserving.

This script is especially useful when script 01 reports:

```text
LogReuseWait = ACTIVE_TRANSACTION
```

### Key columns

- `transaction_begin_time`: When the transaction started.
- `LogMBUsed`: Log space already used by the transaction.
- `LogMBReserved`: Additional log space reserved by the transaction.
- `session_id`: SQL session associated with the transaction.
- `program_name`: Client/application name when SQL Server can identify it.
- `RequestStatus`: Current request status.
- `wait_type`: Current wait for the request.
- `blocking_session_id`: Session blocking the request, if any.
- `total_elapsed_time`: Current request elapsed time.
- `CurrentStatement`: Statement currently executing when available.

### How to decipher the results

Sort order places the largest log consumers first.

During an ADF load, a session with a large `LogMBUsed` and long-running transaction may reflect a large write batch. Compare that observation with the configured ADF write batch size.

If `blocking_session_id` is nonzero, investigate blocking before assuming ADF compute or SQL MI storage is the bottleneck.

### Tuning use

This is a diagnostic script, not a reason to kill a session automatically. If a larger ADF batch causes one transaction to retain several GiB of log with little throughput improvement, test a smaller batch and compare the results.

---

## 06_monitor_file_io_latency.sql

### Purpose

Reports cumulative I/O counters for each database data and log file, including calculated average read and write latency.

### Important behavior

`sys.dm_io_virtual_file_stats` is cumulative. The raw values include I/O that happened before the current ADF test. Therefore, **do not compare absolute counters from different runs**.

Capture the script immediately before and after the ADF load and calculate deltas.

### Key columns

- `NumberOfReads` / `NumberOfWrites`: Cumulative I/O counts.
- `GiBRead` / `GiBWritten`: Cumulative data volume.
- `ReadStallMilliseconds` / `WriteStallMilliseconds`: Time spent waiting for I/O.
- `AverageReadLatencyMs` / `AverageWriteLatencyMs`: Lifetime average latency for the current counter window.

### Correct load-specific calculation

For the transaction-log file:

```text
Load write latency (ms/write)
=
(Post WriteStallMilliseconds - Pre WriteStallMilliseconds)
/
(Post NumberOfWrites - Pre NumberOfWrites)
```

Also calculate load-specific bytes written:

```text
Log GiB written during load
=
Post GiBWritten - Pre GiBWritten
```

### How to decipher the results

If increasing ADF parallelism increases SQL MI writes and write-stall time but produces little improvement in copy duration, the sink is absorbing additional pressure without delivering useful throughput.

Treat latency as a comparative benchmark signal rather than using a single universal good/bad threshold. SQL MI architecture, service tier, workload, and storage configuration all affect the expected value.

---

## 07_capture_load_wait_stats.sql

### Purpose

Measures selected SQL MI wait-stat changes during one ADF load.

### Critical usage requirement

Run Step 1 and Step 2 in the **same SQL query session**. The baseline is stored in the local temporary table `#WaitStatsBefore`, which disappears when that session closes.

Workflow:

1. Run Step 1.
2. Leave the query window/session open.
3. Start the ADF Copy Activity.
4. Wait for the Copy Activity to finish.
5. In the original SQL query window, run Step 2.

### Wait types captured

| Wait type | What it can indicate |
|---|---|
| `WRITELOG` | Sessions waited for transaction-log flushes. Compare with log-file latency and ADF batch/concurrency settings. |
| `INSTANCE_LOG_RATE_GOVERNOR` | SQL MI log-rate governance limited the workload. Increasing ADF DIUs or concurrent writers is unlikely to improve the sink while this is dominant. |
| `PAGEIOLATCH_EX` | SQL Server waited for a data page needed for modification to be read from storage. |
| `PAGEIOLATCH_SH` | SQL Server waited for a data page needed for reading. |
| `IO_COMPLETION` | A request waited for asynchronous I/O completion. Correlate with script 06. |
| `ASYNC_IO_COMPLETION` | Asynchronous I/O completion wait. Interpret alongside file I/O metrics. |
| `ASYNC_NETWORK_IO` | SQL Server waited for the client side to consume or exchange data. This can point attention toward ADF/network/client-side behavior rather than simply adding database resources. |
| `LCK_M_X` | Waiting for an exclusive lock. Concurrent writers may be contending. |
| `LCK_M_IX` | Waiting for an intent-exclusive lock. Review concurrent writers and target-table activity. |
| `LCK_M_SCH_M` | Waiting for a schema-modification lock. Look for DDL or other operations competing with the load. |

### How to use wait deltas

The most important column is `WaitTimeMsDuringTest`, but do not rank a wait in isolation. Correlate it with ADF and SQL MI metrics.

Example diagnostic patterns:

#### Pattern A: ADF is benefiting from more compute

```text
DIUs:              4 -> 8
ADF throughput:    substantial increase
Copy duration:     substantial decrease
WRITELOG:          modest change
LOG_RATE_GOVERNOR: near zero
```

Additional ADF resources are still useful.

#### Pattern B: SQL MI transaction log is becoming the bottleneck

```text
DIUs:              8 -> 16
ADF throughput:    little/no improvement
WRITELOG:          large increase
Log write latency: increases
```

More ADF compute is not solving the limiting resource. Investigate batch size and concurrent writers.

#### Pattern C: SQL MI log-rate limit is reached

```text
INSTANCE_LOG_RATE_GOVERNOR: significant wait time
ADF throughput:             plateaus
Higher DIUs:                no meaningful gain
```

Stop increasing ADF pressure. The Copy Activity is no longer the primary limitation.

#### Pattern D: concurrency creates locking

```text
Writers:      1 -> 2 -> 4
Throughput:   improves at 2, plateaus at 4
LCK_M_X/IX:   rises sharply at 4
```

Two concurrent writers may be more efficient than four for that target-table configuration.

---

## 08_monitor_live_file_iops.sql

### Purpose

Captures a configurable short I/O sample for the current database while an ADF
load is active. It reports observed reads, writes, IOPS, throughput, latency,
and data/log file space without inferring service limits from file size.

### How to use it

Run it in the target database while ADF is transferring data. The default
sample window is five seconds; change `@SampleSeconds` only when a longer
window better represents the workload. Repeat the sample during a long load
because a single interval can miss short bursts.

The optional ceiling variables are `NULL` by default. Populate them only with
current documented limits for the exact SQL MI configuration under test. The
script calculates percentages only when those values are supplied.

### Key output

- Per-file read/write operations, MiB/s, and average latency for the sample.
- Per-file allocated, used, and free space for data files.
- Database-level observed IOPS, read throughput, write throughput, and log
  write throughput.
- Optional percentages of the user-supplied documented IOPS and log-write
  ceilings.

The script creates temporary tables and waits for the configured sample
duration. It does not persist data, alter database files, or generate resize
commands.

---

# Interpreting all eight scripts together

No single query identifies an ADF bottleneck. Use the scripts as a diagnostic chain:

```text
ADF Copy Activity is slow
        |
        v
01 - Why can/can't log space be reused?
        |
        v
02 - How much log is active?
        |
        v
03 - What is holding log truncation and what is the VLF state?
        |
        v
04 - Is log sizing/growth affecting this test?
        |
        v
05 - Is a large/blocked transaction holding the log?
        |
        v
06 - What data/log I/O occurred and what latency did it experience?
        |
        v
07 - Where did SQL MI spend its waiting time during the load?
        |
        v
08 - What live file IOPS, throughput, and latency are observed during the active copy?
```

Combine those observations with these ADF Copy Activity metrics:

- Configured DIUs
- DIUs actually used
- Degree of copy parallelism
- Write batch size
- Number of source files
- Rows copied
- Bytes copied
- Copy duration
- Throughput
- Source and sink peak connections

The goal is not to discover a universally correct DIU or batch-size setting. The goal is to determine **which component becomes limiting as ADF load pressure increases**, and stop adding resources or concurrency once they no longer improve useful throughput.

# Suggested benchmark record

For each run, keep a row in your benchmark results with at least:

```text
Test ID
Date/time
Source file count
Source GiB
Source row count
DIU setting
DIUs used
Parallel copies
Write batch size
Table lock enabled/disabled
Target heap/indexed
Copy duration
ADF throughput
Rows copied
Pre/post log used GiB
Log GiB written during test
Load-specific log write latency
WRITELOG wait ms
INSTANCE_LOG_RATE_GOVERNOR wait ms
PAGEIOLATCH wait ms
Lock wait ms
Result/notes
```

This provides the evidence needed to explain *why* a tuning change helped, hurt, or made no measurable difference.
