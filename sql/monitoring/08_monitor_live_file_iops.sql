/*
08_monitor_live_file_iops.sql
Purpose: Capture a short, live SQL MI file-I/O sample while an ADF load is running.

Design decisions:
- Measures observed IOPS, throughput, and latency. It does not infer the SQL MI
  storage architecture from allocated storage size.
- Does not hard-code service limits that can vary by service tier, hardware,
  storage generation, region, or Microsoft platform changes.
- Optional documented ceilings can be supplied for the exact MI being tested.
- Runs in the current database by default; no undocumented cross-database iterator is used.
- Read-only. No automatic file-growth recommendation is generated.

Usage:
1. Connect to the target database.
2. Start the ADF Copy Activity.
3. Run this script while the load is actively transferring data.
4. Repeat at several points during a long load; a five-second sample can miss bursts.
5. Record results with the ADF activity metrics and scripts 01-07.
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
SET XACT_ABORT ON;

DECLARE @SampleSeconds int = 5;

/* Optional: enter CURRENT documented limits for this exact MI configuration.
   Leave NULL when unknown. Do not copy limits from another MI configuration. */
DECLARE @DocumentedInstanceDataIOPSCeiling decimal(19,2) = NULL;
DECLARE @DocumentedInstanceLogWriteCeilingMiBps decimal(19,2) = NULL;
DECLARE @DocumentedDatabaseLogWriteCeilingMiBps decimal(19,2) = NULL;

IF @SampleSeconds < 1 OR @SampleSeconds > 300
    THROW 50000, '@SampleSeconds must be between 1 and 300.', 1;

DROP TABLE IF EXISTS #FileMetadata;
DROP TABLE IF EXISTS #Snapshot1;
DROP TABLE IF EXISTS #Snapshot2;

CREATE TABLE #FileMetadata
(
    database_id int NOT NULL,
    file_id int NOT NULL,
    DatabaseName sysname NOT NULL,
    LogicalName sysname NOT NULL,
    FileType nvarchar(60) NOT NULL,
    CurrentFileSizeGiB decimal(19,3) NOT NULL,
    SpaceUsedInsideFileGiB decimal(19,3) NULL,
    FreeSpaceInsideFileGiB decimal(19,3) NULL,
    PRIMARY KEY (database_id, file_id)
);

INSERT #FileMetadata
(
    database_id, file_id, DatabaseName, LogicalName, FileType,
    CurrentFileSizeGiB, SpaceUsedInsideFileGiB, FreeSpaceInsideFileGiB
)
SELECT
    DB_ID(),
    df.file_id,
    DB_NAME(),
    df.name,
    df.type_desc,
    CAST(df.size * 8.0 / 1024.0 / 1024.0 AS decimal(19,3)),
    CASE WHEN df.type_desc = N'ROWS'
         THEN CAST(FILEPROPERTY(df.name, 'SpaceUsed') * 8.0 / 1024.0 / 1024.0 AS decimal(19,3))
    END,
    CASE WHEN df.type_desc = N'ROWS'
         THEN CAST((df.size - FILEPROPERTY(df.name, 'SpaceUsed')) * 8.0 / 1024.0 / 1024.0 AS decimal(19,3))
    END
FROM sys.database_files AS df;

CREATE TABLE #Snapshot1
(
    database_id int NOT NULL,
    file_id int NOT NULL,
    num_of_reads bigint NOT NULL,
    num_of_writes bigint NOT NULL,
    num_of_bytes_read bigint NOT NULL,
    num_of_bytes_written bigint NOT NULL,
    io_stall_read_ms bigint NOT NULL,
    io_stall_write_ms bigint NOT NULL,
    sample_ms bigint NOT NULL,
    PRIMARY KEY (database_id, file_id)
);

