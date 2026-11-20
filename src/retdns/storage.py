"""Persistent one-to-one registry and per-authority bidirectional TTL cache."""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .models import ConflictError, Record, normalize_address, normalize_name


class Database:
    def __init__(self, path):
        """Prepare a file-backed database path for short-lived connections."""
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("Use a file path; connections are opened per transaction")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self, *, write=False):
        """Open a transaction and always close its connection afterward."""
        db = sqlite3.connect(self.path, timeout=30)
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def add_announce_column(self, db, table):
        """Upgrade an existing registry or cache without replacing its records."""
        if table not in ("records", "cache"):
            raise ValueError("Unknown record table")
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "announce" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN announce BLOB")


class Registry(Database):
    """Authoritative records persist until explicitly changed or removed.

    TTL controls client caching, not record lifetime. Unique constraints make
    both directions one-to-one across threads and processes.
    """

    def __init__(self, path, *, clock=time.time, max_history=50000):
        """Create the authoritative record and lookup-history tables if absent."""
        super().__init__(path)
        if type(max_history) is not int or max_history <= 0:
            raise ValueError("max_history must be a positive integer")
        self.clock, self.max_history = clock, max_history
        with self.connect(write=True) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    name TEXT PRIMARY KEY,
                    address TEXT NOT NULL UNIQUE,
                    ttl INTEGER NOT NULL CHECK(ttl BETWEEN 0 AND 604800)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS lookup_events (
                    name TEXT NOT NULL,
                    address TEXT NOT NULL,
                    queried_at REAL NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS lookup_time ON lookup_events(queried_at)"
            )
            self.add_announce_column(db, "records")
            self._trim_history(db)

    def _trim_history(self, db):
        """Keep a bounded span of event row IDs, including databases from older versions."""
        db.execute(
            "DELETE FROM lookup_events WHERE rowid <= (SELECT MAX(rowid) FROM lookup_events) - ?",
            (self.max_history,),
        )

    def register(self, name, address, ttl=86400, *, replace=False, announce=None):
        """Store a unique pair, rejecting conflicts unless replacement is explicit."""
        record = Record(name, address, ttl, announce)
        with self.connect(write=True) as db:
            conflicts = db.execute(
                "SELECT name,address,announce FROM records WHERE name=? OR address=?",
                (record.name, record.address),
            ).fetchall()
            if not replace and any(
                pair[:2] != (record.name, record.address) for pair in conflicts
            ):
                raise ConflictError(
                    "Name or address already belongs to another record; use explicit replacement"
                )
            if record.announce is None:
                existing = next(
                    (
                        pair[2]
                        for pair in conflicts
                        if pair[:2] == (record.name, record.address)
                    ),
                    None,
                )
                record = Record(record.name, record.address, record.ttl, existing)
            if replace:
                db.execute(
                    "DELETE FROM lookup_events WHERE name=? OR address=?",
                    (record.name, record.address),
                )
                db.execute(
                    "DELETE FROM records WHERE name=? OR address=?",
                    (record.name, record.address),
                )
            db.execute(
                """
                INSERT INTO records (name, address, ttl, announce) VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET ttl = excluded.ttl, announce = excluded.announce
                """,
                (record.name, record.address, record.ttl, record.announce),
            )
        return record

    def store_announce(self, address, raw):
        """Update an existing registration only after verifying its signed announcement."""
        from .announces import announce_time, read_announce

        address = normalize_address(address)
        read_announce(raw, address)
        with self.connect(write=True) as db:
            row = db.execute(
                "SELECT announce FROM records WHERE address=?", (address,)
            ).fetchone()
            if row is None:
                return False
            if row[0] is not None and announce_time(row[0]) > announce_time(raw):
                return False
            db.execute("UPDATE records SET announce=? WHERE address=?", (raw, address))
        return True

    def lookup_name(self, name):
        """Read the authoritative pair for a normalized name."""
        with self.connect() as db:
            row = db.execute(
                "SELECT name,address,ttl,announce FROM records WHERE name=?",
                (normalize_name(name),),
            ).fetchone()
        return Record(*row) if row else None

    def lookup_address(self, address):
        """Read the authoritative pair for a normalized destination."""
        with self.connect() as db:
            row = db.execute(
                "SELECT name,address,ttl,announce FROM records WHERE address=?",
                (normalize_address(address),),
            ).fetchone()
        return Record(*row) if row else None

    def remove(self, name):
        """Delete a name and its popularity history in one transaction."""
        with self.connect(write=True) as db:
            db.execute(
                "DELETE FROM lookup_events WHERE name=?", (normalize_name(name),)
            )
            return (
                db.execute(
                    "DELETE FROM records WHERE name=?", (normalize_name(name),)
                ).rowcount
                > 0
            )

    def record_lookup(self, record):
        """Count successful network queries only; do not call for cache/admin reads."""
        now = self.clock()
        with self.connect(write=True) as db:
            db.execute(
                "DELETE FROM lookup_events WHERE queried_at<=? OR queried_at>?",
                (now - 604800, now),
            )
            db.execute(
                """
                INSERT INTO lookup_events (name, address, queried_at)
                SELECT ?, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM records WHERE name = ? AND address = ?
                )
                """,
                (record.name, record.address, now, record.name, record.address),
            )
            self._trim_history(db)

    def top_records(self, *, limit=25, window=86400):
        """Rank current pairs by successful queries within a rolling time window."""
        if type(limit) is not int or not 0 <= limit <= 100:
            raise ValueError("Bulk limit must be between 0 and 100")
        if type(window) is not int or not 1 <= window <= 604800:
            raise ValueError("Popularity window must be 1–604800 seconds")
        now = self.clock()
        with self.connect(write=True) as db:
            db.execute(
                "DELETE FROM lookup_events WHERE queried_at<=? OR queried_at>?",
                (now - 604800, now),
            )
            rows = db.execute(
                """
                SELECT records.name, records.address, records.ttl, records.announce
                FROM records
                JOIN lookup_events AS events
                  ON records.name = events.name
                 AND records.address = events.address
                WHERE events.queried_at > ? AND events.queried_at <= ?
                GROUP BY records.name, records.address, records.ttl
                ORDER BY COUNT(*) DESC, records.name ASC
                LIMIT ?
                """,
                (now - window, now, limit),
            ).fetchall()
        return [Record(*row) for row in rows]

    def records(self):
        """List authoritative pairs in canonical name order for local administration."""
        with self.connect() as db:
            return [
                Record(*row)
                for row in db.execute(
                    "SELECT name,address,ttl,announce FROM records ORDER BY name"
                )
            ]


