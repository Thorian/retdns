import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from retdns import Cache, ConflictError, NameNotFound, Record, Registry, Resolver
from retdns.models import normalize_address, normalize_name
from retdns.protocol import answer

A, B, C, S, T = (value * 32 for value in "abcde")


class Fixture(unittest.TestCase):
    def setUp(self):
        """Create isolated temporary state for this test's assertions."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = Registry(self.root / "registry.sqlite")
        self.now = 1000.0
        self.cache = Cache(self.root / "cache.sqlite", clock=lambda: self.now)


class StorageTests(Fixture):
    def test_one_to_one_and_canonical_forward_reverse(self):
        """Enforce one canonical name and address in both lookup directions."""
        record = self.registry.register("Home.RET.", A, 60)
        self.assertEqual(record.name, "home.ret")
        self.assertEqual(self.registry.lookup_name("HOME.ret"), record)
        self.assertEqual(self.registry.lookup_address(bytes.fromhex(A)), record)
        with self.assertRaises(ConflictError):
            self.registry.register("other.ret", A)
        with self.assertRaises(ConflictError):
            self.registry.register("home.ret", B)
        self.assertEqual(len(self.registry.records()), 1)

    def test_replace_removes_both_conflicting_records(self):
        """Remove conflicts on either side before inserting an explicit replacement."""
        self.registry.register("home.ret", A)
        self.registry.register("other.ret", B)
        self.registry.register("home.ret", B, replace=True)
        self.assertIsNone(self.registry.lookup_address(A))
        self.assertIsNone(self.registry.lookup_name("other.ret"))
        self.assertEqual(len(self.registry.records()), 1)

    def test_registry_persistence_update_ttl_remove(self):
        """Persist TTL edits and delete both directions of a removed registration."""
        self.registry.register("home.ret", A, 300)
        reopened = Registry(self.root / "registry.sqlite")
        reopened.register("home.ret", A, 10)
        self.assertEqual(reopened.lookup_address(A).ttl, 10)
        self.assertTrue(reopened.remove("HOME.ret."))
        self.assertIsNone(self.registry.lookup_address(A))

    def test_concurrent_conflicts_are_atomic(self):
        """Allow only one winner when concurrent writers claim the same address."""

        def register(index):
            """
            Attempt one competing registration and report whether it claimed the
            address.
            """
            try:
                self.registry.register(f"host{index}.ret", A)
                return True
            except ConflictError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(register, range(16))), 1)
        self.assertEqual(len(self.registry.records()), 1)

    def test_cache_both_directions_share_expiration_and_survive_restart(self):
        """
        Restore both cache directions with the same original expiry after reopening.
        """
        self.cache.put(S, Record("home.ret", A, 10))
        self.now += 4
        reopened = Cache(self.root / "cache.sqlite", clock=lambda: self.now)
        self.assertEqual(reopened.lookup_name(S, "home.ret").ttl, 6)
        self.assertEqual(reopened.lookup_address(S, A).name, "home.ret")
        self.now += 6
        self.assertIsNone(reopened.lookup_name(S, "home.ret"))
        self.assertIsNone(reopened.lookup_address(S, A))

    def test_cache_zero_ttl_and_remap_invalidate_old_pairs(self):
        """Invalidate superseded pairs and retain no zero-TTL cache entry."""
        self.cache.put(S, Record("home.ret", A, 10))
        self.cache.put(S, Record("other.ret", A, 10))
        self.assertIsNone(self.cache.lookup_name(S, "home.ret"))
        self.cache.put(S, Record("other.ret", B, 10))
        self.assertIsNone(self.cache.lookup_address(S, A))
        self.cache.put(S, Record("other.ret", B, 0))
        self.assertIsNone(self.cache.lookup_name(S, "other.ret"))
        self.assertIsNone(self.cache.lookup_address(S, B))

    def test_cache_servers_are_isolated_and_reads_do_not_extend_ttl(self):
        """Keep authorities separate and prevent reads from renewing cached records."""
        self.cache.put(S, Record("home.ret", A, 10))
        self.cache.put(T, Record("home.ret", B, 10))
        for _ in range(9):
            self.now += 1
            self.assertEqual(self.cache.lookup_name(S, "home.ret").address, A)
        self.assertEqual(self.cache.lookup_name(T, "home.ret").address, B)
        self.now += 1
        self.assertIsNone(self.cache.lookup_name(S, "home.ret"))

    def test_cache_bounds_and_clock_rollback(self):
        """
        Evict excess entries and invalidate cache state after the clock moves back.
        """
        cache = Cache(
            self.root / "bounded.sqlite", max_records=2, clock=lambda: self.now
        )
        for name, address in (("a.ret", A), ("b.ret", B), ("c.ret", C)):
            cache.put(S, Record(name, address, 60))
            self.now += 1
        self.assertIsNone(cache.lookup_name(S, "a.ret"))
        self.now = 500
        self.assertIsNone(cache.lookup_name(S, "c.ret"))

    def test_invalid_inputs_leave_records_unchanged(self):
        """Reject malformed names, addresses and TTLs without creating registry rows."""
        for name in ("", ".", "a..b", "-a.ret", "a_b.ret", "a/ret", "a" * 64):
            with self.assertRaises(ValueError):
                self.registry.register(name, A)
        for ttl in (-1, True, 0.5, 604801):
            with self.assertRaises(ValueError):
                self.registry.register("valid.ret", A, ttl)
        for address in ("zz" * 16, "aa", b"short"):
            with self.assertRaises(ValueError):
                normalize_address(address)
        self.assertEqual(normalize_name("café.ret"), "xn--caf-dma.ret")
        self.assertEqual(self.registry.records(), [])

    def test_protocol_forward_reverse_miss_and_bad_queries(self):
        """
        Return matching pairs or explicit errors for missing and malformed queries.
        """
        self.registry.register("home.ret", A, 40)
        for kind, value in (("name", "HOME.RET"), ("address", A)):
            response = answer(
                self.registry, {"version": 1, "kind": kind, "value": value}
            )
            self.assertEqual(
                response["record"], {"name": "home.ret", "address": A, "ttl": 40}
            )
        self.assertEqual(
            answer(
                self.registry, {"version": 1, "kind": "name", "value": "missing.ret"}
            )["status"],
            "not_found",
        )
        for data in (
            {},
            None,
            {"version": True},
            {"version": 1, "kind": "register", "value": "home.ret"},
        ):
            self.assertEqual(answer(self.registry, data)["status"], "invalid_request")


class ResolverTests(Fixture):
    def test_resolver_cache_reverse_refresh_and_miss(self):
        """
        Reuse reverse cache entries and invalidate a refreshed missing registration.
        """
        self.registry.register("home.ret", A, 10)
        registry = self.registry

        class FakeTransport:
            calls = 0

            def query(self, payload):
                """
                Supply this fixture's reply to the resolver without unrelated network
                traffic.
                """
                self.calls += 1
                return answer(registry, payload)

            def close(self):
                """
                Satisfy the transport cleanup contract; this fixture owns no network
                resources.
                """
                pass

        transport = FakeTransport()
        resolver = Resolver(S, self.cache, transport=transport, require_announce=False)
        self.assertEqual(resolver.resolve("home.ret"), A)
        self.assertEqual(resolver.reverse(A), "home.ret")
        self.assertEqual(transport.calls, 1)
        self.now += 10
        self.assertEqual(resolver.reverse(A), "home.ret")
        self.assertEqual(transport.calls, 2)
        self.registry.remove("home.ret")
        with self.assertRaises(NameNotFound):
            resolver.lookup_name("home.ret", refresh=True)
        self.assertIsNone(self.cache.lookup_address(S, A))
        self.assertEqual(resolver.resolve(B), B)

    def test_invalid_or_mismatched_response_not_cached(self):
        """Keep malformed or unrelated authority replies out of the local cache."""

        class FakeTransport:
            response = None

            def query(self, payload):
                """
                Supply this fixture's reply to the resolver without unrelated network
                traffic.
                """
                return self.response

            def close(self):
                """
                Satisfy the transport cleanup contract; this fixture owns no network
                resources.
                """
                pass

        transport = FakeTransport()
        resolver = Resolver(S, self.cache, transport=transport, require_announce=False)
        for response in (
            {},
            {
                "version": 1,
                "status": "ok",
                "record": {"name": "wrong.ret", "address": A, "ttl": 30},
            },
            {
                "version": 1,
                "status": "ok",
                "record": {"name": "home.ret", "address": A, "ttl": -5},
            },
        ):
            transport.response = response
            with self.assertRaises(ConnectionError):
                resolver.lookup_name("home.ret")
            self.assertIsNone(self.cache.lookup_name(S, "home.ret"))

    def test_expired_cache_does_not_mask_network_failure(self):
        """Propagate a network timeout rather than returning an expired mapping."""
        self.cache.put(S, Record("home.ret", A, 1))
        self.now += 2

        class Offline:
            def query(self, payload):
                """
                Supply this fixture's reply to the resolver without unrelated network
                traffic.
                """
                raise TimeoutError("offline")

            def close(self):
                """
                Satisfy the transport cleanup contract; this fixture owns no network
                resources.
                """
                pass

        resolver = Resolver(S, self.cache, transport=Offline(), require_announce=False)
        with self.assertRaises(TimeoutError):
            resolver.resolve("home.ret")
