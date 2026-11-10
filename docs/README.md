# ADF Performance Tuning Documentation

Three runbooks support different stages of large ADF load tuning: execute the smallest practical test, interpret unclear evidence, or understand the complete methodology and remediation options.

## Which runbook should I use?

### [Quick ADF Copy Activity Performance Tuning Runbook](Quick_ADF_Copy_Activity_Performance_Tuning_Runbook.md)

**Start here when actively tuning or troubleshooting a load.**

Use it when you want to:

- Minimize expensive or long-running test executions.
- Start with one baseline and let the evidence choose the next test.
- Test only the variable most likely to be limiting throughput.
- Stop a tuning branch when the next change does not improve throughput by at least 10%.
- Move efficiently from lab evidence to a controlled production test.

This is the operational runbook for development and production tuning, especially for large loads where exhaustive test matrices are impractical.

### [Quick Reference: Diagnosis-Driven Runbook](Quick_Reference_Diagnosis-Driven_Runbook.md)

**Use this when the Quick runbook identifies a condition that needs more explanation.**

Use it when you need to:

- Understand what `WRITELOG`, `INSTANCE_LOG_RATE_GOVERNOR`, `ACTIVE_TRANSACTION`, I/O latency, or lock waits mean.
- Understand why DIUs, batch size, parallel copies, or file count changed the result.
- Decide whether the limiting component is ADF, the file/source path, SQL MI transaction logging, locking, or storage I/O.
- Review the deeper remediation choices and trade-offs before running another large test.

The reference is not an instruction to run every test. It exists to explain the evidence and help choose the smallest next action.

### [Full ADF Copy Activity Performance Tuning Runbook](ADF_Copy_Activity_Performance_Tuning_Runbook.md)

**Use this when you need the complete method or a remediation path.**

Use it when you need to:

- Learn or design the complete tuning method and its trade-offs.
- Understand SQL MI FULL recovery behavior and transaction-log remediation.
- Compare staging, target-table, SQL MI capacity, and production-acceptance options.
- Record a defensible benchmark and escalation decision.

The full runbook explains the rationale behind the Quick runbook and Diagnosis Reference; it is not intended to replace their focused operational roles.

## Recommended workflow

```text
Start with Quick
      |
      v
Run one baseline
      |
      v
Is the next action obvious?
   /        \
 Yes        No
  |          |
  v          v
Run one    Open Reference
targeted   and interpret
test       the condition
  |          |
  +----<-----+
      |
      v
Return to Quick
      |
      v
Open Full Runbook when deeper methodology,
remediation, or escalation is needed
```

## Available formats

All three runbooks are maintained in:

- Markdown (`.md`) for GitHub-native documentation and version control.
- Word (`.docx`) for review, comments, screenshots, and internal collaboration.
- PDF (`.pdf`) for a stable, read-only reference.

Keep the Markdown version as the source of truth when revising the text, then regenerate Word and PDF so the three formats remain aligned.
