from .models import NameNotFound, Record, normalize_address, normalize_name
from .protocol import MAX_BULK_RECORDS, VERSION
from .transport import Transport


class Resolver:
    """One selected authority, shared forward/reverse TTL cache."""

    def __init__(
        self, server, cache, *, timeout=30, transport=None, require_announce=True
    ):
        """Bind a cache and query transport to one explicitly selected authority."""
        if type(require_announce) is not bool:
            raise ValueError("require_announce must be a boolean")
        self.server = normalize_address(server)
        self.cache = cache
        self.require_announce = require_announce
        self.transport = (
            transport
            if transport is not None
            else Transport(self.server, timeout=timeout)
        )

    def _lookup(self, kind, value, refresh):
        """Use a live cache entry or validate and cache the authority's reply."""
        value = normalize_name(value) if kind == "name" else normalize_address(value)
        cached = (
            self.cache.lookup_name(self.server, value)
            if kind == "name"
            else self.cache.lookup_address(self.server, value)
        )
        if (
            not refresh
            and cached is not None
            and (cached.announce is not None or not self.require_announce)
        ):
            self._import(cached)
            return cached
        response = self.transport.query(
            {
                "version": VERSION,
                "kind": kind,
                "value": value,
                "require_announce": self.require_announce,
            }
        )
        try:
            if (
                not isinstance(response, dict)
                or type(response.get("version")) is not int
                or response["version"] != VERSION
            ):
                raise ValueError("Invalid response version")
            if response["status"] == "not_found":
                self.cache.invalidate(self.server, **{kind: value})
                raise NameNotFound(f"No {kind} record for {value}")
            if response["status"] == "announce_unavailable":
                raise ConnectionError(
                    "The DNS server has not collected this destination's signed announce yet"
                )
            if response["status"] != "ok":
                raise ValueError("Server rejected lookup")
            record = Record.from_wire(response["record"])
            if self.require_announce and record.announce is None:
                raise ValueError("Authority did not supply a signed announce")
            if (record.name if kind == "name" else record.address) != value:
                raise ValueError("Response does not match the query")
        except (ValueError, TypeError, KeyError) as exc:
            raise ConnectionError("Invalid retdns response") from exc
        self._import(record)
        self.cache.put(self.server, record)
        return record

    def _import(self, record):
        """Restore signed destination metadata on both network replies and cache hits."""
        if record.announce is not None:
            from .announces import import_announce

            import_announce(record.announce, record.address)

    def lookup_name(self, name, *, refresh=False):
        """Look up a canonical pair by name, optionally bypassing the cache."""
        return self._lookup("name", name, refresh)

    def lookup_address(self, address, *, refresh=False):
        """Look up the same canonical pair by its destination address."""
        return self._lookup("address", address, refresh)

    def resolve(self, name_or_address):
        """Return a destination string suitable for another Reticulum client."""
        try:
            return normalize_address(name_or_address)
        except ValueError:
            return self.lookup_name(name_or_address).address

    def reverse(self, address):
        """Return the single canonical name assigned to a destination."""
        return self.lookup_address(address).name

    def bulk(self):
        """Fetch the server's selection and atomically cache its valid pairs."""
        response = self.transport.query(
            {
                "version": VERSION,
                "kind": "bulk",
                "require_announce": self.require_announce,
            }
        )
        try:
            if (
                not isinstance(response, dict)
                or type(response.get("version")) is not int
                or response["version"] != VERSION
                or response.get("status") != "ok"
            ):
                raise ValueError("Invalid bulk response")
            items = response["records"]
            if not isinstance(items, list) or len(items) > MAX_BULK_RECORDS:
                raise ValueError("Invalid bulk record count")
            records = [Record.from_wire(item) for item in items]
            if self.require_announce and any(
                record.announce is None for record in records
            ):
                raise ValueError("Authority omitted a signed announce")
            if len({record.name for record in records}) != len(records) or len(
                {record.address for record in records}
            ) != len(records):
                raise ValueError("Bulk response is not one-to-one")
        except (ValueError, TypeError, KeyError) as exc:
            raise ConnectionError("Invalid retdns bulk response") from exc
        for record in records:
            self._import(record)
        self.cache.put_many(self.server, records)
        return records

    def close(self):
        """Release the resolver's reusable network transport."""
        self.transport.close()

    def __enter__(self):
        """Return this resolver for use in a managed client session."""
        return self

    def __exit__(self, *args):
        """Close the transport when the managed session ends."""
        self.close()