class Cache(Database):
    def __init__(self, path, *, max_records=4096, clock=time.time):
        """Create a bounded, persistent cache whose entries are scoped by authority."""
        super().__init__(path)
        if type(max_records) is not int or max_records <= 0:
            raise ValueError("max_records must be positive")
        self.max_records, self.clock = max_records, clock
        with self.connect(write=True) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    server TEXT NOT NULL,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL,
                    cached_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    PRIMARY KEY(server, name),
                    UNIQUE(server, address)
                )
                """
            )
            self.add_announce_column(db, "cache")
            db.execute("CREATE INDEX IF NOT EXISTS cache_expiry ON cache(expires_at)")

    def put(self, server, record):
        """Cache one pair through the same atomic path used for bulk responses."""
        self.put_many(server, [record])

    def put_many(self, server, records):
        """Validate an entire batch, then update both directions atomically."""
        server = normalize_address(server)
        records = list(records)
        if any(not isinstance(record, Record) for record in records):
            raise TypeError("Expected Records")
        names = {record.name for record in records}
        addresses = {record.address for record in records}
        if len(names) != len(records) or len(addresses) != len(records):
            raise ValueError("Bulk records must be one-to-one")
        now = self.clock()
        with self.connect(write=True) as db:
            db.execute(
                "DELETE FROM cache WHERE expires_at<=? OR cached_at>?", (now, now)
            )
            for record in records:
                db.execute(
                    "DELETE FROM cache WHERE server=? AND (name=? OR address=?)",
                    (server, record.name, record.address),
                )
            for record in records:
                if record.ttl:
                    db.execute(
                        "INSERT INTO cache (server,name,address,cached_at,expires_at,accessed_at,announce) VALUES (?,?,?,?,?,?,?)",
                        (
                            server,
                            record.name,
                            record.address,
                            now,
                            now + record.ttl,
                            now,
                            record.announce,
                        ),
                    )
            db.execute(
                """
                DELETE FROM cache WHERE rowid IN (
                    SELECT rowid FROM cache
                    ORDER BY accessed_at DESC, rowid DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.max_records,),
            )

    def _get(self, server, column, value):
        """Return an unexpired pair and update recency without extending its TTL."""
        now = self.clock()
        with self.connect(write=True) as db:
            db.execute(
                "DELETE FROM cache WHERE expires_at<=? OR cached_at>?", (now, now)
            )
            # column is a literal supplied only by the two methods below.
            row = db.execute(
                f"SELECT name,address,expires_at,announce FROM cache WHERE server=? AND {column}=?",
                (normalize_address(server), value),
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE cache SET accessed_at=? WHERE server=? AND name=?",
                    (now, normalize_address(server), row[0]),
                )
        return (
            Record(row[0], row[1], max(0, int(row[2] - now)), row[3]) if row else None
        )

    def lookup_name(self, server, name):
        """Read a live forward entry from the selected authority's cache."""
        return self._get(server, "name", normalize_name(name))

    def lookup_address(self, server, address):
        """Read the reverse direction of the same cached pair."""
        return self._get(server, "address", normalize_address(address))

    def invalidate(self, server, *, name=None, address=None):
        """Remove both cache directions for exactly one name or address."""
        if (name is None) == (address is None):
            raise ValueError("Specify exactly one of name or address")
        column, value = (
            ("name", normalize_name(name))
            if name is not None
            else ("address", normalize_address(address))
        )
        with self.connect(write=True) as db:
            db.execute(
                f"DELETE FROM cache WHERE server=? AND {column}=?",
                (normalize_address(server), value),
            )

    def clear(self, server=None):
        """Clear cached pairs for one authority, or for every authority when omitted."""
        with self.connect(write=True) as db:
            if server is None:
                db.execute("DELETE FROM cache")
            else:
                db.execute(
                    "DELETE FROM cache WHERE server=?", (normalize_address(server),)
                )
