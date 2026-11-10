#!/usr/bin/env python3
"""Generate delimited synthetic data using one of three engines.

Engine priority in ``auto`` mode:
1. SDV, when a database sample is available and SDV is installed.
2. Schema-aware generation, when database metadata is available.
3. Standalone generation, when a JSON schema/config is supplied.

SDV is optional. A missing package or SDV runtime failure can fall back to the
schema-aware engine unless ``--engine sdv`` or ``--no-fallback`` is specified.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import math
import random
import re
import sys
import struct
import uuid
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

LOG = logging.getLogger("generate_delimited_file")


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    sql_type: str
    nullable: bool = True
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    is_identity: bool = False
    is_computed: bool = False
    has_default: bool = False
    is_primary_key: bool = False

    @property
    def normalized_type(self) -> str:
        return self.sql_type.lower().strip()


@dataclass(frozen=True)
class GenerationResult:
    engine_requested: str
    engine_used: str
    row_count: int
    output_file: str
    fallback_reason: str | None = None


def is_package_available(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


def verify_sdv_installation() -> tuple[bool, str]:
    if not is_package_available("sdv"):
        return False, "SDV is not installed. Install it with: python -m pip install sdv"
    try:
        import sdv  # type: ignore
        version = getattr(sdv, "__version__", "unknown")
        from sdv.single_table import GaussianCopulaSynthesizer  # noqa: F401
        try:
            from sdv.metadata import SingleTableMetadata  # noqa: F401
        except ImportError:
            from sdv.metadata import Metadata  # noqa: F401
        return True, f"SDV is installed and importable (version {version})."
    except Exception as exc:  # pragma: no cover - depends on local package state
        return False, f"SDV is installed but failed verification: {exc}"


def quote_identifier(name: str) -> str:
    return "[" + name.replace("]", "]]" ) + "]"


def build_connection_string(server: str, database: str, auth: str,
                            driver: str = "ODBC Driver 18 for SQL Server",
                            encrypt: bool = True,
                            trust_server_certificate: bool = False) -> str:
    """Build a credential-free ODBC connection string from runtime settings."""
    if not server or not database:
        raise ValueError("server and database are required")
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        f"DATABASE={database}",
        f"Encrypt={'yes' if encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if trust_server_certificate else 'no'}",
    ]
    if auth == "windows":
        parts.append("Trusted_Connection=yes")
    elif auth == "entra-interactive":
        parts.append("Authentication=ActiveDirectoryInteractive")
    elif auth == "entra-default":
        # A DefaultAzureCredential token is supplied through attrs_before.
        pass
    else:
        raise ValueError(f"Unsupported authentication mode: {auth}")
    return ";".join(parts) + ";"


def _entra_access_token() -> bytes:
    """Acquire an Azure SQL token from the current user's Azure credential chain."""
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Entra default authentication requires azure-identity. "
            "Install it with: python -m pip install azure-identity"
        ) from exc
    credential = DefaultAzureCredential(
        exclude_environment_credential=True,
        exclude_managed_identity_credential=True,
    )
    token = credential.get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def connect_sql_server(server: str, database: str, auth: str,
                       driver: str = "ODBC Driver 18 for SQL Server",
                       encrypt: bool = True,
                       trust_server_certificate: bool = False):
    """Connect using the identity of the user/process running the script."""
    try:
        import pyodbc  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Database mode requires pyodbc. Install it with: python -m pip install pyodbc"
        ) from exc
    connection_string = build_connection_string(
        server, database, auth, driver, encrypt, trust_server_certificate
    )
    if auth == "entra-default":
        sql_copt_ss_access_token = 1256
        return pyodbc.connect(
            connection_string,
            attrs_before={sql_copt_ss_access_token: _entra_access_token()},
        )
    return pyodbc.connect(connection_string)


