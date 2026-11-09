/* 07_capture_load_wait_stats.sql
   Purpose: Capture wait-stat deltas attributable to an ADF load.
   IMPORTANT: Run Step 1 and Step 2 in the SAME query/session because #WaitStatsBefore is a temp table.
   Safety: Read-only.
*/
SET ANSI_NULLS ON;
GO
SET ANSI_PADDING ON;
GO
SET ANSI_WARNINGS ON;
GO
SET ARITHABORT ON;
GO
SET CONCAT_NULL_YIELDS_NULL ON;
GO
SET QUOTED_IDENTIFIER ON;
GO
SET NUMERIC_ROUNDABORT OFF;
GO
SET NOCOUNT ON;

/* STEP 1 - Run immediately BEFORE starting the ADF load. */
DROP TABLE IF EXISTS #WaitStatsBefore;
SELECT
    wait_type,
    waiting_tasks_count,
    wait_time_ms,
    signal_wait_time_ms
INTO #WaitStatsBefore
FROM sys.dm_os_wait_stats;

SELECT SYSDATETIMEOFFSET() AS BaselineCapturedAt,
       COUNT(*) AS BaselineWaitTypes
FROM #WaitStatsBefore;

/*
   Start the ADF load now.
   Leave this query window/session open.
   After the load finishes, highlight and run STEP 2 below.
*/

/* STEP 2 - Run AFTER the ADF load finishes. */
SELECT
    current_waits.wait_type,
    current_waits.waiting_tasks_count - previous_waits.waiting_tasks_count AS WaitingTasksDuringTest,
    current_waits.wait_time_ms - previous_waits.wait_time_ms AS WaitTimeMsDuringTest,
    current_waits.signal_wait_time_ms - previous_waits.signal_wait_time_ms AS SignalWaitTimeMsDuringTest
FROM sys.dm_os_wait_stats AS current_waits
INNER JOIN #WaitStatsBefore AS previous_waits
    ON previous_waits.wait_type = current_waits.wait_type
WHERE current_waits.wait_type IN
(
    N'WRITELOG',
    N'INSTANCE_LOG_RATE_GOVERNOR',
    N'PAGEIOLATCH_EX',
    N'PAGEIOLATCH_SH',
    N'IO_COMPLETION',
    N'ASYNC_IO_COMPLETION',
    N'ASYNC_NETWORK_IO',
    N'LCK_M_X',
    N'LCK_M_IX',
    N'LCK_M_SCH_M'
)
AND current_waits.wait_time_ms >= previous_waits.wait_time_ms
ORDER BY WaitTimeMsDuringTest DESC;
