import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'split_delimited_file.py'
spec = importlib.util.spec_from_file_location('split_delimited_file', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class SplitDelimitedFileTests(unittest.TestCase):
    def _run_case(self, extension, delimiter):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / f'sample{extension}'
            output = root / 'out'
            rows = [
                ['id', 'description'],
                ['1', 'alpha'],
                ['2', 'contains,comma'],
                ['3', 'contains\nnewline'],
                ['4', 'delta'],
                ['5', 'echo'],
            ]
            with source.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, delimiter=delimiter, quotechar='"', lineterminator='\n')
                writer.writerows(rows)

            mod.split_delimited_file(source, output, 4, 'utf-8-sig', delimiter, '"')
            files = sorted(output.iterdir())
            self.assertEqual(len(files), 4)
            self.assertTrue(all(p.suffix == extension for p in files))
            self.assertEqual([p.name for p in files], [
                f'sample_part_01_of_04{extension}',
                f'sample_part_02_of_04{extension}',
                f'sample_part_03_of_04{extension}',
                f'sample_part_04_of_04{extension}',
            ])

            combined = []
            counts = []
            for p in files:
                with p.open('r', encoding='utf-8-sig', newline='') as f:
                    reader = csv.reader(f, delimiter=delimiter, quotechar='"')
                    self.assertEqual(next(reader), rows[0])
                    data = list(reader)
                    counts.append(len(data))
                    combined.extend(data)
            self.assertEqual(counts, [2, 1, 1, 1])
            self.assertEqual(combined, rows[1:])

    def test_csv_extension_preserved(self):
        self._run_case('.csv', ',')

    def test_txt_extension_preserved(self):
        self._run_case('.txt', '|')

if __name__ == '__main__':
    unittest.main()
