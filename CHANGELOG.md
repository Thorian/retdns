# Changelog

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
