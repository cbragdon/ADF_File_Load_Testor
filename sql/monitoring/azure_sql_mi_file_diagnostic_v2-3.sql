/*
================================================================================
VERSION 2.3
Azure SQL MI GP Storage, File/Instance Capacity, Live Throughput & Latency
================================================================================
Baseline: azure_sql_mi_file_diagnostic_v2-2.sql
Merged measurements: read/write IOPS, MiB/s, and separate latency from the
working 08/delta measurement layer.

Scope:
- All online databases visible to the executing login, including tempdb.
- The database context where the script starts is marked TARGET_DATABASE.
- Read-only against SQL MI. Uses local temporary tables only.

Important:
- Existing v2-2 capacity formulas are preserved as estimated model ceilings.
- WAITFOR DELAY '00:00:05' is preserved exactly.
- File expansion statements remain commented.
================================================================================
*/
SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET QUOTED_IDENTIFIER ON;
SET NUMERIC_ROUNDABORT OFF;
SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @TargetDatabaseID int = DB_ID();
DECLARE @TargetDatabaseName sysname = DB_NAME();
DECLARE @NextGenSpec nvarchar(600) =
    N'Next-gen General Purpose (Elastic SAN-backed) offers pooled instance IOPS not tied to individual file size. '
  + N'Verify current limits, regional availability, and service configuration in Microsoft documentation before changing the MI.';

DROP TABLE IF EXISTS #Instance_Config;
DROP TABLE IF EXISTS #MI_File_Stats;
DROP TABLE IF EXISTS #File_IO_Snapshot_1;
DROP TABLE IF EXISTS #File_IO_Snapshot_2;
DROP TABLE IF EXISTS #FileDeltas;
DROP TABLE IF EXISTS #FileAnalysis;

/* STEP 1: INSTANCE CONFIGURATION - PRESERVED FROM v2-2 */
CREATE TABLE #Instance_Config
(
    ServiceTier nvarchar(128),
    vCores int,
    TotalInstanceReservedGB decimal(12,2),
    TotalInstanceUsedGB decimal(12,2),
    MaxInstanceIOPS int,
    EstimatedInstanceLogWriteCeilingMiBps decimal(19,2)
);

;WITH RawConfig AS
(
    SELECT TOP (1)
        sku AS ServiceTier,
        virtual_core_count AS vCores,
        CAST(reserved_storage_mb / 1024.0 AS decimal(12,2)) AS TotalInstanceReservedGB,
        CAST(storage_space_used_mb / 1024.0 AS decimal(12,2)) AS TotalInstanceUsedGB
    FROM sys.server_resource_stats
    ORDER BY end_time DESC
)
INSERT #Instance_Config
SELECT
    ServiceTier,
    vCores,
    TotalInstanceReservedGB,
    TotalInstanceUsedGB,
    CASE
        WHEN ServiceTier LIKE N'%Business Critical%' THEN vCores * 4000
        WHEN ServiceTier LIKE N'%General Purpose%' AND TotalInstanceReservedGB > 16384 THEN
            IIF((TotalInstanceReservedGB * 3) > (vCores * 1600), vCores * 1600, TotalInstanceReservedGB * 3)
        ELSE
            CASE
                WHEN vCores <= 4 THEN 6400
                WHEN vCores = 8 THEN 8000
                WHEN vCores = 16 THEN 16000
                WHEN vCores = 32 THEN 32000
                ELSE 40000
            END
    END,
    CAST(IIF(vCores * 4.5 > 120, 120, vCores * 4.5) AS decimal(19,2))
FROM RawConfig;

/* STEP 2: INSTANCE-WIDE FILE METADATA */
CREATE TABLE #MI_File_Stats
(
    DatabaseID int NOT NULL,
    FileID int NOT NULL,
    DatabaseName sysname NOT NULL,
    LogicalName sysname NOT NULL,
    FileType varchar(10) NOT NULL,
    FileSizeGB decimal(12,2) NOT NULL,
    SpaceUsedGB decimal(12,2) NULL,
    FreeSpaceInFileGB decimal(12,2) NULL,
    PRIMARY KEY (DatabaseID, FileID)
);

