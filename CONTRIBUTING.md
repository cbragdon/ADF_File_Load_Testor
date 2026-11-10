# Contributing

Thank you for improving the ADF Performance Toolkit. The repository exists to
make load-test results repeatable and understandable, so changes should make
the tools safer, clearer, and easier to validate.

## Before changing code

- Keep generated files in `output\`; do not commit generated data, local
  virtual environments, credentials, or database connection details.
- Preserve credential-free database access. The generator must use the
  executing user's Windows or Microsoft Entra identity rather than accepting
  passwords, tokens, or secrets.
- Make focused changes. Do not mix unrelated refactoring with a bug fix.

## Validation

Run the relevant checks from the repository root before opening a pull request:

```powershell
python -m unittest discover -s tests -v
```

For T-SQL changes, parse the affected scripts with the T-SQL dialect:

```powershell
python -m sqlfluff parse --dialect tsql .\sql\monitoring\<script>.sql
```

When modifying database-aware generation, use a read-only source table and
record the authentication mode, database engine/version, driver version, row
count, and validation outcome in the pull request. Never use production data
or grant source-database write permission merely to run a test.

## Documentation

Update the relevant Markdown guide whenever behavior, arguments, dependencies,
authentication, output format, or test coverage changes. Write instructions
for operators: explain why a step matters, what it changes, and how to verify
the result.

## Pull requests

Describe the purpose of the change, validation performed, and any remaining
risks. Keep generated output and machine-specific files out of the pull
request.
