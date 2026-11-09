#!/usr/bin/env python3
"""Split a delimited text file into exactly N files with near-equal logical record counts.

Designed for large delimited text files: two streaming passes, no whole-file load into memory.
Quoted fields containing embedded newlines are handled by Python's csv module.
"""

import argparse
import csv
from pathlib import Path


def count_records(source: Path, encoding: str, delimiter: str, quotechar: str) -> int:
    with source.open("r", encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=delimiter, quotechar=quotechar)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def distribution(total: int, parts: int) -> list[int]:
    base, remainder = divmod(total, parts)
    return [base + (1 if i < remainder else 0) for i in range(parts)]


def split_delimited_file(source: Path, output_dir: Path, parts: int, encoding: str, delimiter: str, quotechar: str) -> None:
    if parts < 1:
        raise ValueError("--parts must be at least 1")
    if not source.is_file():
        raise FileNotFoundError(source)

    output_dir.mkdir(parents=True, exist_ok=True)
    total = count_records(source, encoding, delimiter, quotechar)
    counts = distribution(total, parts)
    width = max(2, len(str(parts)))
    stem = source.stem

    print(f"Source: {source}")
    print(f"Logical data records: {total:,}")
    print(f"Parts: {parts}")
    print(f"Planned counts: {counts}")

    written_total = 0
    with source.open("r", encoding=encoding, newline="") as src:
        reader = csv.reader(src, delimiter=delimiter, quotechar=quotechar)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError("Source delimited file is empty") from exc

        for idx, planned in enumerate(counts, start=1):
            target = output_dir / f"{stem}_part_{idx:0{width}d}_of_{parts:0{width}d}{source.suffix}"
            current = 0
            with target.open("w", encoding=encoding, newline="") as dst:
                writer = csv.writer(dst, delimiter=delimiter, quotechar=quotechar,
                                    quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
                writer.writerow(header)
                for _ in range(planned):
                    try:
                        row = next(reader)
                    except StopIteration as exc:
                        raise RuntimeError("Source ended earlier than the counting pass indicated") from exc
                    writer.writerow(row)
                    current += 1
                    written_total += 1
            print(f"{target.name}: {current:,} records")

        try:
            next(reader)
        except StopIteration:
            pass
        else:
            raise RuntimeError("Source contains more records than the counting pass indicated")

    if written_total != total:
        raise RuntimeError(f"Validation failed: counted {total:,}, wrote {written_total:,}")
    print(f"Validated: wrote {written_total:,} of {total:,} records across exactly {parts} files")


def main() -> None:
    parser = argparse.ArgumentParser(description="Split a large delimited text file into exactly N near-equal files.")
    parser.add_argument("source", type=Path, help="Source delimited text file path")
    parser.add_argument("--output-dir", type=Path, default=Path("split_output"))
    parser.add_argument("--parts", type=int, default=4)
    parser.add_argument("--encoding", default="utf-8-sig", help="Default handles UTF-8 with or without BOM")
    parser.add_argument("--delimiter", default=",")
    parser.add_argument("--quotechar", default='"')
    args = parser.parse_args()
    split_delimited_file(args.source, args.output_dir, args.parts, args.encoding, args.delimiter, args.quotechar)

if __name__ == "__main__":
    main()
