"""Signed announce storage, validation and cold-client identity recovery."""

import base64
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import RNS
import RNS.vendor.umsgpack as msgpack
from RNS.Stamps import cost_to_bytes, stamp_valid, stamp_workblock

from retdns import Cache, Record, Registry, Resolver
from retdns.announces import AnnounceCollector, import_announce, read_announce
from retdns.protocol import VERSION, WIRE_LIMIT, answer

SERVER = "e" * 32


def make_announce(
    *,
    identity=None,
    app_data=b"Node metadata",
    ratchet=False,
    stamped=False,
    emitted=1000,
):
    """Build a real signed packet with optional ratchet and low-cost stamp metadata."""
    identity = identity or RNS.Identity()
    with (
        patch.object(RNS.Transport, "register_destination"),
        patch.object(RNS.Transport, "STAMP_DESTINATIONS", False),
    ):
        destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE, "retdns_test", "site"
        )
    public = identity.get_public_key()
    ratchet_public = (
        RNS.Identity._ratchet_public_bytes(RNS.Identity._generate_ratchet())
        if ratchet
        else b""
    )
    if stamped:
        workblock = stamp_workblock(
            destination.hash + public + destination.name_hash, expand_rounds=0
        )
        stamp = RNS.Identity.get_random_hash()[: RNS.Identity.STAMPLENGTH]
        while not stamp_valid(stamp, 1, workblock):
            stamp = RNS.Identity.get_random_hash()[: RNS.Identity.STAMPLENGTH]
        cost = cost_to_bytes(4.5)
        app_data += (
            bytes([RNS.Identity.EXTPAYLOADMARKER, 0])
            + stamp
            + bytes([cost[1], cost[0]])
        )
    prefix = (
        public
        + destination.name_hash
        + b"nonce"
        + emitted.to_bytes(5, "big")
        + ratchet_public
    )
    signature = identity.sign(destination.hash + prefix + app_data)
    packet = RNS.Packet(
        destination,
        prefix + signature + app_data,
        RNS.Packet.ANNOUNCE,
        context_flag=RNS.Packet.FLAG_SET if ratchet else RNS.Packet.FLAG_UNSET,
    )
    packet.pack()
    return destination.hash.hex(), packet.raw, identity, ratchet_public


