/* 06_monitor_file_io_latency.sql
   Purpose: Measure cumulative data/log file I/O and average write latency.
   Usage: Capture results immediately before and after each ADF load and compare deltas.
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
SELECT
    DB_NAME(vfs.database_id) AS DatabaseName,
    mf.name AS LogicalFileName,
    mf.type_desc AS FileType,
    vfs.num_of_reads AS NumberOfReads,
    vfs.num_of_writes AS NumberOfWrites,
    CAST(vfs.num_of_bytes_read / 1073741824.0 AS decimal(18,3)) AS GiBRead,
    CAST(vfs.num_of_bytes_written / 1073741824.0 AS decimal(18,3)) AS GiBWritten,
    vfs.io_stall_read_ms AS ReadStallMilliseconds,
    vfs.io_stall_write_ms AS WriteStallMilliseconds,
    CAST(CASE WHEN vfs.num_of_reads = 0 THEN 0
              ELSE vfs.io_stall_read_ms * 1.0 / vfs.num_of_reads END AS decimal(18,3)) AS AverageReadLatencyMs,
    CAST(CASE WHEN vfs.num_of_writes = 0 THEN 0
              ELSE vfs.io_stall_write_ms * 1.0 / vfs.num_of_writes END AS decimal(18,3)) AS AverageWriteLatencyMs
FROM sys.dm_io_virtual_file_stats(DB_ID(), NULL) AS vfs
INNER JOIN sys.database_files AS mf
    ON mf.file_id = vfs.file_id
ORDER BY mf.type_desc, mf.file_id;
