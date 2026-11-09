/* 04_check_log_file_configuration.sql
   Purpose: Review transaction-log file allocation, maximum size, and autogrowth settings.
   Scope: Run before benchmark tests so file growth does not become an uncontrolled test variable.
   Safety: Read-only.
   Note: Do not shrink the log between benchmark runs; that changes test conditions.
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
    name AS LogicalFileName,
    type_desc AS FileType,
    CAST(size * 8.0 / 1024.0 AS decimal(18,2)) AS AllocatedSizeMB,
    CASE
        WHEN max_size = -1 THEN NULL
        ELSE CAST(max_size * 8.0 / 1024.0 AS decimal(18,2))
    END AS MaximumSizeMB,
    is_percent_growth AS UsesPercentageGrowth,
    CASE
        WHEN is_percent_growth = 1 THEN CAST(growth AS decimal(18,2))
        ELSE CAST(growth * 8.0 / 1024.0 AS decimal(18,2))
    END AS GrowthValue,
    CASE WHEN is_percent_growth = 1 THEN N'PERCENT' ELSE N'MB' END AS GrowthUnit
FROM sys.database_files
WHERE type_desc = N'LOG';
