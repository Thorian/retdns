# Changelog

## 0.3.0

- Store and return the complete signed destination announce in forward, reverse and bulk lookups.
- Verify public keys, destination binding and signature before accepting announce data. Let Reticulum enforce its stamp policy when importing it.
- Restore destination keys, application data, ratchets, destination stamps and advertised link cost on network replies and cache hits.
- Collect announcements for registered destinations and request their paths periodically from the command-line server.
- Upgrade existing registry and cache databases in place. Older address-only entries remain pending until an announce is collected or imported.
- Require signed announces in the client by default, with an explicit address-only compatibility option.
- Bound bulk replies by encoded size as well as record count, and encode binary announces as base64 in CLI JSON.

## 0.2.1

Bound incoming lookup work with concurrency and rate limits. Cap persistent lookup history, including existing databases.

## 0.2.0

- Add server-selected bulk responses, defaulting to the 25 most-looked-up pairs in the past 24 hours.
- Track successful forward and reverse network queries without counting cache, admin or bulk reads.
- Validate and cache complete batches atomically.
- Set new registrations to a 24-hour TTL by default.
- Add formatting checks, real-network tests and standalone release instructions.

## 0.1.0 — local prototype

- Introduce the `retdns.server` destination and service discovery.
- Support forward and reverse queries with one-to-one records.
- Persist authoritative records and per-authority TTL caches in SQLite.
