/* 02_monitor_log_space.sql
   Purpose: Measure current transaction-log allocation and usage during an ADF load.
   Scope: Run repeatedly in the target user database before, during, and after a test.
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
    DB_NAME() AS DatabaseName,
    CAST(total_log_size_in_bytes / 1073741824.0 AS decimal(18,3)) AS TotalLogSizeGiB,
    CAST(used_log_space_in_bytes / 1073741824.0 AS decimal(18,3)) AS UsedLogSpaceGiB,
    CAST(used_log_space_in_percent AS decimal(9,2)) AS UsedLogPercent,
    CAST((total_log_size_in_bytes - used_log_space_in_bytes) / 1073741824.0 AS decimal(18,3)) AS AvailableLogSpaceGiB
FROM sys.dm_db_log_space_usage;