DECLARE @DatabaseName sysname;
DECLARE @Sql nvarchar(max);
DECLARE DatabaseCursor CURSOR LOCAL FAST_FORWARD FOR
SELECT name
FROM sys.databases
WHERE state_desc = N'ONLINE';

OPEN DatabaseCursor;
FETCH NEXT FROM DatabaseCursor INTO @DatabaseName;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @Sql = N'
USE ' + QUOTENAME(@DatabaseName) + N';
INSERT #MI_File_Stats
(
    DatabaseID, FileID, DatabaseName, LogicalName, FileType,
    FileSizeGB, SpaceUsedGB, FreeSpaceInFileGB
)
SELECT
    DB_ID(), file_id, DB_NAME(), name, type_desc,
    CAST(size * 8.0 / 1024.0 / 1024.0 AS decimal(12,2)),
    CAST(
        CASE WHEN type_desc = ''LOG''
             THEN ISNULL((SELECT used_log_space_in_bytes * 1.0 / NULLIF(total_log_size_in_bytes,0) FROM sys.dm_db_log_space_usage) * size, 0)
             ELSE ISNULL(FILEPROPERTY(name, ''SpaceUsed''), 0) END
        * 8.0 / 1024.0 / 1024.0 AS decimal(12,2)),
    CAST(
        (size - CASE WHEN type_desc = ''LOG''
                     THEN ISNULL((SELECT used_log_space_in_bytes * 1.0 / NULLIF(total_log_size_in_bytes,0) FROM sys.dm_db_log_space_usage) * size, 0)
                     ELSE ISNULL(FILEPROPERTY(name, ''SpaceUsed''), 0) END)
        * 8.0 / 1024.0 / 1024.0 AS decimal(12,2))
FROM sys.database_files;';

    BEGIN TRY
        EXEC sys.sp_executesql @Sql;
    END TRY
    BEGIN CATCH
        PRINT CONCAT('Metadata skipped for ', QUOTENAME(@DatabaseName), ': ', ERROR_MESSAGE());
    END CATCH;

    FETCH NEXT FROM DatabaseCursor INTO @DatabaseName;
END;
CLOSE DatabaseCursor;
DEALLOCATE DatabaseCursor;

/* STEP 3: FIVE-SECOND INSTANCE-WIDE I/O SNAPSHOT */
CREATE TABLE #File_IO_Snapshot_1
(
    database_id int, file_id int,
    num_of_reads bigint, num_of_writes bigint,
    num_of_bytes_read bigint, num_of_bytes_written bigint,
    io_stall_read_ms bigint, io_stall_write_ms bigint,
    io_stall bigint, sample_ms bigint,
    PRIMARY KEY (database_id, file_id)
);
CREATE TABLE #File_IO_Snapshot_2
(
    database_id int, file_id int,
    num_of_reads bigint, num_of_writes bigint,
    num_of_bytes_read bigint, num_of_bytes_written bigint,
    io_stall_read_ms bigint, io_stall_write_ms bigint,
    io_stall bigint, sample_ms bigint,
    PRIMARY KEY (database_id, file_id)
);

INSERT #File_IO_Snapshot_1
SELECT database_id, file_id, num_of_reads, num_of_writes,
       num_of_bytes_read, num_of_bytes_written,
       io_stall_read_ms, io_stall_write_ms, io_stall, sample_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL);

WAITFOR DELAY '00:00:05';

INSERT #File_IO_Snapshot_2
SELECT database_id, file_id, num_of_reads, num_of_writes,
       num_of_bytes_read, num_of_bytes_written,
       io_stall_read_ms, io_stall_write_ms, io_stall, sample_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL);

/* STEP 4: DELTAS AND LIVE METRICS */
CREATE TABLE #FileDeltas
(
    database_id int NOT NULL,
    file_id int NOT NULL,
    Sample_Seconds decimal(19,3),
    Reads_Delta bigint, Writes_Delta bigint,
    Bytes_Read_Delta bigint, Bytes_Written_Delta bigint,
    Read_Stall_Delta_MS bigint, Write_Stall_Delta_MS bigint,
    IO_Stall_Delta_MS bigint,
    Read_IOPS decimal(19,2), Write_IOPS decimal(19,2), Live_File_IOPS decimal(19,2),
    Live_Read_MiBps decimal(19,2), Live_Write_MiBps decimal(19,2),
    Live_Avg_Read_Latency_MS decimal(19,2), Live_Avg_Write_Latency_MS decimal(19,2),
    Live_Avg_Stall_Per_IO_MS decimal(19,2),
    PRIMARY KEY (database_id, file_id)
);

