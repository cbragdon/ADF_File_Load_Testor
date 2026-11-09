/* 05_find_active_log_transactions.sql
   Purpose: Identify active transactions consuming/reserving log space, especially when log_reuse_wait_desc = ACTIVE_TRANSACTION.
   Scope: Run in the target user database during an ADF load.
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
    at.transaction_id,
    at.transaction_begin_time,
    at.transaction_type,
    at.transaction_state,
    dt.database_transaction_begin_time,
    CAST(dt.database_transaction_log_bytes_used / 1048576.0 AS decimal(18,2)) AS LogMBUsed,
    CAST(dt.database_transaction_log_bytes_reserved / 1048576.0 AS decimal(18,2)) AS LogMBReserved,
    st.session_id,
    s.login_name,
    s.host_name,
    s.program_name,
    r.status AS RequestStatus,
    r.command,
    r.wait_type,
    r.wait_time,
    r.blocking_session_id,
    r.total_elapsed_time,
    SUBSTRING(
        sqltext.text,
        (r.statement_start_offset / 2) + 1,
        ((CASE WHEN r.statement_end_offset = -1 THEN DATALENGTH(sqltext.text)
               ELSE r.statement_end_offset END - r.statement_start_offset) / 2) + 1
    ) AS CurrentStatement
FROM sys.dm_tran_active_transactions AS at
INNER JOIN sys.dm_tran_database_transactions AS dt
    ON dt.transaction_id = at.transaction_id
LEFT JOIN sys.dm_tran_session_transactions AS st
    ON st.transaction_id = at.transaction_id
LEFT JOIN sys.dm_exec_sessions AS s
    ON s.session_id = st.session_id
LEFT JOIN sys.dm_exec_requests AS r
    ON r.session_id = st.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) AS sqltext
WHERE dt.database_id = DB_ID()
ORDER BY dt.database_transaction_log_bytes_used DESC;
