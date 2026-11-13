# Security

retdns is pre-1.0 software and has not had an independent security audit. Discovery finds candidates; it does not decide which server an application should trust. Pin the intended authority by destination address.

Remote requests cannot register or delete authoritative records. Registry files, lookup history and cached records are local SQLite files, protected by the host's file permissions. Service identity files contain private keys and must not be shared or committed.

For a suspected vulnerability, use the repository's private vulnerability reporting feature when available. Do not post private keys, production lookup history or exploit details in a public issue. Use temporary identities in ordinary bug reports and reproductions.

Only the current development version receives fixes. Protocol compatibility changes will be documented in the changelog.
