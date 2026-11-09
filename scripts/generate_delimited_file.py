#!/usr/bin/env python3
"""Generate a delimited load-test file with optional database-derived headers."""

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_HEADERS = ["record_id", "payload", "created_at_utc"]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def load_headers_from_database(connection_string: str, header_query: str) -> list[str]:
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError(
            "Database header retrieval requires pyodbc. Install dependencies with "
            "'python -m pip install -r requirements.txt'."
        ) from exc

    connection = pyodbc.connect(connection_string)
    try:
        cursor = connection.cursor()
        cursor.execute(header_query)
        if cursor.description is None:
            raise ValueError("--header-query must return a result set")
        headers = [column[0] for column in cursor.description]
    finally:
        connection.close()

    if not headers:
        raise ValueError("--header-query returned no columns")
    return headers


def validate_connection_string(connection_string: str) -> None:
    options = {}
    for segment in connection_string.split(";"):
        if "=" in segment:
            key, value = segment.split("=", 1)
            options[key.strip().lower()] = value.strip().lower()

    credential_keys = {"uid", "user id", "user", "pwd", "password"}
    if credential_keys.intersection(options):
        raise ValueError(
            "--connection-string must use the executing user's integrated authentication, "
            "not a user name or password"
        )

    trusted_connection = options.get("trusted_connection") or options.get("integrated security")
    authentication = options.get("authentication", "").replace(" ", "")
    supported_authentication = {
        "activedirectorydefault",
        "activedirectoryintegrated",
        "activedirectoryinteractive",
    }
    if trusted_connection not in {"yes", "true", "sspi"} and authentication not in supported_authentication:
        raise ValueError(
            "--connection-string must use Trusted_Connection=Yes, Integrated Security=SSPI, "
            "or a supported Microsoft Entra Authentication value"
        )


def value_for_column(column: str, record_id: int, created_at_utc: str) -> str:
    normalized = "".join(character for character in column.lower() if character.isalnum())
    if normalized in {"recordid", "id"}:
        return str(record_id)
    if normalized in {"payload", "data", "value"}:
        return f"payload-{record_id:010d}"
    if normalized in {"createdatutc", "createdat", "timestamp"}:
        return created_at_utc
    return f"{column}_{record_id:010d}"


def generate_delimited_file(
    output_file: Path,
    rows: int,
    encoding: str,
    delimiter: str,
    quotechar: str,
    headers: list[str],
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    created_at_utc = datetime.now(timezone.utc).isoformat()

    with output_file.open("w", encoding=encoding, newline="") as destination:
        writer = csv.writer(
            destination,
            delimiter=delimiter,
            quotechar=quotechar,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        writer.writerow(headers)
        for record_id in range(1, rows + 1):
            writer.writerow(
                [value_for_column(column, record_id, created_at_utc) for column in headers]
            )

    print(f"Output: {output_file}")
    print(f"Headers: {len(headers):,}")
    print(f"Generated data records: {rows:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a delimited load-test file with optional database-derived headers."
    )
    parser.add_argument("output_file", type=Path, help="Generated file path")
    parser.add_argument("--rows", type=positive_int, required=True, help="Number of data records to generate")
    parser.add_argument("--encoding", default="utf-8-sig", help="Default handles UTF-8 with or without BOM")
    parser.add_argument("--delimiter", default=",")
    parser.add_argument("--quotechar", default='"')
    parser.add_argument(
        "--connection-string",
        help="ODBC connection string that uses the executing user's authentication",
    )
    parser.add_argument(
        "--header-query",
        help="Query whose result-set column names become the generated file header",
    )
    args = parser.parse_args()

    if bool(args.connection_string) != bool(args.header_query):
        parser.error("--connection-string and --header-query must be supplied together")

    if args.header_query:
        try:
            validate_connection_string(args.connection_string)
            headers = load_headers_from_database(args.connection_string, args.header_query)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        headers = DEFAULT_HEADERS
    generate_delimited_file(
        args.output_file,
        args.rows,
        args.encoding,
        args.delimiter,
        args.quotechar,
        headers,
    )


if __name__ == "__main__":
    main()