def read_table_metadata(connection, schema: str, table: str) -> list[ColumnSpec]:
    """Read authoritative SQL Server column metadata without passing column names."""
    sql = """
    SELECT
        c.column_id,
        c.name AS column_name,
        t.name AS sql_type,
        c.max_length,
        c.[precision],
        c.scale,
        c.is_nullable,
        c.is_identity,
        c.is_computed,
        CASE WHEN dc.object_id IS NULL THEN 0 ELSE 1 END AS has_default,
        CASE WHEN pk.column_id IS NULL THEN 0 ELSE 1 END AS is_primary_key
    FROM sys.tables AS tb
    JOIN sys.schemas AS s ON s.schema_id = tb.schema_id
    JOIN sys.columns AS c ON c.object_id = tb.object_id
    JOIN sys.types AS t ON t.user_type_id = c.user_type_id
    LEFT JOIN sys.default_constraints AS dc ON dc.object_id = c.default_object_id
    LEFT JOIN
    (
        SELECT ic.object_id, ic.column_id
        FROM sys.indexes AS i
        JOIN sys.index_columns AS ic
          ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        WHERE i.is_primary_key = 1
    ) AS pk ON pk.object_id = c.object_id AND pk.column_id = c.column_id
    WHERE s.name = ? AND tb.name = ?
    ORDER BY c.column_id;
    """
    rows = connection.cursor().execute(sql, schema, table).fetchall()
    if not rows:
        raise ValueError(f"Table not found or has no columns: {schema}.{table}")
    specs: list[ColumnSpec] = []
    for r in rows:
        max_len = int(r.max_length) if r.max_length is not None else None
        if str(r.sql_type).lower() in {"nvarchar", "nchar"} and max_len and max_len > 0:
            max_len //= 2
        specs.append(ColumnSpec(
            name=str(r.column_name), sql_type=str(r.sql_type), nullable=bool(r.is_nullable),
            max_length=max_len, precision=int(r.precision), scale=int(r.scale),
            is_identity=bool(r.is_identity), is_computed=bool(r.is_computed),
            has_default=bool(r.has_default), is_primary_key=bool(r.is_primary_key),
        ))
    return specs


def included_columns(columns: Sequence[ColumnSpec], include_server_generated: bool = False) -> list[ColumnSpec]:
    if include_server_generated:
        return list(columns)
    excluded = {"timestamp", "rowversion"}
    return [c for c in columns if not c.is_computed and c.normalized_type not in excluded]


def read_table_sample(connection, schema: str, table: str, columns: Sequence[ColumnSpec], sample_rows: int):
    try:
        import pandas as pd  # type: ignore
    except ImportError as exc:
        raise RuntimeError("SDV database mode requires pandas. Install it with: python -m pip install pandas") from exc
    if sample_rows < 1:
        raise ValueError("sample_rows must be at least 1")
    names = ", ".join(quote_identifier(c.name) for c in columns)
    query = f"SELECT TOP ({int(sample_rows)}) {names} FROM {quote_identifier(schema)}.{quote_identifier(table)};"
    return pd.read_sql_query(query, connection)


