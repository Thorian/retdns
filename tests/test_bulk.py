import tempfile
import unittest
from pathlib import Path

from retdns import Cache, Record, Registry, Resolver
from retdns.protocol import WIRE_LIMIT, answer

A, B, S = "a" * 32, "b" * 32, "c" * 32


class BulkTests(unittest.TestCase):
    def setUp(self):
        """Create isolated temporary state for this test's assertions."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000000
        self.registry = Registry(self.root / "records.sqlite", clock=lambda: self.now)
        self.cache = Cache(self.root / "cache.sqlite", clock=lambda: self.now)

    def query(self, kind, value):
        """
        Supply this fixture's reply to the resolver without unrelated network traffic.
        """
        return answer(self.registry, {"version": 1, "kind": kind, "value": value})

    def bulk(self, **options):
        """Ask the fixture authority for its configured bulk selection."""
        return answer(self.registry, {"version": 1, "kind": "bulk"}, **options)

    def test_default_ttl_is_24_hours(self):
        """Give a new registration a full-day cache lifetime when no TTL is supplied."""
        self.assertEqual(self.registry.register("home.ret", A).ttl, 86400)

    def test_rolling_window_combines_directions_and_ignores_admin_and_bulk_reads(self):
        """Count only successful network lookups within the exact popularity window."""
        self.registry.register("home.ret", A)
        self.registry.register("other.ret", B)
        self.assertEqual(self.bulk()["records"], [])
        self.query("name", "home.ret")
        self.now += 1
        self.query("address", A)
        self.query("name", "other.ret")
        self.query("name", "missing.ret")
        self.assertEqual(
            [r["name"] for r in self.bulk()["records"]], ["home.ret", "other.ret"]
        )
        self.assertEqual(
            [r["name"] for r in self.bulk()["records"]], ["home.ret", "other.ret"]
        )
        self.now += 86399
        # The event exactly 24 hours old no longer counts; ties use name order.
        self.assertEqual(
            [r["name"] for r in self.bulk()["records"]], ["home.ret", "other.ret"]
        )
        self.now += 1
        self.assertEqual(self.bulk()["records"], [])

    def test_top_25_limit_persistence_and_server_selection(self):
        """
        Keep ranking history across restarts while honoring the server's bulk policy.
        """
        for index in range(30):
            name, address = f"host{index:02}.ret", f"{index:032x}"
            self.registry.register(name, address)
            for _ in range(index + 1):
                self.query("name", name)
        batch = self.bulk()["records"]
        self.assertEqual(len(batch), 25)
        self.assertEqual(batch[0]["name"], "host29.ret")
        self.assertEqual(batch[-1]["name"], "host05.ret")
        reopened = Registry(self.root / "records.sqlite", clock=lambda: self.now)
        self.assertEqual(reopened.top_records()[0].name, "host29.ret")
        self.assertEqual(len(self.bulk(bulk_limit=3)["records"]), 3)
        self.assertEqual(self.bulk(bulk_limit=0)["records"], [])
        curated = self.bulk(
            bulk_policy=lambda registry: [registry.lookup_name("host01.ret")]
        )
        self.assertEqual([r["name"] for r in curated["records"]], ["host01.ret"])

    def test_deleted_and_replaced_mappings_leave_ranking(self):
        """Remove obsolete mappings and their counts from future bulk selections."""
        self.registry.register("home.ret", A)
        self.query("name", "home.ret")
        self.registry.register("home.ret", B, replace=True)
        self.assertEqual(self.bulk()["records"], [])
        self.query("name", "home.ret")
        self.registry.remove("home.ret")
        self.assertEqual(self.bulk()["records"], [])

    def test_custom_window_and_retention(self):
        """Apply a custom ranking window and prune history beyond retention."""
        self.registry.register("home.ret", A)
        self.query("name", "home.ret")
        self.now += 61
        self.assertEqual(self.bulk(bulk_window=60)["records"], [])
        self.assertEqual(len(self.bulk()["records"]), 1)
        self.now += 604800
        self.assertEqual(self.bulk()["records"], [])
        with self.registry.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM lookup_events").fetchone()[0], 0
            )

    def test_client_bulk_populates_both_directions_and_honors_ttl(self):
        """Warm forward and reverse caches from one batch without extending expiry."""
        self.registry.register("home.ret", A, 10)
        self.query("name", "home.ret")
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
        resolver = Resolver(S, self.cache, transport=transport)
        self.assertEqual(resolver.bulk()[0].name, "home.ret")
        self.assertEqual(resolver.resolve("home.ret"), A)
        self.assertEqual(resolver.reverse(A), "home.ret")
        self.assertEqual(transport.calls, 1)
        self.now += 10
        self.assertIsNone(self.cache.lookup_address(S, A))

    def test_invalid_batches_do_not_partially_update_cache(self):
        """Leave the cache untouched when any record makes the batch invalid."""

        class FakeTransport:
            records = []

            def query(self, payload):
                """
                Supply this fixture's reply to the resolver without unrelated network
                traffic.
                """
                return {"version": 1, "status": "ok", "records": self.records}

            def close(self):
                """
                Satisfy the transport cleanup contract; this fixture owns no network
                resources.
                """
                pass

        transport = FakeTransport()
        resolver = Resolver(S, self.cache, transport=transport)
        good = Record("home.ret", A, 10).to_wire()
        for records in (
            [good, {"name": "bad.ret", "address": B, "ttl": -1}],
            [good, Record("other.ret", A, 10).to_wire()],
            [good, good],
            [good] * 101,
        ):
            transport.records = records
            with self.assertRaises(ConnectionError):
                resolver.bulk()
            self.assertIsNone(self.cache.lookup_name(S, "home.ret"))

    def test_atomic_swap_and_zero_ttl_records(self):
        """Apply pair swaps atomically and avoid retaining zero-TTL records."""
        self.cache.put_many(S, [Record("home.ret", A, 10), Record("other.ret", B, 10)])
        self.cache.put_many(S, [Record("home.ret", B, 10), Record("other.ret", A, 0)])
        self.assertEqual(self.cache.lookup_address(S, B).name, "home.ret")
        self.assertIsNone(self.cache.lookup_address(S, A))
        self.assertIsNone(self.cache.lookup_name(S, "other.ret"))

    def test_maximum_batch_fits_wire_bound(self):
        """Fit the largest allowed set of maximum-length names into the wire limit."""
        import RNS.vendor.umsgpack as msgpack

        records = [
            Record(
                f"{i:03}" + "a" * 60 + "." + ".".join(["b" * 63, "c" * 63, "d" * 61]),
                f"{i:032x}",
                86400,
            ).to_wire()
            for i in range(100)
        ]
        self.assertLess(
            len(msgpack.packb({"version": 1, "status": "ok", "records": records})),
            WIRE_LIMIT,
        )