;WITH DeltaSource AS
(
    SELECT
        s1.database_id, s1.file_id,
        (s2.sample_ms - s1.sample_ms) / 1000.0 AS Sample_Seconds,
        s2.num_of_reads - s1.num_of_reads AS Reads_Delta,
        s2.num_of_writes - s1.num_of_writes AS Writes_Delta,
        s2.num_of_bytes_read - s1.num_of_bytes_read AS Bytes_Read_Delta,
        s2.num_of_bytes_written - s1.num_of_bytes_written AS Bytes_Written_Delta,
        s2.io_stall_read_ms - s1.io_stall_read_ms AS Read_Stall_Delta_MS,
        s2.io_stall_write_ms - s1.io_stall_write_ms AS Write_Stall_Delta_MS,
        s2.io_stall - s1.io_stall AS IO_Stall_Delta_MS
    FROM #File_IO_Snapshot_1 AS s1
    JOIN #File_IO_Snapshot_2 AS s2
      ON s2.database_id = s1.database_id AND s2.file_id = s1.file_id
)
INSERT #FileDeltas
SELECT
    database_id, file_id,
    CAST(Sample_Seconds AS decimal(19,3)),
    Reads_Delta, Writes_Delta, Bytes_Read_Delta, Bytes_Written_Delta,
    Read_Stall_Delta_MS, Write_Stall_Delta_MS, IO_Stall_Delta_MS,
    CAST(Reads_Delta / NULLIF(Sample_Seconds,0) AS decimal(19,2)),
    CAST(Writes_Delta / NULLIF(Sample_Seconds,0) AS decimal(19,2)),
    CAST((Reads_Delta + Writes_Delta) / NULLIF(Sample_Seconds,0) AS decimal(19,2)),
    CAST(Bytes_Read_Delta / 1048576.0 / NULLIF(Sample_Seconds,0) AS decimal(19,2)),
    CAST(Bytes_Written_Delta / 1048576.0 / NULLIF(Sample_Seconds,0) AS decimal(19,2)),
    CAST(ISNULL(Read_Stall_Delta_MS * 1.0 / NULLIF(Reads_Delta,0),0) AS decimal(19,2)),
    CAST(ISNULL(Write_Stall_Delta_MS * 1.0 / NULLIF(Writes_Delta,0),0) AS decimal(19,2)),
    CAST(ISNULL(IO_Stall_Delta_MS * 1.0 / NULLIF(Reads_Delta + Writes_Delta,0),0) AS decimal(19,2))
FROM DeltaSource;