CREATE TABLE #Snapshot2
(
    database_id int NOT NULL,
    file_id int NOT NULL,
    num_of_reads bigint NOT NULL,
    num_of_writes bigint NOT NULL,
    num_of_bytes_read bigint NOT NULL,
    num_of_bytes_written bigint NOT NULL,
    io_stall_read_ms bigint NOT NULL,
    io_stall_write_ms bigint NOT NULL,
    sample_ms bigint NOT NULL,
    PRIMARY KEY (database_id, file_id)
);

INSERT #Snapshot1
SELECT database_id, file_id, num_of_reads, num_of_writes,
       num_of_bytes_read, num_of_bytes_written,
       io_stall_read_ms, io_stall_write_ms, sample_ms
FROM sys.dm_io_virtual_file_stats(DB_ID(), NULL);

DECLARE @Delay time(0) = DATEADD(SECOND, @SampleSeconds, CAST('00:00:00' AS time));
WAITFOR DELAY @Delay;

INSERT #Snapshot2
SELECT database_id, file_id, num_of_reads, num_of_writes,
       num_of_bytes_read, num_of_bytes_written,
       io_stall_read_ms, io_stall_write_ms, sample_ms
FROM sys.dm_io_virtual_file_stats(DB_ID(), NULL);

;WITH Deltas AS
(
    SELECT
        s1.database_id,
        s1.file_id,
        s2.num_of_reads - s1.num_of_reads AS ReadsDelta,
        s2.num_of_writes - s1.num_of_writes AS WritesDelta,
        s2.num_of_bytes_read - s1.num_of_bytes_read AS BytesReadDelta,
        s2.num_of_bytes_written - s1.num_of_bytes_written AS BytesWrittenDelta,
        s2.io_stall_read_ms - s1.io_stall_read_ms AS ReadStallDeltaMs,
        s2.io_stall_write_ms - s1.io_stall_write_ms AS WriteStallDeltaMs,
        (s2.sample_ms - s1.sample_ms) / 1000.0 AS ActualSampleSeconds
    FROM #Snapshot1 AS s1
    JOIN #Snapshot2 AS s2
      ON s2.database_id = s1.database_id
     AND s2.file_id = s1.file_id
), Metrics AS
(
    SELECT
        d.*,
        CAST((d.ReadsDelta + d.WritesDelta) / NULLIF(d.ActualSampleSeconds, 0) AS decimal(19,2)) AS ObservedIOPS,
        CAST(d.BytesReadDelta / 1048576.0 / NULLIF(d.ActualSampleSeconds, 0) AS decimal(19,2)) AS ReadMiBps,
        CAST(d.BytesWrittenDelta / 1048576.0 / NULLIF(d.ActualSampleSeconds, 0) AS decimal(19,2)) AS WriteMiBps,
        CAST(d.ReadStallDeltaMs * 1.0 / NULLIF(d.ReadsDelta, 0) AS decimal(19,2)) AS AvgReadLatencyMs,
        CAST(d.WriteStallDeltaMs * 1.0 / NULLIF(d.WritesDelta, 0) AS decimal(19,2)) AS AvgWriteLatencyMs
    FROM Deltas AS d
)
SELECT
    fm.DatabaseName,
    fm.LogicalName,
    fm.FileType,
    fm.CurrentFileSizeGiB,
    fm.SpaceUsedInsideFileGiB,
    fm.FreeSpaceInsideFileGiB,
    m.ActualSampleSeconds,
    m.ReadsDelta,
    m.WritesDelta,
    m.ObservedIOPS,
    m.ReadMiBps,
    m.WriteMiBps,
    m.AvgReadLatencyMs,
    m.AvgWriteLatencyMs,
    CASE
      WHEN fm.FileType = N'LOG' AND @DocumentedDatabaseLogWriteCeilingMiBps IS NOT NULL
      THEN CAST(100.0 * m.WriteMiBps / NULLIF(@DocumentedDatabaseLogWriteCeilingMiBps, 0) AS decimal(9,2))
    END AS PercentOfDocumentedDatabaseLogCeiling,
    CASE
      WHEN m.ReadsDelta + m.WritesDelta = 0 THEN N'No I/O observed in sample window'
      WHEN fm.FileType = N'LOG' AND m.AvgWriteLatencyMs >= 10 THEN N'Review log pressure with scripts 02, 03, 05, and 07'
      WHEN fm.FileType = N'ROWS' AND (COALESCE(m.AvgReadLatencyMs,0) >= 15 OR COALESCE(m.AvgWriteLatencyMs,0) >= 15)
        THEN N'Review sustained data-file latency; confirm with repeated samples and wait deltas'
      ELSE N'No threshold alert in this sample; compare with baseline and ADF throughput'
    END AS Interpretation
