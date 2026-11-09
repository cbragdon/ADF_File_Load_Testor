# Delimited File Generator

[![Test delimited file generator](https://github.com/cbragdon/ADF_Performance_Toolkit/actions/workflows/test.yml/badge.svg)](https://github.com/cbragdon/ADF_Performance_Toolkit/actions/workflows/test.yml)

`generate_delimited_file.py` creates delimited files for Azure Data Factory
load testing. It streams records directly to the output file, so memory use
does not grow with the requested row count.

By default, the generated columns are `record_id`, `payload`, and
`created_at_utc`. To use column names from SQL Server or Azure SQL Managed
Instance, provide both `--connection-string` and `--header-query`.

## Requirements

- Python 3.9 or later
- No additional Python package when using the default generated headers

### When database headers are needed

Install `pyodbc` only when you use both `--connection-string` and
`--header-query` to read column names from SQL Server or Azure SQL Managed
Instance. The package lets the generator connect to the database; it is not
needed for files that use the default `record_id`, `payload`, and
`created_at_utc` headers.

The dependency list is in the repository-root
[`requirements.txt`](../requirements.txt) file. From the repository root, run:

```powershell
python -m pip install -r .\requirements.txt
```

You also need a compatible SQL Server ODBC driver on the computer running the
script when retrieving database headers.

## Usage

Generate one million comma-delimited records with the default headers:

```powershell
python .\scripts\generate_delimited_file.py .\data\load_input.csv --rows 1000000
```

Generate pipe-delimited output with Windows-1252 encoding:

```powershell
python .\scripts\generate_delimited_file.py .\data\load_input.psv --rows 500000 --delimiter "|" --encoding "cp1252"
```

## Database-derived headers

Use a query that returns the required columns. A zero-row `SELECT` is
recommended because only the result-set column names are used:

```sql
SELECT TOP (0) order_id, customer_id, amount, created_at_utc
FROM dbo.orders;
```

The script rejects connection strings containing a user name or password. Its
connection string must use the executing person's authentication:
`Trusted_Connection=Yes`, `Integrated Security=SSPI`, or
`Authentication=ActiveDirectoryDefault`, `ActiveDirectoryIntegrated`, or
`ActiveDirectoryInteractive`.

### SQL Server VM with Windows Integrated Authentication

```powershell
python .\scripts\generate_delimited_file.py .\data\orders.csv --rows 1000000 --connection-string "Driver={ODBC Driver 18 for SQL Server};Server=sqlvm.contoso.com;Database=Sales;Trusted_Connection=Yes;Encrypt=Yes;TrustServerCertificate=Yes;" --header-query "SELECT TOP (0) order_id, customer_id, amount, created_at_utc FROM dbo.orders"
```

### Azure SQL Managed Instance with Microsoft Entra Integrated Authentication

```powershell
python .\scripts\generate_delimited_file.py .\data\orders.csv --rows 1000000 --connection-string "Driver={ODBC Driver 18 for SQL Server};Server=managed-instance.public.database.windows.net;Database=Sales;Authentication=ActiveDirectoryIntegrated;Encrypt=Yes;TrustServerCertificate=No;" --header-query "SELECT TOP (0) order_id, customer_id, amount, created_at_utc FROM dbo.orders"
```

For Microsoft Entra authentication, the executing user must already be signed
in and authorized for the target database. `Authentication=ActiveDirectoryDefault`
is also suitable when the environment provides that credential.

## Generated values

The script generates deterministic values based on each record number. Known
column names such as `record_id`, `payload`, and `created_at_utc` receive their
corresponding default values. Other database-derived column names receive a
unique `<column-name>_<record-number>` value. This provides valid delimited
load-test data while preserving the database column names.

## Verification

This script is verified by `tests\test_generate_delimited_file.py` and the
repository's [GitHub Actions workflow](https://github.com/cbragdon/ADF_Performance_Toolkit/actions/workflows/test.yml).
The automated checks validate:

- Generation of the requested number of rows with the default headers.
- Command-line encoding, delimiter, and quote-character options.
- Database-derived header retrieval through a mocked `pyodbc` connection.
- Integrated authentication acceptance and rejection of stored SQL credentials.

Run the same verification from the repository root:

```powershell
python -m unittest tests/test_generate_delimited_file.py -v
```
