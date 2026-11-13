# Contributing

Use Python 3.11 or newer. Create a virtual environment, activate it, then install the project and development tools:

```sh
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format .
python -m unittest discover -s tests -v
```

Set `RETDNS_NETWORK_TEST=1` to include real service discovery and resolution over a loopback Reticulum link. In PowerShell use `$env:RETDNS_NETWORK_TEST = "1"`; in a POSIX shell use `export RETDNS_NETWORK_TEST=1`. The test creates temporary server and client instances, without using your personal identities or configuration.

Use descriptive names and ordinary control flow. Give every function and method a concise docstring explaining its purpose or contract, including private helpers and test fixtures. Additional comments should explain constraints or decisions, especially around authority selection, transactions and TTL expiry. Avoid comments that repeat the code. Write SQL across several lines when joins, constraints or conditions need room to read.

Preserve one-to-one mappings and atomic cache updates. A lookup in either direction must return the same pair, and cache reads must not extend its lifetime. A new test should cover changes to these rules. Record protocol changes in `docs/protocol.md` and `CHANGELOG.md`.

Do not commit private keys, database files, caches, local settings or generated distributions. Reticulum remains a separately installed dependency with its own license.

Before a pull request, run the CI checks and describe the behavior that changed and how you tested it. See [RELEASING.md](RELEASING.md) before building a release.
