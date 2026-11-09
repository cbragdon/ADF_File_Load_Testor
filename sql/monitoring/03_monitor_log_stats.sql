/* 03_monitor_log_stats.sql
   Purpose: Inspect detailed transaction-log statistics, VLF counts, truncation holdup, and last log backup time.
   Scope: Run in the target user database on Azure SQL Managed Instance.
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
    DB_NAME(database_id) AS DatabaseName,
    recovery_model,
    log_min_lsn,
    log_end_lsn,
    current_vlf_sequence_number,
    active_vlf_count,
    total_vlf_count,
    CAST(total_log_size_mb / 1024.0 AS decimal(18,3)) AS TotalLogSizeGiB,
    CAST(active_log_size_mb / 1024.0 AS decimal(18,3)) AS ActiveLogSizeGiB,
    log_truncation_holdup_reason,
    log_backup_time
FROM sys.dm_db_log_stats(DB_ID());
