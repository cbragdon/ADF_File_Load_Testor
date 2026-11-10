# `generate_delimited_file.py` Refactoring Plan

## Goal

Add SDV as an optional database-sample-driven generation engine without removing schema-based or standalone generation.

## Engine selection

1. **Database + `--engine auto` + SDV available:** read SQL metadata, sample rows, fit SDV, generate data.
2. **Database + SDV unavailable/fails:** warn and fall back to schema-aware generation.
3. **Database + `--engine sdv`:** require SDV; fail rather than silently change engines.
4. **Database + `--engine schema`:** use metadata only; do not read source rows.
5. **No database + `--config`:** preserve standalone generation.

The command prints a JSON result containing `engine_requested`, `engine_used`, output path, row count, and fallback reason.

## Refactoring phases

### Phase 1 - implemented scaffold

- `ColumnSpec` and `GenerationResult` models.
- SQL Server metadata discovery from `sys.tables`, `sys.schemas`, `sys.columns`, `sys.types`, defaults, identities, computed columns, and primary keys.
- Automatic column discovery. No column-name argument is required.
- Optional SDV import and verification.
- SDV `GaussianCopulaSynthesizer` adapter using a database sample, compatible
  with both the legacy `SingleTableMetadata` API and SDV 2.x `Metadata` API.
- Schema-aware fallback generator.
- Standalone JSON schema mode.
- Streaming delimited writer for schema/standalone generation.
- Clear engine reporting and strict `--no-fallback` behavior.

### Phase 2

- Refactor existing script-specific features into reusable functions rather than deleting them.
- Add foreign-key discovery and multi-table generation.
- Add configurable omission of identity/default columns for load-vs-file scenarios.
- Add chunked SDV sampling/writing strategy for very large requested outputs.
- Add SDV model save/load so repeated large file generation does not retrain every run.
- Add data-quality evaluation between source sample and synthetic output.

## Optional SDV installation

Create and activate a virtual environment, then install the optional dependency group:

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-sdv.txt
```

Linux/macOS activation:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-sdv.txt
```

If database access is not needed, `pyodbc` can be omitted. SQL Server/MI database mode also requires a compatible Microsoft ODBC Driver for SQL Server installed on the machine.

## Verification

```bash
python generate_delimited_file.py --verify-sdv
python -c "import sdv; print(sdv.__version__)"
```

Expected success message:

```text
SDV is installed and importable (version ...).
```

The generation command prints the engine actually used:

```json
{
  "engine_requested": "auto",
  "engine_used": "sdv",
  "fallback_reason": null
}
```

## Usage examples

Database mode with automatic SDV preference:

```bash
python scripts/generate_delimited_file.py \
  --server "localhost\\SQL2022" --database AdventureWorks \
  --schema Sales --table SalesOrderDetailEnlarged --auth windows \
  --engine auto --sample-rows 10000 --rows 1200000 \
  --output output/SalesOrderDetailEnlarged.csv
```

Force metadata-only generation:

```bash
python scripts/generate_delimited_file.py \
  --server "localhost\\SQL2022" --database AdventureWorks \
  --schema Sales --table SalesOrderDetailEnlarged --auth windows \
  --engine schema --rows 1200000 --output output/SalesOrderDetailEnlarged.csv
```

Standalone mode:

```bash
python scripts/generate_delimited_file.py \
  --config config/generate_delimited_file.example.json \
  --engine schema --rows 10000 --delimiter "|" --output output/sample.txt
```

## Test plan

| Test | Setup | Expected result |
|---|---|---|
| Standalone CSV | JSON schema, comma delimiter | Correct columns, header, row count, `.csv` output. |
| Standalone TXT | JSON schema, pipe delimiter | Correct delimiter, header, row count, `.txt` output. |
| SDV absent + auto | Mock database metadata; SDV unavailable | Schema engine used; warning and fallback reason returned. |
| SDV absent + forced | `--engine sdv` | Clear failure with install command; no partial output. |
| SDV runtime failure + auto | Mock synthesizer exception | Schema fallback used and reason recorded. |
| SDV runtime failure + no fallback | `--no-fallback` | Failure propagated; no silent engine change. |
| SDV success | Small DataFrame and mocked/installed SDV | `engine_used = sdv`; requested row count and column order retained. |
| Metadata discovery | Mock SQL catalog result | Names, types, lengths, precision/scale, nullability, identity/computed/default/PK mapped correctly. |
| Server-generated columns | Computed and rowversion columns | Excluded by default; included only when explicitly requested. |
| Deterministic fallback | Same schema and seed | Same standalone/schema output. |
| Invalid delimiter | More than one character | CLI rejects input. |
| Row validation | `--rows 0` | CLI rejects input. |

## Initial limitations

- The first implementation is single-table. Foreign-key-consistent multi-table generation is a later phase.
- SDV trains in memory from the selected sample and samples the requested output in memory. Large production-size generation needs chunking/model persistence before it should be used for 100+ GiB output.
- SQL type correctness does not automatically imply business-rule correctness. Check constraints and domain rules need a later metadata layer.

## User-context database connections

The generator does not accept a username, password, or full connection string. It builds the ODBC connection dynamically from `--server`, `--database`, `--auth`, and driver/security switches.

### Windows integrated authentication

Use this for a local or domain SQL Server that accepts the Windows identity running Python:

```powershell
python scripts/generate_delimited_file.py `
  --server "localhost\\SQL2022" --database AdventureWorks `
  --schema Sales --table SalesOrderDetail `
  --auth windows --engine auto --rows 100000 `
  --output output/SalesOrderDetail.csv
```

The generated ODBC settings include `Trusted_Connection=yes`. No SQL credentials are accepted or stored.

### Microsoft Entra user context for SQL MI

Preferred non-interactive developer option after signing in with Azure CLI, Visual Studio Code, or another credential supported by `DefaultAzureCredential`:

```powershell
az login
python scripts/generate_delimited_file.py `
  --server "my-mi.public.database.windows.net,3342" --database AdventureWorks `
  --schema Sales --table SalesOrderDetail `
  --auth entra-default --engine auto --rows 100000 `
  --output output/SalesOrderDetail.csv
```

`entra-default` obtains an Azure SQL access token for `https://database.windows.net/.default` from the current user credential chain and passes it to ODBC. The script intentionally excludes environment-secret and managed-identity credentials so local execution remains user-context based.

Interactive Entra authentication is also available:

```powershell
python scripts/generate_delimited_file.py `
  --server "my-mi.public.database.windows.net,3342" --database AdventureWorks `
  --schema Sales --table SalesOrderDetail `
  --auth entra-interactive --engine auto --rows 100000 `
  --output output/SalesOrderDetail.csv
```

This uses ODBC `Authentication=ActiveDirectoryInteractive` and may open an interactive sign-in prompt.

### Prerequisites and security

- Install Microsoft ODBC Driver 18 for SQL Server.
- The Windows or Entra identity running the script must have `CONNECT` plus permission to read catalog metadata and the sampled table.
- SQL MI networking must permit the client path, such as a private connection/VPN or a deliberately restricted public endpoint.
- Encryption defaults to enabled and certificate trust defaults to disabled.
- Do not use `--trust-server-certificate` for normal SQL MI production connections.
- The script never accepts passwords, client secrets, or tokens as CLI values.
