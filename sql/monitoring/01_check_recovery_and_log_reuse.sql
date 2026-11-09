/* 01_check_recovery_and_log_reuse.sql
   Purpose: Confirm the current database recovery model and the reason log space cannot currently be reused.
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
    d.name AS DatabaseName,
    d.recovery_model_desc AS RecoveryModel,
    d.log_reuse_wait_desc AS LogReuseWait,
    d.is_auto_shrink_on AS AutoShrinkEnabled
FROM sys.databases AS d
WHERE d.database_id = DB_ID();
