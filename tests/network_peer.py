"""Actual server/client subprocesses, with an application using a resolved name."""

import json
import sys
import threading
import time
from pathlib import Path

import RNS

from retdns import Cache, Discovery, NameNotFound, Registry, Resolver, Server
from retdns.transport import Transport, load_identity

mode, root = sys.argv[1], Path(sys.argv[2])
RNS.Reticulum(configdir=str(root / mode))
if mode == "server":
    identity = load_identity(root / "dns.identity")
    assert identity.hash == load_identity(root / "dns.identity").hash
    site_identity = load_identity(root / "site.identity")
    site = RNS.Destination(
        site_identity, RNS.Destination.IN, RNS.Destination.SINGLE, "retdns_test", "site"
    )

    def respond(path, data, request_id, link_id, remote_identity, requested_at):
        """Return a recognizable application reply through the resolved destination."""
        return b"hello home.ret"

    site.register_request_handler(
        "/hello", response_generator=respond, allow=RNS.Destination.ALLOW_ALL
    )
    registry = Registry(root / "records.sqlite")
    registry.register("home.ret", site.hash.hex(), 2)
    server = Server(identity, registry, name="Test DNS")
    assert server.destination.hash == RNS.Destination.hash(identity, "retdns", "server")
    assert server.destination.hash != RNS.Destination.hash(identity, "nomadpl", "node")
    assert server.destination.hash != RNS.Destination.hash(
        identity, "nomadnetwork", "node"
    )
    (root / "ready.json").write_text(
        json.dumps({"server": server.address, "site": site.hash.hex()})
    )
    next_announce = 0
    while not (root / "stop").exists():
        if time.monotonic() >= next_announce:
            server.announce()
            site.announce()
            next_announce = time.monotonic() + 2
        time.sleep(0.1)
else:
    ready = json.loads((root / "ready.json").read_text())
    discovery = Discovery().start()
    try:
        deadline = time.monotonic() + 20
        while not discovery.services() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert any(
            service.address == ready["server"] and service.name == "Test DNS"
            for service in discovery.services()
        )

        class CountingTransport(Transport):
            calls = 0

            def query(self, data):
                """Count actual network queries so cache hits can be distinguished."""
                self.calls += 1
                return super().query(data)

        transport = CountingTransport(ready["server"], timeout=20)
        cache = Cache(root / "cache.sqlite")
        with Resolver(ready["server"], cache, transport=transport) as resolver:
            address = resolver.resolve("HOME.RET.")
            assert address == ready["site"]
            assert resolver.reverse(address) == "home.ret"
            assert transport.calls == 1
            # Reopen a resolver/cache: reverse is available without a network call.
            reopened_transport = CountingTransport(ready["server"], timeout=20)
            with Resolver(
                ready["server"],
                Cache(root / "cache.sqlite"),
                transport=reopened_transport,
            ) as reopened:
                assert reopened.reverse(address) == "home.ret"
                assert reopened_transport.calls == 0
            time.sleep(2.1)
            assert resolver.reverse(address) == "home.ret"
            assert transport.calls == 2
            cache.clear()
            batch = resolver.bulk()
            assert len(batch) == 1 and batch[0].name == "home.ret" and batch[0].ttl == 2
            assert resolver.resolve("home.ret") == address
            assert resolver.reverse(address) == "home.ret"
            assert transport.calls == 3
            try:
                resolver.lookup_name("missing.ret")
                raise AssertionError("missing name should fail")
            except NameNotFound:
                pass
            # Feed the name-resolved destination to a normal Reticulum app.
            destination_hash = bytes.fromhex(address)
            if not RNS.Transport.has_path(destination_hash):
                RNS.Transport.request_path(destination_hash)
            deadline = time.monotonic() + 15
            while (
                not RNS.Transport.has_path(destination_hash)
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            identity = RNS.Identity.recall(destination_hash)
            destination = RNS.Destination(
                identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "retdns_test",
                "site",
            )
            assert destination.hash.hex() == address
            link = RNS.Link(destination)
            while link.status != RNS.Link.ACTIVE and time.monotonic() < deadline:
                time.sleep(0.05)
            done, response = threading.Event(), {}

            def received(receipt):
                """Capture the application reply and unblock the integration test."""
                response["body"] = receipt.response
                done.set()

            link.request("/hello", response_callback=received, timeout=10)
            assert done.wait(12) and response["body"] == b"hello home.ret"
            link.teardown()
        print("RETDNS_NETWORK_OK", flush=True)
    finally:
        discovery.close()
