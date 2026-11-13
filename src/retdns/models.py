"""Canonical record format shared by authorities, clients and caches."""

import re
from dataclasses import dataclass

MAX_TTL = 604800


def normalize_name(value):
    """Return a lowercase IDNA name after checking domain-label boundaries."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("Name must be a non-empty domain-style name")
    # A single final dot is accepted as the fully-qualified spelling.
    value = value[:-1] if value.endswith(".") else value
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Invalid name encoding") from exc
    if len(value) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
        for part in value.split(".")
    ):
        raise ValueError(
            "Name must contain valid domain labels, at most 253 ASCII bytes"
        )
    return value


def normalize_address(value):
    """Normalize a 16-byte destination or a 32-character hexadecimal address."""
    if isinstance(value, bytes):
        value = value.hex()
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", value):
        raise ValueError(
            "Address must be a 16-byte Reticulum destination or 32 hex characters"
        )
    return value.lower()


def checked_ttl(value):
    """Require a whole-second TTL within the supported cache lifetime."""
    if type(value) is not int or not 0 <= value <= MAX_TTL:
        raise ValueError(f"TTL must be an integer between 0 and {MAX_TTL} seconds")
    return value


@dataclass(frozen=True)
class Record:
    name: str
    address: str
    ttl: int

    def __post_init__(self):
        """Canonicalize both sides of the mapping and validate its TTL."""
        object.__setattr__(self, "name", normalize_name(self.name))
        object.__setattr__(self, "address", normalize_address(self.address))
        checked_ttl(self.ttl)

    def to_wire(self):
        """Expose the canonical name, address and TTL as a protocol record."""
        return {"name": self.name, "address": self.address, "ttl": self.ttl}

    @classmethod
    def from_wire(cls, data):
        """Construct a validated record from a decoded peer response."""
        if not isinstance(data, dict):
            raise ValueError("Invalid record")
        return cls(data["name"], data["address"], data["ttl"])


class ConflictError(ValueError):
    """A name or address is already assigned to a different record."""


class NameNotFound(LookupError):
    """The selected authority has no matching record."""