class AnnounceTests(unittest.TestCase):
    def setUp(self):
        """Give each test separate databases and process-local Reticulum identity stores."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = Registry(self.root / "registry.sqlite")
        self.cache = Cache(self.root / "cache.sqlite")
        for owner, name, value in (
            (RNS.Identity, "known_destinations", {}),
            (RNS.Identity, "known_ratchets", {}),
            (RNS.Identity, "announce_valid_cache", {}),
            (RNS.Identity, "announce_invalid_cache", {}),
            (RNS.Identity, "known_destinations_store", None),
            (
                RNS.Transport,
                "owner",
                SimpleNamespace(is_connected_to_shared_instance=True),
            ),
        ):
            context = patch.object(owner, name, value, create=True)
            context.start()
            self.addCleanup(context.stop)

    def test_signature_and_destination_binding(self):
        """Reject modified packets and keys signed for a different destination."""
        address, raw, _, _ = make_announce()
        self.assertEqual(read_announce(raw, address).destination_hash.hex(), address)
        with self.assertRaises(ValueError):
            Record("home.ret", "a" * 32, 30, raw)
        with self.assertRaises(ValueError):
            Record("home.ret", address, 30, raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaises(ValueError):
            Record("home.ret", address, 30, raw[:30])

    def test_registry_restart_ttl_edit_and_address_replacement(self):
        """Keep complete announce bytes through restart without attaching them to a new address."""
        address, raw, _, _ = make_announce()
        self.registry.register("home.ret", address, announce=raw)
        reopened = Registry(self.root / "registry.sqlite")
        self.assertEqual(reopened.lookup_address(address).announce, raw)
        self.assertEqual(reopened.register("home.ret", address, ttl=10).announce, raw)
        other, other_raw, _, _ = make_announce()
        reopened.register("home.ret", other, replace=True)
        self.assertIsNone(reopened.lookup_name("home.ret").announce)
        self.assertFalse(reopened.store_announce(address, raw))
        self.assertTrue(reopened.store_announce(other, other_raw))

    def test_legacy_databases_are_upgraded_without_data_loss(self):
        """Add announce storage in place while preserving old names and cache expiry."""
        registry_path, cache_path = (
            self.root / "old.sqlite",
            self.root / "old-cache.sqlite",
        )
        with closing(sqlite3.connect(registry_path)) as db, db:
            db.execute(
                "CREATE TABLE records(name TEXT PRIMARY KEY,address TEXT UNIQUE NOT NULL,ttl INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT INTO records VALUES(?,?,?)", ("old.ret", "a" * 32, 86400)
            )
        registry = Registry(registry_path)
        self.assertIsNone(registry.lookup_name("old.ret").announce)
        with closing(sqlite3.connect(cache_path)) as db, db:
            db.execute(
                "CREATE TABLE cache(server TEXT,name TEXT,address TEXT,cached_at REAL,expires_at REAL,accessed_at REAL,PRIMARY KEY(server,name),UNIQUE(server,address))"
            )
            db.execute(
                "INSERT INTO cache VALUES(?,?,?,?,?,?)",
                (SERVER, "old.ret", "a" * 32, 100, 200, 100),
            )
        cache = Cache(cache_path, clock=lambda: 150)
        self.assertEqual(cache.lookup_name(SERVER, "old.ret").ttl, 50)

    def test_cold_lookup_cache_hit_and_bulk_restore_identity(self):
        """Restore public keys from DNS even when no application announce reached the client."""
        address, raw, identity, _ = make_announce()
        self.registry.register("home.ret", address, announce=raw)
        registry = self.registry

        class LocalTransport:
            calls = 0

            def query(self, payload):
                """Answer locally so no broadcast can accidentally populate the identity cache."""
                self.calls += 1
                return answer(registry, payload)

            def close(self):
                """Satisfy the transport lifecycle without owning a network link."""
                pass

        transport = LocalTransport()
        resolver = Resolver(SERVER, self.cache, transport=transport)
        with patch.object(RNS.Transport, "inbound") as inbound:
            self.assertEqual(resolver.resolve("home.ret"), address)
            recalled = RNS.Identity.recall(bytes.fromhex(address), _no_use=True)
            self.assertEqual(recalled.get_public_key(), identity.get_public_key())
            self.assertEqual(recalled.app_data, b"Node metadata")
            RNS.Identity.known_destinations.clear()
            reopened = Resolver(
                SERVER, Cache(self.root / "cache.sqlite"), transport=transport
            )
            self.assertEqual(reopened.reverse(address), "home.ret")
            self.assertEqual(transport.calls, 1)
            self.assertIsNotNone(
                RNS.Identity.recall(bytes.fromhex(address), _no_use=True)
            )
            self.cache.clear()
            RNS.Identity.known_destinations.clear()
            self.assertEqual(resolver.bulk()[0].announce, raw)
            self.assertIsNotNone(
                RNS.Identity.recall(bytes.fromhex(address), _no_use=True)
            )
            inbound.assert_not_called()

    def test_stamps_ratchets_and_advertised_link_cost_are_preserved(self):
        """Let Reticulum verify a real stamp and restore its ratchet and link requirement."""
        address, raw, _, ratchet = make_announce(ratchet=True, stamped=True)
        record = self.registry.register("stamped.ret", address, announce=raw)
        self.assertEqual(record.announce, raw)
        with (
            patch.object(RNS.Transport, "DESTINATION_COST", 1),
            patch.object(RNS.Transport, "STAMPS_ENFORCED", True),
        ):
            import_announce(record.announce, address)
        self.assertEqual(RNS.Identity.known_ratchets[bytes.fromhex(address)], ratchet)
        self.assertEqual(RNS.Identity.recall_link_cost(bytes.fromhex(address)), 4.5)
        self.assertIsNotNone(RNS.Identity.known_destinations[bytes.fromhex(address)][5])
        self.assertEqual(
            RNS.Identity.recall_app_data(bytes.fromhex(address), _no_use=True),
            b"Node metadata",
        )

    def test_missing_announce_is_not_a_successful_modern_resolution(self):
        """Do not silently return an address that still lacks the destination's keys."""
        self.registry.register("pending.ret", "a" * 32)
        response = answer(
            self.registry,
            {
                "version": VERSION,
                "kind": "name",
                "value": "pending.ret",
                "require_announce": True,
            },
        )
        self.assertEqual(response["status"], "announce_unavailable")
        self.assertEqual(
            answer(
                self.registry,
                {"version": VERSION, "kind": "bulk", "require_announce": True},
            )["records"],
            [],
        )

    def test_legacy_reply_requires_explicit_client_compatibility(self):
        """Reject address-only replies even when an older server ignores the request flag."""

        class LegacyTransport:
            def query(self, payload):
                """Return the format used before signed announcements were carried."""
                return {
                    "version": 1,
                    "status": "ok",
                    "record": {"name": "old.ret", "address": "a" * 32, "ttl": 86400},
                }

            def close(self):
                """Own no resources in this fixture."""
                pass

        with Resolver(SERVER, self.cache, transport=LegacyTransport()) as resolver:
            with self.assertRaises(ConnectionError):
                resolver.resolve("old.ret")
        self.assertIsNone(self.cache.lookup_name(SERVER, "old.ret"))
        with Resolver(
            SERVER, self.cache, transport=LegacyTransport(), require_announce=False
        ) as resolver:
            self.assertEqual(resolver.resolve("old.ret"), "a" * 32)

    def test_local_stamp_policy_can_reject_a_valid_signature(self):
        """Do not bypass Reticulum's required proof of work for a signed destination."""
        address, raw, _, _ = make_announce()
        with patch.object(RNS.Transport, "STAMPS_ENFORCED", True):
            with self.assertRaises(ConnectionError):
                import_announce(raw, address)
        self.assertNotIn(bytes.fromhex(address), RNS.Identity.known_destinations)

    def test_collector_updates_only_registered_destinations_and_refuses_older_data(
        self,
    ):
        """Capture the cached signed packet and avoid reverting to an older announcement."""
        address, raw, identity, _ = make_announce(emitted=2000)
        self.registry.register("home.ret", address)
        collector = AnnounceCollector(self.registry)
        with patch.object(
            RNS.Transport, "get_cached_packet", return_value=SimpleNamespace(raw=raw)
        ):
            collector.received_announce(
                bytes.fromhex(address), identity, b"Node metadata", b"packet"
            )
        self.assertEqual(self.registry.lookup_name("home.ret").announce, raw)
        _, old, _, _ = make_announce(identity=identity, emitted=1000)
        self.assertFalse(self.registry.store_announce(address, old))

    def test_bulk_response_respects_wire_size_with_full_announces(self):
        """Fit complete signed packets within the existing response limit."""
        records = []
        for index in range(100):
            address, raw, _, _ = make_announce(app_data=b"x" * 250)
            name = (
                f"{index:03}"
                + "a" * 60
                + "."
                + ".".join(["b" * 63, "c" * 63, "d" * 61])
            )
            records.append(self.registry.register(name, address, announce=raw))
        response = answer(
            self.registry,
            {"version": VERSION, "kind": "bulk", "require_announce": True},
            bulk_limit=100,
            bulk_policy=lambda registry: records,
        )
        self.assertLessEqual(len(msgpack.packb(response)), WIRE_LIMIT)
        self.assertGreater(len(response["records"]), 25)
        self.assertLess(len(response["records"]), 100)

    def test_cli_json_is_an_explicit_lossless_base64_encoding(self):
        """Keep binary packet data readable and recoverable in command-line output."""
        address, raw, _, _ = make_announce()
        result = Record("home.ret", address, 86400, raw).to_json()
        self.assertEqual(result["announce_encoding"], "base64")
        self.assertEqual(base64.b64decode(result["announce"]), raw)