def semantic_value(column: ColumnSpec, row_number: int, rng: random.Random) -> Any:
    """Generate a SQL-type-valid fallback value with lightweight name semantics."""
    if column.nullable and rng.random() < 0.02:
        return None
    t, n = column.normalized_type, column.name.lower()
    if column.is_identity or column.is_primary_key:
        return row_number
    if t in {"bit"}:
        return rng.choice([0, 1])
    if t in {"tinyint"}: return rng.randint(0, 255)
    if t in {"smallint"}: return rng.randint(-32768, 32767)
    if t in {"int"}: return rng.randint(1, 2_000_000_000)
    if t in {"bigint"}: return rng.randint(1, 9_000_000_000_000)
    if t in {"decimal", "numeric", "money", "smallmoney", "float", "real"}:
        scale = column.scale if column.scale is not None else 2
        max_whole = 10 ** max(1, min((column.precision or 12) - scale, 8)) - 1
        return f"{rng.uniform(0, max_whole):.{scale}f}"
    if t in {"date"}: return (date(2020, 1, 1) + timedelta(days=rng.randint(0, 2500))).isoformat()
    if t in {"datetime", "datetime2", "smalldatetime", "datetimeoffset"}:
        return (datetime(2020, 1, 1) + timedelta(seconds=rng.randint(0, 200_000_000))).isoformat(sep=" ")
    if t in {"time"}:
        seconds = rng.randint(0, 86399); return f"{seconds//3600:02d}:{(seconds%3600)//60:02d}:{seconds%60:02d}"
    if t == "uniqueidentifier": return str(uuid.uuid4())
    if t in {"binary", "varbinary", "image"}: return uuid.uuid4().hex[: max(2, min((column.max_length or 16) * 2, 64))]
    if "email" in n: value = f"user{row_number}@example.test"
    elif "phone" in n: value = f"555-{rng.randint(100,999)}-{rng.randint(1000,9999)}"
    elif n.endswith("id"): value = str(rng.randint(1, max(1000, row_number + 1)))
    elif "name" in n: value = f"{column.name}_{row_number}"
    elif "address" in n: value = f"{rng.randint(1,9999)} Example Street"
    else: value = f"{column.name}_{row_number}_{rng.randint(1000,9999)}"
    limit = column.max_length if column.max_length and column.max_length > 0 else 100
    return value[:limit]


def generate_schema_rows(columns: Sequence[ColumnSpec], row_count: int, seed: int) -> Iterable[dict[str, Any]]:
    rng = random.Random(seed)
    for row_number in range(1, row_count + 1):
        yield {c.name: semantic_value(c, row_number, rng) for c in columns}


def synthesize_with_sdv(sample_df, row_count: int, seed: int):
    """Fit a single-table SDV synthesizer and return synthetic rows."""
    from sdv.single_table import GaussianCopulaSynthesizer  # type: ignore
    try:
        from sdv.metadata import SingleTableMetadata  # type: ignore
    except ImportError:
        from sdv.metadata import Metadata  # type: ignore
        metadata = Metadata.detect_from_dataframes({"data": sample_df})
        synthesizer = GaussianCopulaSynthesizer(metadata)
        if hasattr(synthesizer, "set_random_state"):
            synthesizer.set_random_state(seed)
        synthesizer.fit({"data": sample_df})
        return synthesizer.sample(num_rows=row_count)["data"]
    else:
        metadata = SingleTableMetadata()
        metadata.detect_from_dataframe(data=sample_df)
        synthesizer = GaussianCopulaSynthesizer(metadata)
        if hasattr(synthesizer, "set_random_state"):
            synthesizer.set_random_state(seed)
        synthesizer.fit(sample_df)
        return synthesizer.sample(num_rows=row_count)


def write_delimited(
    output: Path,
    columns: Sequence[str],
    rows: Iterable[dict[str, Any]],
    delimiter: str,
    quotechar: str,
    encoding: str,
    include_header: bool,
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), delimiter=delimiter, quotechar=quotechar,
                                quoting=csv.QUOTE_MINIMAL, lineterminator="\n", extrasaction="ignore")
        if include_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def load_schema_config(path: Path) -> list[ColumnSpec]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload["columns"] if isinstance(payload, dict) else payload
    return [ColumnSpec(**item) for item in items]


