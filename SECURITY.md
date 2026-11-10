# Security Policy

## Supported version

Security fixes are applied to the current `main` branch.

## Reporting a vulnerability

Do not post credentials, connection strings, access tokens, customer data, or
an exploitable vulnerability report in a public issue.

Use the repository's **Security** tab to submit a private vulnerability report
when private reporting is available. If it is not available, contact the
repository owner through their GitHub profile and include:

- A concise description of the issue and affected file or command.
- Steps to reproduce it without sensitive data.
- The potential impact.
- Any suggested mitigation.

Allow time for acknowledgment and remediation before public disclosure.

## Handling data and credentials

This repository must not contain:

- User names, passwords, client secrets, raw Microsoft Entra access tokens, or
  credential-bearing connection strings.
- Production extracts, customer data, or generated files derived from them.
- Local `.env` files, virtual environments, or machine-specific settings.

Database tools must authenticate as the person or process executing them and
must use read-only source access for metadata and sample validation. Keep
encryption enabled and certificate trust disabled unless a controlled local
test explicitly requires `--trust-server-certificate`.

## Dependency updates

Review dependency changes for security impact and use the smallest dependency
set needed for the requested capability. Optional SDV dependencies belong in
`requirements-sdv.txt`; base database support remains in `requirements.txt`.
