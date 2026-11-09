/*
========================================================================================
                                    VERSION 2.2 (v2)
                 Azure SQL MI GP Storage, Live Latency & Overload Monitor
========================================================================================
Key Columns to Review in the New Output

â€¢ Current_File_Size_GB / Space_Used_Inside_File_GB / Free_Space_Inside_File_GB:
  Provides a granular look at the data or log capacity inside each individual file.

â€¢ Estimated_File_Max_IOPS_Or_Log_Cap: For ROWS (data files), it directly predicts your
  absolute IOPS performance cap. For LOG files, it now explicitly maps out the
  throughput speed limit governing that exact database log size.

â€¢ Max_Instance_Log_Write_Ceiling: Displays the global cap for your whole Managed
  Instance (4.5 MiB/s per vCore up to 120 MiB/s). Even if you have 10 databases each
  capable of 65 MB/s, their combined write speed cannot break past this instance limit.

â€¢ Max_Instance_Data_IOPS_Ceiling: Displays the total combined IOPS limit allowed by the
  underlying Azure VM container based on your vCore compute allocation.

â€¢ Instance_IOPS_Alert_Status: Evaluates the live 5-second snapshot and warns junior DBAs
  if the entire server's combined storage load is crashing into the global VM ceiling.

â€¢ Safety_Script_To_Expand_File: Dynamically generates the exact T-SQL syntax needed to
  expand a file to the next performance bracket. It begins with '--' to prevent
  accidental execution.

========================================================================================
What is MiB/s and how does it differ from MB/s?

When explaining this to the team, think of it as a difference between how humans count
(base-10) versus how computers think (base-2).

â€¢ MB/s (Megabytes per second): Based on the metric system where units change by multiples
  of 1,000. This is what marketing departments and drive manufacturers use. To them,
  1 Megabyte is exactly 1,000,000 bytes.

â€¢ MiB/s (Mebibytes per second): Based on the binary system that operating systems and
  database engines like SQL Server use internally. Because computers count by powers of 2,
  a "kilo" is 1,024. Therefore, 1 Mebibyte is exactly 1,048,576 bytes (1,024 x 1,024).

The Takeaway for the Team:
Because a Mebibyte handles slightly more bytes than a standard Megabyte, numbers reported
in MiB/s look a bit lower on paper, but they actually represent a faster data transfer.
To convert the script's MiB/s output into decimal MB/s, multiply the value by 1.048576.

For example, the Azure Max Log Ceiling of 120 MiB/s is roughly equal to 126 MB/s.
========================================================================================
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

-- ==========================================
-- STEP 1: GATHER INSTANCE CEILINGS GLOBAL STORAGE
-- ==========================================
IF OBJECT_ID('tempdb..#Instance_Config') IS NOT NULL
    DROP TABLE #Instance_Config;

CREATE TABLE #Instance_Config (
    ServiceTier NVARCHAR(128),
    vCores INT,
    TotalInstanceReservedGB DECIMAL(12,2),
    TotalInstanceUsedGB DECIMAL(12,2),
    MaxInstanceIOPS INT
);

WITH RawConfig AS (
    SELECT TOP 1
        sku AS ServiceTier,
        virtual_core_count AS vCores,
        CAST(reserved_storage_mb / 1024.0 AS DECIMAL(12,2)) AS TotalInstanceReservedGB,
        CAST(storage_space_used_mb / 1024.0 AS DECIMAL(12,2)) AS TotalInstanceUsedGB
    FROM sys.server_resource_stats
    ORDER BY end_time DESC
)
INSERT INTO #Instance_Config
SELECT
    ServiceTier, vCores, TotalInstanceReservedGB, TotalInstanceUsedGB,
    CASE
        WHEN ServiceTier LIKE '%Business Critical%' THEN vCores * 4000
        WHEN ServiceTier LIKE '%General Purpose%' AND TotalInstanceReservedGB > 16384 THEN
            IIF((TotalInstanceReservedGB * 3) > (vCores * 1600), vCores * 1600, TotalInstanceReservedGB * 3)
        ELSE
            CASE
                WHEN vCores <= 4 THEN 6400
                WHEN vCores = 8 THEN 8000
                WHEN vCores = 16 THEN 16000
                WHEN vCores = 32 THEN 32000
                ELSE 40000
            END
    END AS MaxInstanceIOPS
FROM RawConfig;

-- ==========================================
-- STEP 2: GATHER FILE LEVEL STORAGE DETAILS
-- ==========================================
IF OBJECT_ID('tempdb..#MI_File_Stats') IS NOT NULL
    DROP TABLE #MI_File_Stats;

CREATE TABLE #MI_File_Stats (
    DatabaseID INT,
    FileID INT,
    DatabaseName SYSNAME,
    LogicalName SYSNAME,
    FileType VARCHAR(10),
    FileSizeGB DECIMAL(12, 2),
    SpaceUsedGB DECIMAL(12, 2),
    FreeSpaceInFileGB DECIMAL(12, 2)
);

DECLARE @command NVARCHAR(MAX);
SET @command = N'
USE [?];
INSERT INTO #MI_File_Stats
SELECT
    DB_ID() AS DatabaseID,
    file_id AS FileID,
    DB_NAME() AS DatabaseName,
    name AS LogicalName,
    type_desc AS FileType,
    CAST(size * 8. / 1024 / 1024 AS DECIMAL(12,2)) AS FileSizeGB,
    CAST(FILEPROPERTY(name, ''SpaceUsed'') * 8. / 1024 / 1024 AS DECIMAL(12,2)) AS SpaceUsedGB,
    CAST((size - FILEPROPERTY(name, ''SpaceUsed'')) * 8. / 1024 / 1024 AS DECIMAL(12,2)) AS FreeSpaceInFileGB
FROM sys.database_files;
';

EXEC sp_MSforeachdb @command;

-- ==========================================
-- STEP 3: CAPTURE 5-SECOND I/O DELTA SNAPSHOT
-- ==========================================
IF OBJECT_ID('tempdb..#File_IO_Snapshot_1') IS NOT NULL DROP TABLE #File_IO_Snapshot_1;
IF OBJECT_ID('tempdb..#File_IO_Snapshot_2') IS NOT NULL DROP TABLE #File_IO_Snapshot_2;

CREATE TABLE #File_IO_Snapshot_1 (
    database_id INT, file_id INT, num_of_reads BIGINT, num_of_writes BIGINT, io_stall BIGINT, sample_ms BIGINT
);
CREATE TABLE #File_IO_Snapshot_2 (
    database_id INT, file_id INT, num_of_reads BIGINT, num_of_writes BIGINT, io_stall BIGINT, sample_ms BIGINT
);

INSERT INTO #File_IO_Snapshot_1
SELECT database_id, file_id, num_of_reads, num_of_writes, io_stall, sample_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL);

WAITFOR DELAY '00:00:05';

INSERT INTO #File_IO_Snapshot_2
SELECT database_id, file_id, num_of_reads, num_of_writes, io_stall, sample_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL);

-- ==========================================
-- STEP 4: PARSE PERFORMANCE DELTAS
-- ==========================================
IF OBJECT_ID('tempdb..#FileDeltas') IS NOT NULL DROP TABLE #FileDeltas;

CREATE TABLE #FileDeltas (
    database_id INT,
    file_id INT,
    Reads_Delta BIGINT,
    Writes_Delta BIGINT,
    IO_Stall_Delta_MS BIGINT,
    Live_File_IOPS DECIMAL(12,2)
);

INSERT INTO #FileDeltas
SELECT
    s1.database_id,
    s1.file_id,
    (s2.num_of_reads - s1.num_of_reads) AS Reads_Delta,
    (s2.num_of_writes - s1.num_of_writes) AS Writes_Delta,
    (s2.io_stall - s1.io_stall) AS IO_Stall_Delta_MS,
    CAST(((s2.num_of_reads - s1.num_of_reads) + (s2.num_of_writes - s1.num_of_writes)) / ((s2.sample_ms - s1.sample_ms) / 1000.0) AS DECIMAL(12,2)) AS Live_File_IOPS
FROM #File_IO_Snapshot_1 s1
JOIN #File_IO_Snapshot_2 s2 ON s1.database_id = s2.database_id AND s1.file_id = s2.file_id;

DECLARE @NextGenSpec NVARCHAR(600) =
    N'Next-gen General Purpose (Elastic SAN-backed) offers up to 32 TB storage (vs 16 TB classic GP), '
  + N'pooled instance IOPS up to 80,000 not tied to file size, independently scalable IOPS/memory '
  + N'(up to ~13.6 GB RAM per vCore), and lower I/O latency. Upgrade via Portal/CLI/PowerShell (online update of the MI '
  + N'service tier setting); verify current limits in Microsoft docs for your region.';

DECLARE @CombinedLiveInstanceIOPS DECIMAL(12,2);
SELECT @CombinedLiveInstanceIOPS = ISNULL(SUM(Live_File_IOPS), 0.00) FROM #FileDeltas;

-- ====================================================
-- RESULT SET 1: DETAILED FILE PROFILE & TARGET GUIDANCE
-- ====================================================
SELECT
    f.DatabaseName,
    f.LogicalName,
    f.FileType,
    f.FileSizeGB AS [Current_File_Size_GB],
    f.SpaceUsedGB AS [Space_Used_Inside_File_GB],      -- formalized space metrics
    f.FreeSpaceInFileGB AS [Free_Space_Inside_File_GB], -- formalized space metrics

    CASE
        WHEN ic.ServiceTier LIKE '%Business Critical%' THEN CAST((ic.vCores * 4000) AS VARCHAR(30)) + ' (Pooled BC Total)'
        WHEN ic.ServiceTier LIKE '%General Purpose%' AND ic.TotalInstanceReservedGB > 16384 THEN CAST(ic.MaxInstanceIOPS AS VARCHAR(30)) + ' (Pooled SAN Total)'
        WHEN f.FileType = 'LOG' THEN
            CASE
                WHEN f.FileSizeGB <= 128 THEN '22 MB/s Cap (Shared Quota)'
                WHEN f.FileSizeGB <= 512 THEN '30 MB/s Cap (Shared Quota)'
                WHEN f.FileSizeGB <= 1024 THEN '50 MB/s Cap (Shared Quota)'
                ELSE '65 MB/s Cap (Shared Quota)'
            END
        WHEN f.FileSizeGB <= 32   THEN '120 IOPS'
        WHEN f.FileSizeGB <= 64   THEN '240 IOPS'
        WHEN f.FileSizeGB <= 128  THEN '500 IOPS'
        WHEN f.FileSizeGB <= 512  THEN '1100 IOPS'
        WHEN f.FileSizeGB <= 1024 THEN '2300 IOPS'
        WHEN f.FileSizeGB <= 2048 THEN '5000 IOPS'
        ELSE '7500 IOPS'
    END AS [Estimated_File_Max_IOPS_Or_Log_Cap],

    ISNULL(d.Live_File_IOPS, 0.00) AS [Live_Current_File_IOPS],
    d.IO_Stall_Delta_MS AS [Live_IO_Stall_Window_Delta_MS],
    CAST(
        CASE
            WHEN (d.Reads_Delta + d.Writes_Delta) = 0 THEN 0
            ELSE d.IO_Stall_Delta_MS / CAST((d.Reads_Delta + d.Writes_Delta) AS DECIMAL(12,2))
        END
    AS DECIMAL(12, 2)) AS [Live_Avg_Stall_Per_IO_MS],

    CASE
        WHEN ic.ServiceTier LIKE '%Business Critical%' THEN 'Healthy Subsystem (Pooled NVMe local SSDs)'
        WHEN ic.ServiceTier LIKE '%General Purpose%' AND ic.TotalInstanceReservedGB > 16384 THEN 'Healthy Subsystem (Next-gen GP Elastic SAN)'

        WHEN f.FileType = 'ROWS' AND (d.IO_Stall_Delta_MS / NULLIF((d.Reads_Delta + d.Writes_Delta), 0)) > 15 THEN
            'CRITICAL LATENCY: This file is causing PAGEIOLATCH wait states. ' +
            CASE
                WHEN f.FileSizeGB <= 32   THEN 'Action Required: Expand file size to 33 GB to scale up from 120 IOPS to 240 IOPS.'
                WHEN f.FileSizeGB <= 64   THEN 'Action Required: Expand file size to 65 GB to scale up from 240 IOPS to 500 IOPS.'
                WHEN f.FileSizeGB <= 128  THEN 'Action Required: Expand file size to 129 GB to scale up from 500 IOPS to 1,100 IOPS.'
                WHEN f.FileSizeGB <= 512  THEN 'Action Required: Expand file size to 513 GB to scale up from 1,100 IOPS to 2,300 IOPS.'
                WHEN f.FileSizeGB <= 1024 THEN 'Action Required: Expand file size to 1,025 GB to scale up from 2,300 IOPS to 5,000 IOPS.'
                WHEN f.FileSizeGB <= 2048 THEN 'Action Required: Expand file size to 2,049 GB to scale up from 5,000 IOPS to 7,500 IOPS.'
                ELSE 'Max file-level IOPS reached for this tier (7,500 IOPS). If stalls persist, you must scale up instance vCores.'
            END
        ELSE 'Healthy Storage Subsystem'
    END AS [Diagnostic_Action_Item],

    CASE
        WHEN ic.ServiceTier LIKE '%Business Critical%' OR (ic.ServiceTier LIKE '%General Purpose%' AND ic.TotalInstanceReservedGB > 16384) THEN '-- Performance is pooled; file sizing expansions are not required to scale IOPS.'
        WHEN f.FileType = 'ROWS' AND (d.IO_Stall_Delta_MS / NULLIF((d.Reads_Delta + d.Writes_Delta), 0)) > 15 THEN
            '-- ALTER DATABASE [' + f.DatabaseName + '] MODIFY FILE (NAME = N''' + f.LogicalName + ''', SIZE = ' +
            CASE
                WHEN f.FileSizeGB <= 32   THEN '33GB'
                WHEN f.FileSizeGB <= 64   THEN '65GB'
                WHEN f.FileSizeGB <= 128  THEN '129GB'
                WHEN f.FileSizeGB <= 512  THEN '513GB'
                WHEN f.FileSizeGB <= 1024 THEN '1025GB'
                WHEN f.FileSizeGB <= 2048 THEN '2049GB'
                ELSE CAST(CAST(f.FileSizeGB AS INT) AS VARCHAR(10)) + 'GB'
            END + ');'
        ELSE '-- File performance is healthy.'
    END AS [Safety_Script_To_Expand_File],

    -- Next Gen GP is recommended when classic GP cannot remediate: file already at the top
    -- IOPS bracket (>2048 GB), or the volume lacks the free GB needed for the next bracket.
    CASE
        WHEN ic.ServiceTier NOT LIKE '%General Purpose%' OR ic.TotalInstanceReservedGB > 16384 THEN 'N/A (Not classic General Purpose)'
        WHEN f.FileType = 'ROWS' AND (d.IO_Stall_Delta_MS / NULLIF((d.Reads_Delta + d.Writes_Delta), 0)) > 15
             AND (f.FileSizeGB > 2048 OR (ic.TotalInstanceReservedGB - ic.TotalInstanceUsedGB) < (tg.TargetGB - f.FileSizeGB))
            THEN 'RECOMMEND NEXT GEN GP: ' + @NextGenSpec
        ELSE 'Not needed for this file.'
    END AS [Next_Gen_GP_Recommendation]
FROM #MI_File_Stats f
LEFT JOIN #FileDeltas d ON f.DatabaseID = d.database_id AND f.FileID = d.file_id
CROSS JOIN #Instance_Config ic
CROSS APPLY (SELECT CASE
                WHEN f.FileSizeGB <= 32   THEN 33
                WHEN f.FileSizeGB <= 64   THEN 65
                WHEN f.FileSizeGB <= 128  THEN 129
                WHEN f.FileSizeGB <= 512  THEN 513
                WHEN f.FileSizeGB <= 1024 THEN 1025
                WHEN f.FileSizeGB <= 2048 THEN 2049
                ELSE f.FileSizeGB END AS TargetGB) tg
ORDER BY f.DatabaseName, f.FileType;


-- ====================================================
-- RESULT SET 2: INSTANCE VOLUME CAPACITY & IOPS OVERLOAD CHECK
-- ====================================================
SELECT
    ic.ServiceTier AS [Service_Tier],
    ic.vCores AS [Allocated_vCores],
    ic.MaxInstanceIOPS AS [Max_Instance_Data_IOPS_Ceiling],
    @CombinedLiveInstanceIOPS AS [Live_Total_Instance_Driven_IOPS],

    CASE
        WHEN @CombinedLiveInstanceIOPS >= (ic.MaxInstanceIOPS * 0.90)
            THEN 'CRITICAL OVERLOAD: Instance is consuming ' + CAST(CAST(@CombinedLiveInstanceIOPS AS INT) AS VARCHAR(10)) + ' IOPS. You are breaching 90% of our global vCore container ceiling (' + CAST(ic.MaxInstanceIOPS AS VARCHAR(10)) + ' IOPS). File expansions will fail to improve performance. Action Required: Upscale compute node vCores.'
        WHEN @CombinedLiveInstanceIOPS >= (ic.MaxInstanceIOPS * 0.70)
            THEN 'WARNING: High storage volume traffic. Consuming over 70% of instance bandwidth pool. Monitor closely.'
        ELSE 'Healthy: Combined instance storage workload is well within our VM container processing brackets.'
    END AS [Instance_IOPS_Alert_Status],

    ic.TotalInstanceReservedGB AS [Total_Volume_Size_GB],
    (ic.TotalInstanceReservedGB - ic.TotalInstanceUsedGB) AS [Volume_Free_Space_GB],
    CAST(IIF(ic.vCores * 4.5 > 120, 120, ic.vCores * 4.5) AS VARCHAR(10)) + ' MiB/s' AS [Instance_Log_Write_Ceiling],

    CASE
        WHEN ic.ServiceTier NOT LIKE '%General Purpose%' OR ic.TotalInstanceReservedGB > 16384
            THEN 'N/A (Not classic General Purpose)'
        WHEN (ic.TotalInstanceReservedGB - ic.TotalInstanceUsedGB) < (ic.TotalInstanceReservedGB * 0.10)
            THEN 'RECOMMEND NEXT GEN GP: Storage is insufficient (under 10% free of ' + CAST(ic.TotalInstanceReservedGB AS VARCHAR(20)) + ' GB). ' + @NextGenSpec
        WHEN @CombinedLiveInstanceIOPS >= (ic.MaxInstanceIOPS * 0.70)
            THEN 'RECOMMEND NEXT GEN GP: Classic GP IOPS ceiling is being approached and file expansion cannot raise the instance limit. ' + @NextGenSpec
        WHEN EXISTS (SELECT 1 FROM #MI_File_Stats WHERE FileType = 'ROWS' AND FileSizeGB > 2048)
            THEN 'RECOMMEND NEXT GEN GP: One or more data files are already at the top classic GP file bracket. ' + @NextGenSpec
        ELSE 'No immediate need; classic GP remediation (file expansion) remains available.'
    END AS [Next_Gen_GP_Recommendation]
FROM #Instance_Config ic;

-- Cleanup Environment
DROP TABLE #Instance_Config;
DROP TABLE #MI_File_Stats;
DROP TABLE #File_IO_Snapshot_1;
DROP TABLE #File_IO_Snapshot_2;
DROP TABLE #FileDeltas;