DECLARE @CombinedLiveInstanceIOPS decimal(19,2) =
    (SELECT ISNULL(SUM(Live_File_IOPS),0) FROM #FileDeltas);
DECLARE @ObservedInstanceLogWriteMiBps decimal(19,2) =
    (SELECT ISNULL(SUM(CASE WHEN f.FileType = 'LOG' THEN d.Live_Write_MiBps ELSE 0 END),0)
     FROM #MI_File_Stats AS f
     LEFT JOIN #FileDeltas AS d ON d.database_id=f.DatabaseID AND d.file_id=f.FileID);
DECLARE @MaxInstanceIOPS decimal(19,2) = (SELECT TOP (1) MaxInstanceIOPS FROM #Instance_Config);
DECLARE @EstimatedInstanceLogCeiling decimal(19,2) =
    (SELECT TOP (1) EstimatedInstanceLogWriteCeilingMiBps FROM #Instance_Config);
DECLARE @InstanceIOPSUtilization decimal(9,2) =
    100.0 * @CombinedLiveInstanceIOPS / NULLIF(@MaxInstanceIOPS,0);
DECLARE @InstanceLogUtilization decimal(9,2) =
    100.0 * @ObservedInstanceLogWriteMiBps / NULLIF(@EstimatedInstanceLogCeiling,0);

/* Build reusable file analysis while preserving v2-2 estimated brackets. */
SELECT
    f.*,
    CASE
        WHEN ic.ServiceTier LIKE N'%Business Critical%' THEN CAST(ic.vCores * 4000 AS decimal(19,2))
        WHEN ic.ServiceTier LIKE N'%General Purpose%' AND ic.TotalInstanceReservedGB > 16384 THEN CAST(ic.MaxInstanceIOPS AS decimal(19,2))
        WHEN f.FileType = 'ROWS' THEN
            CASE WHEN f.FileSizeGB <= 32 THEN 120 WHEN f.FileSizeGB <= 64 THEN 240
                 WHEN f.FileSizeGB <= 128 THEN 500 WHEN f.FileSizeGB <= 512 THEN 1100
                 WHEN f.FileSizeGB <= 1024 THEN 2300 WHEN f.FileSizeGB <= 2048 THEN 5000 ELSE 7500 END
    END AS EstimatedFileIOPSCeiling,
    CASE WHEN f.FileType = 'LOG' THEN
        CASE WHEN f.FileSizeGB <= 128 THEN 22 WHEN f.FileSizeGB <= 512 THEN 30
             WHEN f.FileSizeGB <= 1024 THEN 50 ELSE 65 END END AS EstimatedDatabaseLogCapMBps,
    ISNULL(d.Sample_Seconds,0) AS Sample_Seconds,
    ISNULL(d.Read_IOPS,0) AS Read_IOPS,
    ISNULL(d.Write_IOPS,0) AS Write_IOPS,
    ISNULL(d.Live_File_IOPS,0) AS Live_File_IOPS,
    ISNULL(d.Live_Read_MiBps,0) AS Live_Read_MiBps,
    ISNULL(d.Live_Write_MiBps,0) AS Live_Write_MiBps,
    ISNULL(d.Live_Avg_Read_Latency_MS,0) AS Live_Avg_Read_Latency_MS,
    ISNULL(d.Live_Avg_Write_Latency_MS,0) AS Live_Avg_Write_Latency_MS,
    ISNULL(d.IO_Stall_Delta_MS,0) AS IO_Stall_Delta_MS,
    ISNULL(d.Live_Avg_Stall_Per_IO_MS,0) AS Live_Avg_Stall_Per_IO_MS,
    CAST(100.0 * ISNULL(d.Live_File_IOPS,0) /
         NULLIF(CASE WHEN f.FileType='ROWS' THEN
            CASE WHEN f.FileSizeGB <= 32 THEN 120 WHEN f.FileSizeGB <= 64 THEN 240
                 WHEN f.FileSizeGB <= 128 THEN 500 WHEN f.FileSizeGB <= 512 THEN 1100
                 WHEN f.FileSizeGB <= 1024 THEN 2300 WHEN f.FileSizeGB <= 2048 THEN 5000 ELSE 7500 END END,0)
         AS decimal(9,2)) AS EstimatedFileIOPSUtilizationPercent
INTO #FileAnalysis
FROM #MI_File_Stats AS f
LEFT JOIN #FileDeltas AS d ON d.database_id=f.DatabaseID AND d.file_id=f.FileID
CROSS JOIN #Instance_Config AS ic;

/* RESULT SET 1: DETAILED FILE PROFILE AND GUIDANCE - ENHANCED v2-2 */
SELECT
    DatabaseName, LogicalName, FileType,
    FileSizeGB AS Current_File_Size_GB,
    SpaceUsedGB AS Space_Used_Inside_File_GB,
    FreeSpaceInFileGB AS Free_Space_Inside_File_GB,
    CASE
        WHEN ic.ServiceTier LIKE '%Business Critical%' THEN CAST((ic.vCores * 4000) AS varchar(30)) + ' (Pooled BC Total)'
        WHEN ic.ServiceTier LIKE '%General Purpose%' AND ic.TotalInstanceReservedGB > 16384 THEN CAST(ic.MaxInstanceIOPS AS varchar(30)) + ' (Pooled SAN Total)'
        WHEN FileType='LOG' THEN
            CASE WHEN FileSizeGB <= 128 THEN '22 MB/s Cap (Shared Quota)'
                 WHEN FileSizeGB <= 512 THEN '30 MB/s Cap (Shared Quota)'
                 WHEN FileSizeGB <= 1024 THEN '50 MB/s Cap (Shared Quota)'
                 ELSE '65 MB/s Cap (Shared Quota)' END
        WHEN FileSizeGB <= 32 THEN '120 IOPS'
        WHEN FileSizeGB <= 64 THEN '240 IOPS'
        WHEN FileSizeGB <= 128 THEN '500 IOPS'
        WHEN FileSizeGB <= 512 THEN '1100 IOPS'
        WHEN FileSizeGB <= 1024 THEN '2300 IOPS'
        WHEN FileSizeGB <= 2048 THEN '5000 IOPS'
        ELSE '7500 IOPS'
    END AS Estimated_File_Max_IOPS_Or_Log_Cap,
    Live_File_IOPS AS Live_Current_File_IOPS,
    IO_Stall_Delta_MS AS Live_IO_Stall_Window_Delta_MS,
    Live_Avg_Stall_Per_IO_MS,
    CASE
        WHEN FileType='ROWS' AND EstimatedFileIOPSUtilizationPercent>=70
             AND (Live_Avg_Read_Latency_MS>15 OR Live_Avg_Write_Latency_MS>15)
             AND ISNULL(@InstanceIOPSUtilization,0)<70 THEN
            'CRITICAL LATENCY: This file is causing PAGEIOLATCH wait states. ' +
            CASE WHEN FileSizeGB<=32 THEN 'Action Required: Expand file size to 33 GB to scale up from 120 IOPS to 240 IOPS.'
                 WHEN FileSizeGB<=64 THEN 'Action Required: Expand file size to 65 GB to scale up from 240 IOPS to 500 IOPS.'
                 WHEN FileSizeGB<=128 THEN 'Action Required: Expand file size to 129 GB to scale up from 500 IOPS to 1,100 IOPS.'
                 WHEN FileSizeGB<=512 THEN 'Action Required: Expand file size to 513 GB to scale up from 1,100 IOPS to 2,300 IOPS.'
                 WHEN FileSizeGB<=1024 THEN 'Action Required: Expand file size to 1,025 GB to scale up from 2,300 IOPS to 5,000 IOPS.'
                 WHEN FileSizeGB<=2048 THEN 'Action Required: Expand file size to 2,049 GB to scale up from 5,000 IOPS to 7,500 IOPS.'
                 ELSE 'Max file-level IOPS reached for this tier (7,500 IOPS). If stalls persist, you must scale up instance vCores.' END
        ELSE 'Healthy Storage Subsystem'
    END AS Diagnostic_Action_Item,
    CASE
        WHEN FileType='ROWS' AND EstimatedFileIOPSUtilizationPercent>=70
             AND (Live_Avg_Read_Latency_MS>15 OR Live_Avg_Write_Latency_MS>15)
             AND ISNULL(@InstanceIOPSUtilization,0)<70 THEN
            '-- ALTER DATABASE ' + QUOTENAME(DatabaseName) + ' MODIFY FILE (NAME = N''' + REPLACE(LogicalName,'''','''''') + ''', SIZE = ' +
            CASE WHEN FileSizeGB<=32 THEN '33GB' WHEN FileSizeGB<=64 THEN '65GB'
                 WHEN FileSizeGB<=128 THEN '129GB' WHEN FileSizeGB<=512 THEN '513GB'
                 WHEN FileSizeGB<=1024 THEN '1025GB' WHEN FileSizeGB<=2048 THEN '2049GB'
                 ELSE CAST(CAST(FileSizeGB AS int) AS varchar(20))+'GB' END + ');'
        ELSE '-- File performance is healthy.'
    END AS Safety_Script_To_Expand_File,
    CASE
        WHEN ISNULL(@InstanceIOPSUtilization,0)>=70 OR ISNULL(@InstanceLogUtilization,0)>=70
          OR (FileType='ROWS' AND FileSizeGB>2048) THEN 'RECOMMEND EVALUATION: ' + @NextGenSpec
        ELSE 'No immediate need; classic GP remediation (file expansion) remains available.'
    END AS Next_Gen_GP_Recommendation,
    Sample_Seconds, Read_IOPS, Write_IOPS, Live_File_IOPS,
    Live_Read_MiBps, Live_Write_MiBps,
    Live_Avg_Read_Latency_MS, Live_Avg_Write_Latency_MS,
    EstimatedFileIOPSUtilizationPercent,
    @InstanceIOPSUtilization AS Estimated_Instance_IOPS_Utilization_Percent,
    CASE
        WHEN FileType='ROWS' AND EstimatedFileIOPSUtilizationPercent>=70
             AND (Live_Avg_Read_Latency_MS>15 OR Live_Avg_Write_Latency_MS>15)
             AND ISNULL(@InstanceIOPSUtilization,0)<70 THEN 'FILE_LEVEL'
        WHEN ISNULL(@InstanceIOPSUtilization,0)>=70 OR ISNULL(@InstanceLogUtilization,0)>=70 THEN 'INSTANCE_LEVEL'
        WHEN FileType='ROWS' AND (Live_Avg_Read_Latency_MS>15 OR Live_Avg_Write_Latency_MS>15) THEN 'NOT_PROVEN'
        ELSE 'HEALTHY_OR_NO_ACTIVITY'
    END AS Constraint_Classification,
    CASE
        WHEN ISNULL(@InstanceIOPSUtilization,0)>=70 OR ISNULL(@InstanceLogUtilization,0)>=70
            THEN 'File expansion is unlikely to resolve the primary constraint. Evaluate MI capacity/configuration.'
        WHEN FileType='ROWS' AND (Live_Avg_Read_Latency_MS>15 OR Live_Avg_Write_Latency_MS>15)
            THEN 'Elevated latency is present, but file/instance ceiling proximity is not proven. Investigate waits and repeat the sample before resizing.'
        ELSE 'No additional file or instance capacity constraint is proven in this sample.'
    END AS Merged_Recommendation
FROM #FileAnalysis
CROSS JOIN #Instance_Config AS ic
ORDER BY DatabaseName, FileType DESC, LogicalName;

/* RESULT SET 2: INSTANCE CAPACITY AND OVERLOAD DIAGNOSIS - ENHANCED v2-2 */
SELECT
    ic.ServiceTier AS Service_Tier,
    ic.vCores AS Allocated_vCores,
    ic.MaxInstanceIOPS AS Max_Instance_Data_IOPS_Ceiling,
    @CombinedLiveInstanceIOPS AS Live_Total_Instance_Driven_IOPS,
    CASE
        WHEN ISNULL(@InstanceIOPSUtilization,0)>=90 THEN 'CRITICAL OVERLOAD: Instance is consuming ' + CAST(CAST(@CombinedLiveInstanceIOPS AS int) AS varchar(10)) + ' IOPS. You are breaching 90% of our global vCore container ceiling (' + CAST(ic.MaxInstanceIOPS AS varchar(10)) + ' IOPS). File expansions will fail to improve performance. Action Required: Upscale compute node vCores.'
        WHEN ISNULL(@InstanceIOPSUtilization,0)>=70 THEN 'WARNING: High storage volume traffic. Consuming over 70% of instance bandwidth pool. Monitor closely.'
        ELSE 'Healthy: Combined instance storage workload is well within our VM container processing brackets.'
    END AS Instance_IOPS_Alert_Status,
    ic.TotalInstanceReservedGB AS Total_Volume_Size_GB,
    ic.TotalInstanceReservedGB-ic.TotalInstanceUsedGB AS Volume_Free_Space_GB,
    CAST(ic.EstimatedInstanceLogWriteCeilingMiBps AS varchar(20)) + ' MiB/s' AS Instance_Log_Write_Ceiling,
    CASE
        WHEN ic.ServiceTier NOT LIKE N'%General Purpose%' OR ic.TotalInstanceReservedGB>16384 THEN 'N/A (Not classic General Purpose)'
        WHEN ic.TotalInstanceReservedGB-ic.TotalInstanceUsedGB < ic.TotalInstanceReservedGB*0.10
          OR ISNULL(@InstanceIOPSUtilization,0)>=70
          OR EXISTS (SELECT 1 FROM #FileAnalysis WHERE FileType='ROWS' AND FileSizeGB>2048)
        THEN 'RECOMMEND EVALUATION: '+@NextGenSpec
        ELSE 'No immediate need; classic GP remediation (file expansion) remains available.'
    END AS Next_Gen_GP_Recommendation,
    @InstanceIOPSUtilization AS Estimated_Instance_IOPS_Utilization_Percent,
    ic.TotalInstanceUsedGB AS Total_Volume_Used_GB,
    @ObservedInstanceLogWriteMiBps AS Observed_Instance_Log_Write_MiBps,
    @InstanceLogUtilization AS Estimated_Instance_Log_Utilization_Percent,
    CASE
        WHEN ISNULL(@InstanceIOPSUtilization,0)>=70 AND EXISTS
             (SELECT 1 FROM #FileAnalysis WHERE EstimatedFileIOPSUtilizationPercent>=70) THEN 'MIXED'
        WHEN ISNULL(@InstanceIOPSUtilization,0)>=70 OR ISNULL(@InstanceLogUtilization,0)>=70 THEN 'INSTANCE_LEVEL'
        WHEN EXISTS (SELECT 1 FROM #FileAnalysis WHERE EstimatedFileIOPSUtilizationPercent>=70
                     AND (Live_Avg_Read_Latency_MS>15 OR Live_Avg_Write_Latency_MS>15)) THEN 'FILE_LEVEL'
        ELSE 'NOT_PROVEN'
    END AS Capacity_Constraint_Classification,
    CASE
        WHEN ISNULL(@InstanceLogUtilization,0)>=70 THEN
            'Observed log write throughput is near the estimated instance ceiling. Correlate with WRITELOG and INSTANCE_LOG_RATE_GOVERNOR; additional ADF writers may not help.'
        WHEN ISNULL(@InstanceIOPSUtilization,0)>=70 THEN
            'Observed data IOPS is near the estimated instance ceiling. Single-file expansion is unlikely to resolve an instance-wide limit.'
        WHEN EXISTS (SELECT 1 FROM #FileAnalysis WHERE EstimatedFileIOPSUtilizationPercent>=70
                     AND (Live_Avg_Read_Latency_MS>15 OR Live_Avg_Write_Latency_MS>15)) THEN
            'Instance has headroom but one or more files show high latency near their estimated file ceiling. Evaluate file-level remediation.'
        ELSE 'This sample does not prove a file-size or instance-capacity constraint. Correlate with waits and repeat during sustained load.'
    END AS Evidence_Based_Next_Action
FROM #Instance_Config AS ic;

/* RESULT SET 3: DATABASE ACTIVITY BREAKDOWN */
;WITH DatabaseActivity AS
(
    SELECT
        DatabaseID, DatabaseName,
        CASE WHEN DatabaseID=@TargetDatabaseID THEN 1
             WHEN DatabaseName='tempdb' THEN 2
             WHEN DatabaseID>4 THEN 3 ELSE 4 END AS DisplayOrder,
        CASE WHEN DatabaseID<=4 THEN 1 ELSE 0 END AS IsSystemDatabase,
        SUM(Read_IOPS) AS Read_IOPS,
        SUM(Write_IOPS) AS Write_IOPS,
        SUM(Live_File_IOPS) AS Total_IOPS,
        SUM(Live_Read_MiBps) AS Read_MiBps,
        SUM(Live_Write_MiBps) AS Write_MiBps,
        SUM(CASE WHEN FileType='LOG' THEN Live_Write_MiBps ELSE 0 END) AS Log_Write_MiBps
    FROM #FileAnalysis
    GROUP BY DatabaseID, DatabaseName
)
SELECT
    DatabaseName,
    CAST(Read_IOPS AS decimal(19,2)) AS Read_IOPS,
    CAST(Write_IOPS AS decimal(19,2)) AS Write_IOPS,
    CAST(Total_IOPS AS decimal(19,2)) AS Total_IOPS,
    CAST(Read_MiBps AS decimal(19,2)) AS Read_MiBps,
    CAST(Write_MiBps AS decimal(19,2)) AS Write_MiBps,
    CAST(Log_Write_MiBps AS decimal(19,2)) AS Log_Write_MiBps,
    CAST(ISNULL(100.0*Total_IOPS/NULLIF(@CombinedLiveInstanceIOPS,0),0) AS decimal(9,2)) AS Percent_Of_Observed_Instance_IOPS
FROM DatabaseActivity
ORDER BY Total_IOPS DESC;

DROP TABLE #FileAnalysis;
DROP TABLE #FileDeltas;
DROP TABLE #File_IO_Snapshot_2;
DROP TABLE #File_IO_Snapshot_1;
DROP TABLE #MI_File_Stats;
DROP TABLE #Instance_Config;