FROM #FileMetadata AS fm
JOIN Metrics AS m
  ON m.database_id = fm.database_id
 AND m.file_id = fm.file_id
ORDER BY fm.FileType, fm.file_id;

;WITH Deltas AS
(
    SELECT
        s2.num_of_reads - s1.num_of_reads AS ReadsDelta,
        s2.num_of_writes - s1.num_of_writes AS WritesDelta,
        s2.num_of_bytes_read - s1.num_of_bytes_read AS BytesReadDelta,
        s2.num_of_bytes_written - s1.num_of_bytes_written AS BytesWrittenDelta,
        (s2.sample_ms - s1.sample_ms) / 1000.0 AS ActualSampleSeconds,
        fm.FileType
    FROM #Snapshot1 AS s1
    JOIN #Snapshot2 AS s2 ON s2.database_id=s1.database_id AND s2.file_id=s1.file_id
    JOIN #FileMetadata AS fm ON fm.database_id=s1.database_id AND fm.file_id=s1.file_id
)
SELECT
    DB_NAME() AS DatabaseName,
    CAST(MAX(ActualSampleSeconds) AS decimal(19,3)) AS ActualSampleSeconds,
    CAST(SUM(ReadsDelta + WritesDelta) / NULLIF(MAX(ActualSampleSeconds),0) AS decimal(19,2)) AS ObservedDatabaseIOPS,
    CAST(SUM(BytesReadDelta) / 1048576.0 / NULLIF(MAX(ActualSampleSeconds),0) AS decimal(19,2)) AS ObservedReadMiBps,
    CAST(SUM(BytesWrittenDelta) / 1048576.0 / NULLIF(MAX(ActualSampleSeconds),0) AS decimal(19,2)) AS ObservedWriteMiBps,
    CAST(SUM(CASE WHEN FileType=N'LOG' THEN BytesWrittenDelta ELSE 0 END) / 1048576.0 /
         NULLIF(MAX(ActualSampleSeconds),0) AS decimal(19,2)) AS ObservedLogWriteMiBps,
    @DocumentedInstanceDataIOPSCeiling AS EnteredInstanceDataIOPSCeiling,
    @DocumentedInstanceLogWriteCeilingMiBps AS EnteredInstanceLogWriteCeilingMiBps,
    @DocumentedDatabaseLogWriteCeilingMiBps AS EnteredDatabaseLogWriteCeilingMiBps,
    CASE WHEN @DocumentedInstanceDataIOPSCeiling IS NOT NULL
         THEN CAST(100.0 * (SUM(ReadsDelta + WritesDelta) / NULLIF(MAX(ActualSampleSeconds),0)) /
                   NULLIF(@DocumentedInstanceDataIOPSCeiling,0) AS decimal(9,2)) END AS PercentOfEnteredInstanceIOPSCeiling,
    CASE WHEN @DocumentedDatabaseLogWriteCeilingMiBps IS NOT NULL
         THEN CAST(100.0 * (SUM(CASE WHEN FileType=N'LOG' THEN BytesWrittenDelta ELSE 0 END) / 1048576.0 /
                   NULLIF(MAX(ActualSampleSeconds),0)) / NULLIF(@DocumentedDatabaseLogWriteCeilingMiBps,0) AS decimal(9,2))
    END AS PercentOfEnteredDatabaseLogCeiling
FROM Deltas;
