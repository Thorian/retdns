# retdns

An independently distributable Reticulum name service and client library. It provides **one name ↔ one destination address**, forward/reverse queries, service announcements and persistent local TTL caching.

Version 0.2.0 is an experimental, pre-1.0 release. APIs may change; see the changelog before upgrading.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development, [RELEASING.md](RELEASING.md) for GitHub publication, and [CHANGELOG.md](CHANGELOG.md) for version history.

The service uses its own **`retdns.server`** destination namespace, separate from NomadNet, NomadPL and LXMF. Names can refer to destinations belonging to any application. A client resolves the name with retdns, then connects using the destination's actual application protocol.

## Project layout

```text
retdns/
  pyproject.toml          Package metadata and command-line entry point
  src/retdns/
    models.py            Canonical names, addresses and record format
    storage.py           Authoritative registry and bidirectional TTL cache
    protocol.py          Versioned query/response protocol
    transport.py         Reticulum server, announcements and query transport
    discovery.py         Announced service discovery with expiry
    resolver.py          Client API shared by applications and command line
    cli.py               Server, registry administration and client commands
  examples/client.py     Forward/reverse resolution in an application
  docs/protocol.md       Wire format and interoperability rules
  tests/                 Unit and real-network integration tests
  dist/                  Built wheel and source archive
```

## Install

Python 3.11+ and Reticulum 1.6.0+. The same package installs server and client APIs. It does not depend on the sibling projects.

```sh
python -m pip install .
# Or install the separately distributed artifact:
python -m pip install dist/retdns-0.2.0-py3-none-any.whl
```

## Register names and start a server

Use the actual **destination hash**, not its identity hash. For a NomadPL site this is the address printed by NomadPL under its `nomadpl.node` namespace.

```sh
# Replace the sample address with a real application's destination.
retdns records add home.ret 0123456789abcdef0123456789abcdef
retdns records list
retdns serve --name "Community DNS"
```

The server prints its `retdns.server` destination address. It announces after three seconds and every 1800 seconds thereafter. `--announce-interval` changes the interval, with a minimum of 60 seconds. Clients use this **DNS server address** for queries; the record's address is the separate application destination returned by the query.

Defaults are `state/server.identity` and `state/records.sqlite3`, relative to the working directory. New records default to a **24-hour TTL (86400 seconds)** in the API and CLI. Override it with `--ttl SECONDS`; existing records retain their assigned TTL until updated. Override identity/database paths using `serve --identity PATH --database PATH`. Registry administration also accepts a database path: `retdns records --database PATH add NAME ADDRESS --ttl 86400`. Admin changes are visible to a running service without restart. Run one server process for an identity. Back up its identity and database, and protect the state directory with OS permissions.

The service uses the normal Reticulum instance and configured interfaces. To select another configuration directory, put `--rns-config PATH` before the subcommand. Configure an inbound `TCPServerInterface` in Reticulum if needed; retdns does not overwrite network configuration or open an Internet UDP/53 DNS listener.

## One-to-one rules

- Names are case-insensitive and stored as canonical lowercase domain-style names. One final dot is accepted. Unicode names use Python's IDNA codec; use the returned ASCII spelling as canonical.
- Addresses are canonical lowercase, 32-character destination hashes. The Python API also accepts 16-byte destination values.
- A name cannot point to two addresses; an address cannot have two names. There are no aliases or multiple-address records.
- Both database uniqueness constraints and transactions enforce these rules, including concurrent registrations.
- Registering the same pair updates its TTL. Conflicting pairs are rejected by default.

An explicit replacement removes any record conflicting with **either** side before inserting the new pair:

```sh
retdns records add home.ret fedcba9876543210fedcba9876543210 --replace
retdns records remove home.ret
```

The one-to-one rule applies to each authoritative registry. Independent servers may maintain different records; this version does not implement federation, delegation or globally unique name registration. Configure clients with the server whose registry they should use. Existing cached records can remain valid until their original TTL expires after a server-side update; `--refresh` bypasses that cache.

## Discover and query

```sh
retdns discover --seconds 15
retdns lookup home.ret --server DNS_SERVER_DESTINATION
retdns reverse APPLICATION_DESTINATION --server DNS_SERVER_DESTINATION
retdns lookup home.ret --server DNS_SERVER_DESTINATION --refresh
```

Replace uppercase placeholders with actual hashes. Discovery returns service names and addresses from `retdns.server` announcements, not application records. Display names need not be unique. Select/pin the service by destination address; discovery never automatically trusts or selects the first server it sees. Known server addresses can be queried using Reticulum path requests even if a live announcement was missed.

Queries return JSON records containing `name`, `address` and `ttl`. Both directions return the same canonical pair. A missing record exits with an error. Administrative registration/deletion is local only; anonymous remote requests cannot change records.

## TTL and local caching

`state/cache.sqlite3` stores client records persistently. `--cache PATH` changes that path. A successful forward **or reverse** query fills both directions as one database row with one expiry time. Reads do not extend expiry. The cache survives application restarts, removes both directions when a record expires, and never returns expired records as an offline fallback. Names and reverse entries from different DNS servers are isolated.

