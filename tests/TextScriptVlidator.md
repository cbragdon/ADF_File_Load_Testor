The tests verify:
.csv input produces four .csv outputs.
.txt input produces four .txt outputs.
CSV uses comma delimiters.
TXT test uses pipe delimiters.
Headers appear in every split file.
Logical records with embedded newlines survive correctly.
Rows are distributed [2, 1, 1, 1] for the five-record test.
Recombining the four outputs exactly reproduces the original five data records.

python -m unittest tests/test_split_delimited_file.py