/*
================================================================================
Azure SQL MI File Diagnostic v2.3 - WORKING MERGE DELTA
================================================================================
This is the first coding artifact after merge review. It is NOT a standalone
replacement for v2-2 yet. Apply these extensions to a copy of v2-2 so the
original file remains unchanged during comparison testing.

Non-negotiable:
    Preserve: WAITFOR DELAY '00:00:05';
================================================================================
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

/* -----------------------------------------------------------------------------
STEP 3 REPLACEMENT: extend both snapshot tables with bytes and split stalls.
Keep the existing instance-wide DMV scope: NULL, NULL.
----------------------------------------------------------------------------- */
CREATE TABLE #File_IO_Snapshot_1
(
    database_id bigint,
    file_id bigint,
    num_of_reads bigint,
    num_of_writes bigint,
    num_of_bytes_read bigint,
    num_of_bytes_written bigint,
    io_stall_read_ms bigint,
    io_stall_write_ms bigint,
    io_stall bigint,
    sample_ms bigint
);

CREATE TABLE #File_IO_Snapshot_2
(
    database_id bigint,
    file_id bigint,
    num_of_reads bigint,
    num_of_writes bigint,
    num_of_bytes_read bigint,
    num_of_bytes_written bigint,
    io_stall_read_ms bigint,
    io_stall_write_ms bigint,
    io_stall bigint,
    sample_ms bigint
);

INSERT INTO #File_IO_Snapshot_1
(
    database_id, file_id, num_of_reads, num_of_writes,
    num_of_bytes_read, num_of_bytes_written,
    io_stall_read_ms, io_stall_write_ms, io_stall, sample_ms
)
SELECT
    database_id, file_id, num_of_reads, num_of_writes,
    num_of_bytes_read, num_of_bytes_written,
    io_stall_read_ms, io_stall_write_ms, io_stall, sample_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL);

/* Preserve the proven v2-2 delay construct. */
WAITFOR DELAY '00:00:05';

INSERT INTO #File_IO_Snapshot_2
(
    database_id, file_id, num_of_reads, num_of_writes,
    num_of_bytes_read, num_of_bytes_written,
    io_stall_read_ms, io_stall_write_ms, io_stall, sample_ms
)
SELECT
    database_id, file_id, num_of_reads, num_of_writes,
    num_of_bytes_read, num_of_bytes_written,
    io_stall_read_ms, io_stall_write_ms, io_stall, sample_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL);

/* -----------------------------------------------------------------------------
STEP 4 REPLACEMENT: retain original totals and add split measurements.
----------------------------------------------------------------------------- */
CREATE TABLE #FileDeltas
(
    database_id int NOT NULL,
    file_id int NOT NULL,
    Sample_Seconds decimal(19, 3) NULL,
    Reads_Delta bigint NOT NULL,
    Writes_Delta bigint NOT NULL,
    Bytes_Read_Delta bigint NOT NULL,
    Bytes_Written_Delta bigint NOT NULL,
    Read_Stall_Delta_MS bigint NOT NULL,
    Write_Stall_Delta_MS bigint NOT NULL,
    IO_Stall_Delta_MS bigint NOT NULL,
    Read_IOPS decimal(19, 2) NULL,
    Write_IOPS decimal(19, 2) NULL,
    Live_File_IOPS decimal(19, 2) NULL,
    Live_Read_MiBps decimal(19, 2) NULL,
    Live_Write_MiBps decimal(19, 2) NULL,
    Live_Avg_Read_Latency_MS decimal(19, 2) NULL,
    Live_Avg_Write_Latency_MS decimal(19, 2) NULL,
    Live_Avg_Stall_Per_IO_MS decimal(19, 2) NULL,
    PRIMARY KEY (database_id, file_id)
);