def generate(args: argparse.Namespace) -> GenerationResult:
    requested = args.engine
    columns: list[ColumnSpec]
    connection = None
    database_mode = bool(args.server or args.database)
    fallback_reason = None

    if bool(args.server) != bool(args.database):
        raise ValueError("Database mode requires both --server and --database")
    if requested == "sdv" and not database_mode:
        raise ValueError("--engine sdv requires database arguments")

    if database_mode:
        if not args.schema or not args.table:
            raise ValueError("Database mode requires --schema and --table")
        connection = connect_sql_server(
            args.server, args.database, args.auth, args.driver,
            not args.no_encrypt, args.trust_server_certificate
        )
        columns = included_columns(read_table_metadata(connection, args.schema, args.table), args.include_server_generated)
    elif args.config:
        columns = included_columns(load_schema_config(args.config), args.include_server_generated)
    else:
        raise ValueError("Provide database arguments or --config for standalone mode")

    engine_used = "schema"
    rows: Iterable[dict[str, Any]]
    wants_sdv = requested in {"auto", "sdv"} and database_mode
    if wants_sdv:
        ok, verification = verify_sdv_installation()
        if ok:
            try:
                sample = read_table_sample(connection, args.schema, args.table, columns, args.sample_rows)
                synthetic = synthesize_with_sdv(sample, args.rows, args.seed)
                rows = synthetic.to_dict(orient="records")
                engine_used = "sdv"
            except Exception as exc:
                if requested == "sdv" or args.no_fallback:
                    raise
                fallback_reason = f"SDV failed: {exc}"
                LOG.warning("%s. Falling back to schema generation.", fallback_reason)
                rows = generate_schema_rows(columns, args.rows, args.seed)
        else:
            if requested == "sdv" or args.no_fallback:
                raise RuntimeError(verification)
            fallback_reason = verification
            LOG.warning("%s Falling back to schema generation.", verification)
            rows = generate_schema_rows(columns, args.rows, args.seed)
    else:
        rows = generate_schema_rows(columns, args.rows, args.seed)
        engine_used = "standalone" if not database_mode else "schema"

    written = write_delimited(args.output, [c.name for c in columns], rows, args.delimiter, args.quotechar,
                              args.encoding, not args.no_header)
    if connection is not None:
        connection.close()
    result = GenerationResult(requested, engine_used, written, str(args.output), fallback_reason)
    LOG.info("Generation engine used: %s", engine_used)
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate synthetic delimited data from SQL metadata or a standalone schema.")
    p.add_argument("--server", help="SQL Server or SQL MI host name")
    p.add_argument("--database", help="Database name")
    p.add_argument("--auth", choices=["windows", "entra-default", "entra-interactive"], default="windows",
                   help="Credential-free user-context authentication mode")
    p.add_argument("--driver", default="ODBC Driver 18 for SQL Server")
    p.add_argument("--no-encrypt", action="store_true", help="Disable encryption (not recommended for MI)")
    p.add_argument("--trust-server-certificate", action="store_true",
                   help="Trust the presented server certificate; use only for controlled development")
    p.add_argument("--schema", help="SQL schema name")
    p.add_argument("--table", help="SQL table name")
    p.add_argument("--config", type=Path, help="Standalone JSON schema file")
    p.add_argument("--engine", choices=["auto", "sdv", "schema"], default="auto")
    p.add_argument("--no-fallback", action="store_true", help="Fail instead of falling back when SDV is unavailable/fails")
    p.add_argument("--sample-rows", type=int, default=10_000)
    p.add_argument("--rows", type=int, help="Number of synthetic data rows to generate")
    p.add_argument("--output", type=Path, help="Generated delimited file path")
    p.add_argument("--delimiter", default=",")
    p.add_argument("--quotechar", default='"')
    p.add_argument("--encoding", default="utf-8")
    p.add_argument("--no-header", action="store_true")
    p.add_argument("--include-server-generated", action="store_true")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--verify-sdv", action="store_true")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return p


def main() -> int:
    parser = build_parser(); args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    if args.verify_sdv:
        ok, message = verify_sdv_installation(); print(message); return 0 if ok else 1
    if args.rows is None:
        parser.error("--rows is required unless --verify-sdv is used")
    if args.output is None:
        parser.error("--output is required unless --verify-sdv is used")
    if args.rows < 1: parser.error("--rows must be at least 1")
    if len(args.delimiter) != 1: parser.error("--delimiter must be exactly one character")
    if len(args.quotechar) != 1: parser.error("--quotechar must be exactly one character")
    try:
        result = generate(args)
    except Exception as exc:
        LOG.error("Generation failed: %s", exc); return 2
    print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
