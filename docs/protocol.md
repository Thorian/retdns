# retdns protocol v1

## Destination and discovery

The service uses `RNS.Destination(identity, IN, SINGLE, "retdns", "server")`. This namespace is independent of `nomadnetwork.node`, `nomadpl.node` and LXMF. A service address is the resulting destination hash, not the identity hash. Underlying links, path requests and announcements use the [standard Reticulum APIs](https://reticulum.network/manual/reference.html).

Service announcements carry RNS MessagePack app data:

```text
{"version": 1, "name": "Community DNS"}
```

The name is a service display name (1–128 UTF-8 bytes without controls), not a registered record. `Discovery.aspect_filter` is `retdns.server`. It checks the announced identity/destination namespace, version, app-data size and display-name shape. Reticulum validates signed announcements before delivery to the handler. Discovery expires candidates locally; it does not establish trust in their records. A client explicitly selects a service address.

## Queries

Open an ordinary encrypted RNS Link to the selected `retdns.server` destination. Send a request to `/retdns/lookup` with one of these native MessagePack-compatible dictionaries:

```text
{"version": 1, "kind": "name", "value": "home.ret"}
{"version": 1, "kind": "address", "value": "0123456789abcdef0123456789abcdef"}
```

No extra JSON or base64 layer is used. Requests and responses are bounded at 65536 bytes, allowing batches of up to 100 maximum-length records. Version must be integer 1, not a boolean. Names are normalized using lowercase domain labels, Python's IDNA codec, optional single trailing-dot removal and a 253-byte ASCII limit. Addresses are 16-byte destination hashes encoded as 32 hex characters; byte values are also accepted by the Python normalization API. TTLs are integer seconds in the inclusive range 0–604800, with a default of 86400 (24 hours) for new registrations.

## Responses

```text
{
  "version": 1,
  "status": "ok",
  "record": {
    "name": "home.ret",
    "address": "0123456789abcdef0123456789abcdef",
    "ttl": 300
  }
}
```

Other statuses are `not_found`, `invalid_request` and `server_error`, without a record. Forward and reverse queries return the same record format. A resolver rejects a record that does not match the queried name/address, invalid record fields, unsupported versions and unexpected statuses. Only validated records from the explicitly selected authority enter its cache. There are no network registration/deletion operations.

## Bulk extension (package 0.2.0+)

Use the same endpoint with this request:

```text
{"version": 1, "kind": "bulk"}
```

The server responds with a list of its own choosing:

```text
{"version": 1, "status": "ok", "records": [
  {"name": "home.ret", "address": "0123456789abcdef0123456789abcdef", "ttl": 86400}
]}
```

An empty list is a successful response. The server may disable sharing or return fewer than its maximum, and clients cannot require specific names or ordering. The default policy chooses up to 25 active records ranked by combined successful forward/reverse network lookups in `(now - 86400, now]`, with ties ordered by canonical name. This is a rolling window, not a calendar-day counter. Misses, cache hits, administrative reads and bulk reads do not count. Events persist for up to seven days; the selected window is configurable within that retention limit. Custom server policies may select other current authoritative pairs. Lookup counts and query timestamps are not included in responses.

The batch must contain at most 100 records and satisfy one-to-one uniqueness across all names and addresses. The client validates the entire list before committing a single cache transaction. Each pair retains its own TTL, and all batch entries use the receipt/commit time as the cache start. Unrelated cache rows are retained. Servers keep protocol version 1 for this additive request kind; package 0.1.0 servers reject it as `invalid_request`. New clients must not assume older services support bulk requests.

## Uniqueness and cache behavior

The authoritative registry has unique constraints on **both** canonical name and canonical destination address. It stores one row per pair, not separate forward and reverse tables. Updates, explicit replacement and deletion are transactional, so both query directions always consult the same authoritative mapping. TTL specifies the client's cache lifetime; it does not expire an authoritative registration.

The cache is similarly one row per pair, additionally scoped by the selected server address. Each row has one expiry shared by both lookup directions. A new validated mapping invalidates cached conflicts on either side. TTL-zero records are returned but not retained. A refreshed `not_found` removes a matching cached row. No stale or negative caching is implemented. Other clients can retain a previous mapping until its previously issued TTL expires; there is no push invalidation.

## Client use and scope

`Resolver.resolve(name)` returns the application destination string. The calling application opens that destination using its own protocol/aspects; retdns does not change the destination or act as a content proxy. `Resolver.reverse(address)` returns the canonical name. A raw destination passed to `resolve()` is normalized directly; `lookup_address()` performs the reverse network/cache lookup.

This service is a Reticulum-native directory, not Internet DNS wire format, DNSSEC, UDP/53 or a global consensus registry. Distinct authorities may publish different mappings. Select the authority by its destination address and manage its registry under the desired naming policy. The transport verifies that the recalled server identity and `retdns.server` namespace derive the selected server address before linking; another application destination is not silently accepted as a DNS server.

## Storage interfaces

A registry backend supplies `lookup_name(name) -> Record | None`, `lookup_address(address) -> Record | None`, `record_lookup(record)` and `top_records(limit=..., window=...)` for default serving; local administration adds `register`, `remove` and `records`. A custom bulk policy may replace `top_records`. A cache backend supplies `lookup_name(server, name)`, `lookup_address(server, address)`, `put(server, record)`, atomic `put_many(server, records)` and `invalidate(server, name=... | address=...)`. Implement the one-to-one and shared-expiry semantics atomically when replacing SQLite. Resolver transports supply `query(dict) -> dict` and `close()`; this boundary also supports tests without networking.

## Overload behavior (0.2.1)

A server may reject excess application work with `{"version": 1, "status": "server_busy"}`. Clients treat this as a failed lookup and must not change cached records. The default SQLite registry retains at most 50,000 successful lookup events and seven days of history. Under load the count cap can shorten the popularity window. Anonymous requests can influence popularity.
