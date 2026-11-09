# Split Delimited File Tests

`test_split_delimited_file.py` verifies that
`scripts\split_delimited_file.py` produces complete, balanced output files for
Azure Data Factory load-test preparation. The tests use temporary directories,
so they do not modify repository files or require a source data file.

## Requirements

- Python 3.9 or later
- No third-party packages

## Run the tests

Run this test module from the repository root:

```powershell
python -m unittest tests/test_split_delimited_file.py -v
```

Or discover every test in this directory:

```powershell
python -m unittest discover -s tests -v
```

## What the tests verify

| Test | Verification |
| --- | --- |
| Headered CSV | Creates exactly four `.csv` files, repeats the header in each, preserves records with commas and embedded newlines, and distributes five records as 2/1/1/1. |
| Headered pipe-delimited text | Applies the same checks to `.txt` input using `|` as the delimiter and preserves the extension. |
| Headerless pipe-delimited text | Invokes the command-line script with `--no-header`, confirms all five records are retained, and confirms the 2/1/1/1 distribution without adding a header. |

Each test recombines the generated data records and compares them with the
original source rows. This confirms that every source record is written exactly
once.
