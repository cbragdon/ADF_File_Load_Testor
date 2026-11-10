# Delimited File Generator

[![Test delimited file generator](https://github.com/cbragdon/ADF_Performance_Toolkit/actions/workflows/test.yml/badge.svg)](https://github.com/cbragdon/ADF_Performance_Toolkit/actions/workflows/test.yml)

`generate_delimited_file.py` creates synthetic delimited files for Azure Data
Factory performance testing. It supports three generation paths:

| Path | When to use it | Data source |
| --- | --- | --- |
| Standalone schema | You do not need database metadata. | A JSON configuration file. |
| Schema-aware database | You need a file that matches a table's shape. | SQL Server metadata only. |
| SDV synthetic data | You need values statistically modeled from source data. | SQL Server metadata plus a read-only sample. |

The generator never accepts a user name, password, client secret, access token,
or credential-bearing connection string. Database access always uses the
identity of the person running the script.

## Requirements

- Python 3.9 or later
- `pyodbc` and Microsoft ODBC Driver 18 for SQL Server for database generation
- `sdv`, `pandas`, and their optional dependencies only for SDV generation

Install the base dependency for database access:

```powershell
python -m pip install -r .\requirements.txt
```

`requirements.txt` is in the repository root. You can skip this installation
when using only standalone JSON-schema generation.

## Standalone schema generation

Use the example schema in `config\generate_delimited_file.example.json` when
you do not need a database connection:

```powershell
python .\scripts\generate_delimited_file.py `
  --config .\config\generate_delimited_file.example.json `
  --engine schema `
  --rows 1000 `
  --delimiter "|" `
  --quotechar "'" `
  --output .\output\standalone.txt
```

This writes 1,000 data records plus the configured header. The `output`
directory is ignored by Git so generated test data is never committed.

## SQL Server schema-aware generation

Use `--engine schema` to discover the table columns without reading source
rows. Computed and `rowversion`/`timestamp` columns are excluded by default.
Identity columns remain included because a file-generation workload may need
them; use the output only where that behavior is appropriate.

```powershell
python .\scripts\generate_delimited_file.py `
  --server "DESKTOP-6BVBI90" `
  --database "AdventureWorks2019" `
  --schema "Sales" `
  --table "SalesOrderHeaderEnlarged" `
  --auth windows `
  --engine schema `
  --rows 1000 `
  --output .\output\SalesOrderHeaderEnlarged_schema.csv `
  --trust-server-certificate
```

`--auth windows` uses Windows Integrated Authentication
(`Trusted_Connection=yes`) for the account running Python. The
`--trust-server-certificate` option is appropriate only for a controlled local
development SQL Server with an untrusted certificate. Do not use it for normal
Azure SQL Managed Instance connections.

For Azure SQL Managed Instance, use Microsoft Entra authentication without
embedding credentials:

```powershell
python .\scripts\generate_delimited_file.py `
  --server "my-mi.public.database.windows.net,3342" `
  --database "AdventureWorks" `
  --schema "Sales" `
  --table "SalesOrderHeader" `
  --auth entra-default `
  --engine schema `
  --rows 1000 `
  --output .\output\SalesOrderHeader_schema.csv
```

Use `--auth entra-interactive` when an interactive Microsoft Entra sign-in is
required. `entra-default` obtains the current user's token through
`DefaultAzureCredential`.

## SDV synthetic data

SDV is optional. Install it in an isolated virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\requirements-sdv.txt
.\.venv\Scripts\python.exe .\scripts\generate_delimited_file.py --verify-sdv
```

With SDV installed, `--engine auto` reads a controlled sample, trains an SDV
model in memory, and produces synthetic rows:

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_delimited_file.py `
  --server "DESKTOP-6BVBI90" `
  --database "AdventureWorks2019" `
  --schema "Sales" `
  --table "SalesOrderHeaderEnlarged" `
  --auth windows `
  --engine auto `
  --sample-rows 5000 `
  --rows 10000 `
  --output .\output\SalesOrderHeaderEnlarged_sdv.csv `
  --trust-server-certificate
```

If SDV is unavailable or fails, `--engine auto` falls back to schema-aware
generation and reports the reason. Use `--engine sdv` or `--no-fallback` when
failure must stop the command instead. SDV sampling and generation occur in
memory, so do not use this engine for multi-gigabyte output until chunked
generation and model reuse are implemented.

## Output options

| Option | Default | Purpose |
| --- | --- | --- |
| `--rows` | Required | Number of data records to generate. |
| `--output` | Required | Generated file path. |
| `--delimiter` | `,` | One-character field delimiter. |
| `--quotechar` | `"` | One-character CSV quote character. |
| `--encoding` | `utf-8` | Output encoding. |
| `--no-header` | Disabled | Omit the output header row. |
| `--include-server-generated` | Disabled | Include computed and rowversion/timestamp columns. |
| `--seed` | `42` | Makes schema-generated values repeatable. |

## Verification

The generator has been tested against the local SQL Server instance
`DESKTOP-6BVBI90`, database `AdventureWorks2019`, and table
`Sales.SalesOrderHeaderEnlarged` with Windows Integrated Authentication and
explicit local certificate trust. Validation confirmed:

- Metadata discovery returned 24 eligible columns in catalog order.
- Schema generation produced 1,000 rows with the expected header, type/length,
  decimal-scale, nullability, GUID, and date checks.
- Standalone pipe-delimited generation produced 1,000 rows plus the header.
- SDV 2.2.0 trained on a 5,000-row read-only sample and produced 10,000 rows
  with the expected header.
- SDV-unavailable fallback and strict SDV failure behavior work as designed.

Run the automated unit suite from the repository root:

```powershell
python -m unittest -v tests/test_generate_delimited_file.py
```