; WITH DeltaSource AS (
    SELECT
        CONVERT(int, s1.database_id) AS database_id,
        CONVERT(int, s1.file_id) AS file_id,
        (s2.sample_ms - s1.sample_ms) / 1000.0 AS Sample_Seconds,
        s2.num_of_reads - s1.num_of_reads AS Reads_Delta,
        s2.num_of_writes - s1.num_of_writes AS Writes_Delta,
        s2.num_of_bytes_read - s1.num_of_bytes_read AS Bytes_Read_Delta,
        s2.num_of_bytes_written - s1.num_of_bytes_written AS Bytes_Written_Delta,
        s2.io_stall_read_ms - s1.io_stall_read_ms AS Read_Stall_Delta_MS,
        s2.io_stall_write_ms - s1.io_stall_write_ms AS Write_Stall_Delta_MS,
        s2.io_stall - s1.io_stall AS IO_Stall_Delta_MS
    FROM #File_IO_Snapshot_1 AS s1
    INNER JOIN #File_IO_Snapshot_2 AS s2
        ON s2.database_id = s1.database_id
       AND s2.file_id = s1.file_id
)
INSERT INTO #FileDeltas
SELECT
    database_id,
    file_id,
    CAST(Sample_Seconds AS decimal(19, 3)),
    Reads_Delta,
    Writes_Delta,
    Bytes_Read_Delta,
    Bytes_Written_Delta,
    Read_Stall_Delta_MS,
    Write_Stall_Delta_MS,
    IO_Stall_Delta_MS,
    CAST(Reads_Delta / NULLIF(Sample_Seconds, 0) AS decimal(19, 2)),
    CAST(Writes_Delta / NULLIF(Sample_Seconds, 0) AS decimal(19, 2)),
    CAST((Reads_Delta + Writes_Delta) / NULLIF(Sample_Seconds, 0) AS decimal(19, 2)),
    CAST(Bytes_Read_Delta / 1048576.0 / NULLIF(Sample_Seconds, 0) AS decimal(19, 2)),
    CAST(Bytes_Written_Delta / 1048576.0 / NULLIF(Sample_Seconds, 0) AS decimal(19, 2)),
    CAST(Read_Stall_Delta_MS * 1.0 / NULLIF(Reads_Delta, 0) AS decimal(19, 2)),
    CAST(Write_Stall_Delta_MS * 1.0 / NULLIF(Writes_Delta, 0) AS decimal(19, 2)),
    CAST(IO_Stall_Delta_MS * 1.0 / NULLIF(Reads_Delta + Writes_Delta, 0) AS decimal(19, 2))
FROM DeltaSource;

/* -----------------------------------------------------------------------------
NEW RESULT SET 2: database activity breakdown.
Requires #MI_File_Stats and #FileDeltas from the merged v2-2 script.
----------------------------------------------------------------------------- */
; WITH DatabaseActivity AS (
    SELECT
        f.DatabaseID,
        f.DatabaseName,
        CASE
            WHEN f.DatabaseName = DB_NAME() THEN 'TARGET_DATABASE'
            WHEN f.DatabaseName = 'tempdb' THEN 'TEMPDB'
            WHEN f.DatabaseID > 4 THEN 'OTHER_USER_DATABASE'
            ELSE 'SYSTEM_DATABASE'
        END AS DatabaseCategory,
        SUM(ISNULL(d.Read_IOPS, 0)) AS Read_IOPS,
        SUM(ISNULL(d.Write_IOPS, 0)) AS Write_IOPS,
        SUM(ISNULL(d.Live_File_IOPS, 0)) AS Total_IOPS,
        SUM(ISNULL(d.Live_Read_MiBps, 0)) AS Read_MiBps,
        SUM(ISNULL(d.Live_Write_MiBps, 0)) AS Write_MiBps,
        SUM(CASE WHEN f.FileType = 'LOG'
                 THEN ISNULL(d.Live_Write_MiBps, 0) ELSE 0 END) AS Log_Write_MiBps
    FROM #MI_File_Stats AS f
    LEFT JOIN #FileDeltas AS d
        ON d.database_id = f.DatabaseID
       AND d.file_id = f.FileID
    GROUP BY f.DatabaseID, f.DatabaseName
), Totals AS (
    SELECT SUM(Total_IOPS) AS Instance_IOPS
    FROM DatabaseActivity
)
SELECT
    a.DatabaseName,
    a.DatabaseCategory,
    CAST(a.Read_IOPS AS decimal(19, 2)) AS Read_IOPS,
    CAST(a.Write_IOPS AS decimal(19, 2)) AS Write_IOPS,
    CAST(a.Total_IOPS AS decimal(19, 2)) AS Total_IOPS,
    CAST(a.Read_MiBps AS decimal(19, 2)) AS Read_MiBps,
    CAST(a.Write_MiBps AS decimal(19, 2)) AS Write_MiBps,
    CAST(a.Log_Write_MiBps AS decimal(19, 2)) AS Log_Write_MiBps,
    CAST(100.0 * a.Total_IOPS / NULLIF(t.Instance_IOPS, 0) AS decimal(9, 2))
        AS Percent_Of_Observed_Instance_IOPS
FROM DatabaseActivity AS a
CROSS JOIN Totals AS t
WHERE a.DatabaseCategory <> 'SYSTEM_DATABASE'
   OR a.Total_IOPS > 0
ORDER BY
    CASE a.DatabaseCategory
        WHEN 'TARGET_DATABASE' THEN 1
        WHEN 'TEMPDB' THEN 2
        WHEN 'OTHER_USER_DATABASE' THEN 3
        ELSE 4
    END,
    a.Total_IOPS DESC;

/* -----------------------------------------------------------------------------
NEXT CODING STEP
-----------------------------------------------------------------------------
Merge these columns into v2-2 Result Set 1, then tighten recommendation logic:

    d.Read_IOPS
    d.Write_IOPS
    d.Live_File_IOPS
    d.Live_Read_MiBps
    d.Live_Write_MiBps
    d.Live_Avg_Read_Latency_MS
    d.Live_Avg_Write_Latency_MS

Do not remove original v2-2 fields or action-oriented outputs.
Do not retire script 08 until the complete v2.3 script passes side-by-side tests.
*/
