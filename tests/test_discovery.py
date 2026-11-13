import unittest

import RNS
import RNS.vendor.umsgpack as msgpack

from retdns import Discovery


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        """Create isolated temporary state for this test's assertions."""
        self.now = 1000
        self.discovery = Discovery(ttl=10, max_services=2, clock=lambda: self.now)
        self.identity = RNS.Identity()
        self.address = RNS.Destination.hash(self.identity, "retdns", "server")

    def announce(self, data, address=None):
        """
        Feed the discovery handler a fixture announcement and selected destination.
        """
        self.discovery.received_announce(address or self.address, self.identity, data)

    def test_discovery_has_own_aspects_and_expires(self):
        """Discover only the DNS namespace and expire candidates on schedule."""
        self.assertEqual(self.discovery.aspect_filter, "retdns.server")
        self.announce(msgpack.packb({"version": 1, "name": "Local DNS"}))
        self.assertEqual(self.discovery.services()[0].address, self.address.hex())
        self.now += 10
        self.assertEqual(self.discovery.services(), [])

    def test_malformed_and_wrong_namespace_announcements_are_ignored(self):
        """Ignore invalid service data and destinations from unrelated protocols."""
        valid = msgpack.packb({"version": 1, "name": "Local DNS"})
        wrong = RNS.Destination.hash(self.identity, "nomadpl", "node")
        self.announce(valid, wrong)
        for data in (
            None,
            b"\x81",
            b"x" * 257,
            msgpack.packb([]),
            msgpack.packb({"version": 2, "name": "bad"}),
            msgpack.packb({"version": 1, "name": "bad\nname"}),
        ):
            self.announce(data)
        self.assertEqual(self.discovery.services(), [])

    def test_discovery_bounds(self):
        """Keep announced service candidates within the configured count limit."""
        for _ in range(3):
            self.identity = RNS.Identity()
            self.address = RNS.Destination.hash(self.identity, "retdns", "server")
            self.announce(msgpack.packb({"version": 1, "name": "DNS"}))
            self.now += 1
        self.assertEqual(len(self.discovery.services()), 2)