TTLs are whole seconds from 0 to 604800 (seven days). TTL zero returns a record without caching it and invalidates an older cached pair for that record. TTL controls **client cache validity**, not authoritative record lifetime: registry records remain until changed or removed. Cached responses report the remaining whole seconds, rounded down. A final fraction of a second may therefore report TTL zero. The default cache holds at most 4096 records and evicts least recently used entries. Expired entries are removed during reads/writes; entries from before a backward wall-clock jump are invalidated conservatively.

Negative results are not cached. Service discovery has a separate in-memory expiry (3600 seconds by default) and a 256-service bound. Receiving an announcement does not add or change name records.

## Bulk requests and popular records

```sh
retdns bulk --server DNS_SERVER_DESTINATION
# Server settings: default selection is the top 25 over the past 24 hours.
retdns serve --bulk-limit 25 --bulk-window 86400
# Disable bulk sharing:
retdns serve --bulk-limit 0
```

Clients can request a server-selected batch with `Resolver.bulk()`. The default policy returns up to **25 currently registered pairs with the most successful network lookups in the rolling past 24 hours**. Forward and reverse queries count toward the same pair. Ties sort by canonical name. Cache hits, local administrative reads, misses and bulk requests do not increase popularity. If fewer than 25 pairs have been queried, fewer are returned; a new server with no query history returns an empty list.

Successful network-query events persist across restarts. They retain only the canonical pair and timestamp, not client identities. Events older than seven days are pruned during lookups/bulk selection; the default ranking considers only the last 24 hours. Removed or explicitly replaced pairs lose their history. Traffic volume determines the statistics table size within that retention period.

The server controls selection: `--bulk-limit` accepts 0–100 and `--bulk-window` accepts 1–604800 seconds. Library hosts can pass `Server(..., bulk_policy=lambda registry: registry.records())` to choose their own ordering or curated subset instead of popularity. The count limit still applies, and selected pairs must be current authoritative mappings. Clients cannot demand a complete registry or override the policy.

Each returned pair carries its own record TTL, normally 24 hours. The client validates the whole response for valid fields and one-to-one mappings **before** atomically caching the batch in both directions. Invalid batches do not partially populate the cache. A bulk response does not delete unrelated cached records; it only replaces conflicting mappings. TTL zero still means no caching. The popularity window and record TTL are independent settings.

```python
with Resolver("DNS_SERVER_DESTINATION", Cache("state/client-cache.sqlite3")) as dns:
    records = dns.bulk()
    for record in records:
        print(record.name, record.address, record.ttl)
    # Names in the batch are now immediately usable via dns.resolve()/reverse().
```

## Use the same package in a client

```python
import RNS
from retdns import Cache, Resolver, NameNotFound

RNS.Reticulum()
cache = Cache("state/client-cache.sqlite3")

with Resolver("DNS_SERVER_DESTINATION", cache, timeout=30) as dns:
    record = dns.lookup_name("home.ret")  # Record(name, address, ttl)
    address = dns.resolve("home.ret")  # Canonical destination string
    name = dns.reverse(address)  # "home.ret", usually a cache hit
    reverse_record = dns.lookup_address(address)
    fresh = dns.lookup_name("home.ret", refresh=True)
```

`resolve()` also accepts a raw destination hash and returns it unchanged after validation. Use `lookup_name()` explicitly if a registered name itself consists of exactly 32 hex characters. Missing names/addresses raise `NameNotFound`; network failures raise `ConnectionError` or `TimeoutError`. A `Resolver` reuses and serializes its encrypted link; close it or use a context manager. Initialize Reticulum once in the host application before making network queries.

For example, pass the result directly to the separately installed NomadPL client:

```python
from nomadpl import Client, Request

# Within the Resolver context from above:
web = Client(dns.resolve("home.ret"))
try:
    response = web.request(Request("GET", "/about/?lang=en"))
finally:
    web.close()
```

That is application-level integration: retdns does not patch the OS resolver, intercept browser DNS, or automatically modify other packages' URL parsers. See `examples/client.py` for a runnable client. The registry/cache boundaries are ordinary Python interfaces; alternative backends can implement the same methods without changing the transport.

## Test and distribute

```sh
python -m unittest discover -s tests -v
# Enable the actual server/client Reticulum integration test:
# PowerShell: $env:RETDNS_NETWORK_TEST='1'
# POSIX: export RETDNS_NETWORK_TEST=1
python -m unittest discover -s tests -v
python -m pip install build
python -m build
```

The network test verifies service discovery, distinct namespaces, both query directions, persistent caching, TTL expiry/requery, bulk prefetch, missing records, and a connection to a real application using a name-resolved destination. Other tests cover conflicting/concurrent registrations, replacement, ranking/rolling-window boundaries, custom bulk policies, atomic batch validation, cache remapping, malformed responses and announcements, and cache bounds.

Distribute the wheel or source archive in `dist/`. Nothing is automatically published to a registry. retdns code is MIT-licensed; Reticulum is a separately installed dependency with its own license. The [protocol specification](docs/protocol.md) documents the wire format.
