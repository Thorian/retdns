"""Versioned lookup messages and server-controlled bulk selection."""

from itertools import islice

from .models import Record

ASPECTS = ("retdns", "server")
LOOKUP_PATH = "/retdns/lookup"
VERSION = 1
WIRE_LIMIT = 65536
MAX_BULK_RECORDS = 100


def answer(registry, data, *, bulk_limit=25, bulk_window=86400, bulk_policy=None):
    """Answer a query and count successful individual network lookups."""
    try:
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != VERSION
        ):
            raise ValueError("Invalid protocol version")
        kind = data["kind"]
        if kind == "bulk":
            record = None
        elif kind == "name":
            record = registry.lookup_name(data["value"])
        elif kind == "address":
            record = registry.lookup_address(data["value"])
        else:
            raise ValueError("Invalid query kind")
    except (KeyError, TypeError, ValueError):
        return {"version": VERSION, "status": "invalid_request"}
    if kind == "bulk":
        return bulk_answer(registry, bulk_limit, bulk_window, bulk_policy)
    if record is None:
        return {"version": VERSION, "status": "not_found"}
    registry.record_lookup(record)
    return {"version": VERSION, "status": "ok", "record": record.to_wire()}


def bulk_answer(registry, limit, window, policy):
    """Return the server's selection using current authoritative records."""
    if type(limit) is not int or not 0 <= limit <= MAX_BULK_RECORDS:
        raise ValueError("Invalid server bulk limit")
    if limit == 0:
        selected = []
    elif policy is not None:
        selected = policy(registry)
    else:
        selected = registry.top_records(limit=limit, window=window)

    records = []
    names = set()
    addresses = set()
    for record in islice(selected, limit):
        if not isinstance(record, Record):
            raise ValueError("Bulk policy must return Records")
        authoritative = registry.lookup_name(record.name)
        if authoritative is None or authoritative.address != record.address:
            raise ValueError("Bulk policy selected an unregistered mapping")
        if record.name in names or record.address in addresses:
            raise ValueError("Bulk policy returned conflicting records")
        names.add(record.name)
        addresses.add(record.address)
        records.append(authoritative.to_wire())
    return {"version": VERSION, "status": "ok", "records": records}
