# ADF Copy Activity Performance Tuning - 5-Page Runbook

Efficient lab testing and production acceptance for DIUs, batch sizing, parallelism, and file counts

## 1. Test only what matters

Use one rule throughout: **change one variable, run three comparable executions, compare medians, then stop when the next change no longer produces useful improvement.**

### Required measurements per run

| Measure | Why it matters |
|---|---|
| Copy duration and throughput | Primary performance result |
| Rows copied / rejected | Correctness gate |
| DIUs used and parallel copies | Confirms what ADF actually used |
| WRITELOG and INSTANCE_LOG_RATE_GOVERNOR | Detects SQL MI log pressure |
| Log write latency / live IOPS | Detects sink I/O pressure |
| Cost per GiB | Prevents paying more for negligible speed |

### Decision thresholds

- **PASS:** median performance improves **>=10%**, data is correct, and no material new SQL MI bottleneck appears.
- **STOP:** next increase improves performance **<5%** or materially worsens SQL MI waits/latency/cost.
- **CONFIRM:** improvement is **5-10%**; repeat the comparison before accepting it.
- **INVALID TEST SERIES:** repeated baseline changes **>15%** between the beginning and end of the series.

Use the `ADF_Performance_Toolkit` SQL scripts 01-08 for supporting evidence. Do not run every diagnostic continuously; use the focused scripts when the primary signals point to SQL MI.

## 2. DIU tuning plan

Keep file count, batch size, target table, and parallel-copy setting unchanged.

| Step | Setting | Runs | Decision |
|---|---|---:|---|
| D0 | DIU = Auto | 3 | Establish baseline median and DIUs actually used. |
| D1 | DIU = 4 | 3 | Continue only if >=10% improvement or lower cost per GiB. |
| D2 | DIU = 8 | 3 | Continue only if another >=10% gain and SQL MI remains healthy. |
| D3 | Next supported DIU value | 3 | Stop at first <5% gain, material cost increase, or sink-pressure increase. |
| D4 | DIU = Auto | 3 | Baseline drift >15% invalidates the series. |

### Read the result

- **Throughput rises and SQL MI waits stay stable:** ADF compute was useful. Continue one level.
- **Throughput plateaus while WRITELOG/log latency rises:** stop increasing DIUs; the sink is taking the pressure.
- **INSTANCE_LOG_RATE_GOVERNOR becomes material:** stop adding ADF write pressure.
- **Higher DIUs cost more but gain <5%:** use the lower setting.

### Production DIU acceptance

1. Capture at least three untouched production executions as P0.
2. Apply one lab-qualified DIU change only.
3. Validate row counts/rejects first.
4. Compare median duration, throughput, SQL MI waits, and cost against P0.
5. Promote only for >=10% useful improvement with acceptable SQL MI health and cost; otherwise rollback or investigate.

## 3. Write batch sizing plan

Batch sizing is mainly a **commit/log-pressure experiment** on SQL MI FULL recovery.

### Lab

| Step | Batch size | Runs | What matters |
|---|---:|---:|---|
| B0 | Default/unset | 3 | Baseline duration, WRITELOG, log usage, latency. |
| B1 | 10,000 | 3 | Keep if >=10% useful improvement. |
| B2 | 50,000 | 3 | Continue only if scaling persists without disproportionate log growth. |
| B3 | 100,000 | 3 | Stop if gain <5%, WRITELOG worsens, active log grows sharply, or rollback risk is unacceptable. |
| B4 | Higher only if B3 still scales | 3 | A larger batch must earn promotion; do not assume 250,000 is better. |
| B5 | Default/unset | 3 | Check baseline drift. |

### Production

1. Record current batch size and three representative P0 runs.
2. Choose **one** lab-qualified neighboring setting, not multiple jumps.
3. Change batch size only.
4. Validate exact row counts and target integrity.
5. Compare duration/throughput plus WRITELOG, log usage, log latency, rollback behavior, and cost.
6. PASS only if >=10% useful median improvement with no unacceptable operational impact.
7. FAIL rolls back. INVESTIGATE is not promoted.

## 4. Parallelism and file-count plan

Test these separately. **DIUs, parallel copies, source-file count, and simultaneous SQL writers are not the same variable.**

### A. Parallel copies

Keep the chosen DIU, batch size, and source layout fixed.

| Step | Parallel copies | Runs | Decision |
|---|---:|---:|---|
| P0 | Auto | 3 | Baseline. |
| P1 | 1 | 3 | Serialized reference. |
| P2 | 2 | 3 | Continue if >=10% gain and waits/latency remain stable. |
| P3 | 4 | 3 | Stop if <5% gain or locking/log pressure rises. |
| P4 | Next supported value only if P3 scales | 3 | Stop at the knee. |
| P5 | Auto | 3 | Detect drift. |

**Deterministic signals:**
- `LCK_M_X`/`LCK_M_IX` rises with concurrency: reduce writers/concurrency.
- Log-rate governor rises: do not add more write pressure.
- Source throughput rises but end-to-end throughput does not: SQL MI or another downstream component is limiting.

### B. File count

Use the exact same logical data. Create partitions with `split_delimited_file.py`; verify partition row counts sum to the original.

| Step | Source layout | Runs | Decision |
|---|---:|---:|---|
| F0 | 1 file | 3 | Canonical file-layout baseline. |
| F1 | 2 files | 3 | Keep only if file-level parallelism improves end-to-end result. |
| F2 | 4 files | 3 | Continue if >=10% gain with similar SQL MI pressure. |
| F3 | 8 files | 3 | Test only if 4 files still scale or production naturally uses many files. |
| F4 | 16 only if 8 scales | 3 | Never equate 16 files with 16 SQL writers. |
| F5 | 1 file | 3 | Detect drift. |

**Stop increasing file count** when end-to-end improvement is <5%, SQL MI pressure rises without time savings, or partition-management overhead outweighs the gain.

## 5. Final confirmation and production promotion

After independent tuning, test the selected DIU + batch + parallelism + file-count configuration together three times. Then repeat the untouched baseline three times.

| Outcome | Rule |
|---|---|
| PASS | >=10% median improvement vs original baseline, exact data, acceptable SQL MI health, acceptable cost. |
| FAIL | Incorrect data, reliability regression, unacceptable sink pressure, or no useful improvement. |
| INVESTIGATE | Faster, but cost or a displaced bottleneck needs explanation. Do not promote. |

### Production sequence

1. Capture P0 baseline.
2. State one hypothesis.
3. Change one variable.
4. Validate correctness.
5. Measure ADF + SQL MI.
6. Compare median performance and cost.
7. Check for displaced bottlenecks.
8. PASS / FAIL / INVESTIGATE.
9. Only after PASS does the setting become the new production baseline.

> **Production reminder:** the referenced production feed is approximately **134.04 GiB / 78,949,101 rows**. Sustained log throughput, restart behavior, reliability, and operational correctness matter more there than short-run lab speed.
