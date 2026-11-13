import logging
import math
import threading
import time
from pathlib import Path

from .admission import RequestGate
from .models import normalize_address
from .protocol import ASPECTS, LOOKUP_PATH, VERSION, WIRE_LIMIT, answer


def load_identity(path):
    """Load the service key or create a new private file without overwriting it."""
    import RNS

    path = Path(path)
    if path.exists():
        identity = RNS.Identity.from_file(str(path))
        if identity is None:
            raise ValueError(f"Invalid identity file: {path}")
        return identity
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = RNS.Identity()
    with path.open("xb") as stream:
        stream.write(identity.get_private_key())
    path.chmod(0o600)
    return identity


class Server:
    """Serve registry queries and announce a retdns.server destination."""

    def __init__(
        self,
        identity,
        registry,
        *,
        name="retdns",
        bulk_limit=25,
        bulk_window=86400,
        bulk_policy=None,
        max_requests=8,
        request_rate=32,
        request_burst=64,
    ):
        """
        Register the lookup endpoint and configure server-controlled bulk selection.
        """
        import RNS

        if (
            not isinstance(name, str)
            or not 1 <= len(name.encode("utf-8")) <= 128
            or any(ord(c) < 32 for c in name)
        ):
            raise ValueError("Service name must be 1–128 UTF-8 bytes without controls")
        self.registry, self.name = registry, name
        self._gate = RequestGate(max_requests, request_rate, request_burst)
        if type(bulk_limit) is not int or not 0 <= bulk_limit <= 100:
            raise ValueError("Bulk limit must be between 0 and 100")
        if type(bulk_window) is not int or not 1 <= bulk_window <= 604800:
            raise ValueError("Bulk window must be 1–604800 seconds")
        if bulk_policy is not None and not callable(bulk_policy):
            raise ValueError("Bulk policy must be callable")
        self.bulk_limit, self.bulk_window, self.bulk_policy = (
            bulk_limit,
            bulk_window,
            bulk_policy,
        )
        self.destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE, *ASPECTS
        )
        self.destination.set_max_request_size(WIRE_LIMIT)
        self.destination.register_request_handler(
            LOOKUP_PATH,
            response_generator=self._respond,
            allow=RNS.Destination.ALLOW_ALL,
        )

    @property
    def address(self):
        """Return the name service's destination hash as hexadecimal text."""
        return self.destination.hash.hex()

    def announce(self):
        """Announce the protocol version and display name in MessagePack app data."""
        import RNS.vendor.umsgpack as msgpack

        self.destination.announce(
            app_data=msgpack.packb({"version": VERSION, "name": self.name})
        )

    def _respond(self, path, data, request_id, link_id, remote_identity, requested_at):
        """Reject excess work before parsing requests or accessing application state."""
        with self._gate.admit() as accepted:
            if not accepted:
                return {"version": VERSION, "status": "server_busy"}
            return self._handle_request(
                path, data, request_id, link_id, remote_identity, requested_at
            )

    def _handle_request(
        self, path, data, request_id, link_id, remote_identity, requested_at
    ):
        """Answer a query and hide internal registry errors behind a protocol status."""
        try:
            return answer(
                self.registry,
                data,
                bulk_limit=self.bulk_limit,
                bulk_window=self.bulk_window,
                bulk_policy=self.bulk_policy,
            )
        except Exception:
            logging.exception("retdns registry query failed")
            return {"version": VERSION, "status": "server_error"}


class Transport:
    """Authenticated encrypted link to a selected retdns.server address."""

    def __init__(self, server, *, timeout=30):
        """Prepare a serialized link to the explicitly selected name service."""
        self.address = bytes.fromhex(normalize_address(server))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be finite and positive")
        self.timeout, self._link = timeout, None
        self._lock = threading.Lock()

    def _wait(self, ready, deadline):
        """Wait for a routing or link condition within the query deadline."""
        while not ready():
            if time.monotonic() >= deadline:
                raise TimeoutError("retdns path/link timed out")
            time.sleep(0.05)

    def query(self, payload):
        """Verify the service namespace and exchange a bounded lookup message."""
        import RNS

        if not self._lock.acquire(timeout=self.timeout):
            raise TimeoutError("retdns request queue timed out")
        try:
            deadline = time.monotonic() + self.timeout
            if self._link is None or self._link.status != RNS.Link.ACTIVE:
                if not RNS.Transport.has_path(self.address):
                    RNS.Transport.request_path(self.address)
                self._wait(lambda: RNS.Transport.has_path(self.address), deadline)
                identity = RNS.Identity.recall(self.address)
                if identity is None:
                    raise ConnectionError("DNS server identity unavailable")
                destination = RNS.Destination(
                    identity, RNS.Destination.OUT, RNS.Destination.SINGLE, *ASPECTS
                )
                if destination.hash != self.address:
                    raise ConnectionError(
                        "Selected destination is not a retdns.server destination"
                    )
                self._link = RNS.Link(destination)
                self._wait(
                    lambda: self._link.status in (RNS.Link.ACTIVE, RNS.Link.CLOSED),
                    deadline,
                )
                if self._link.status != RNS.Link.ACTIVE:
                    raise ConnectionError("DNS server link closed")
            done, result = threading.Event(), {}

            def received(receipt):
                """Save the decoded reply and signal that the query has completed."""
                result["response"] = receipt.response
                done.set()

            def failed(receipt):
                """Wake the waiting query after Reticulum reports a failed exchange."""
                result["failed"] = True
                done.set()

            remaining = max(0.01, deadline - time.monotonic())
            receipt = self._link.request(
                LOOKUP_PATH,
                data=payload,
                response_callback=received,
                failed_callback=failed,
                timeout=remaining,
                max_response_size=WIRE_LIMIT,
            )
            if receipt is False:
                raise ConnectionError("Could not send retdns request")
            if not done.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("retdns response timed out")
            if result.get("failed"):
                raise ConnectionError("retdns request failed")
            return result["response"]
        except Exception:
            if self._link is not None:
                self._link.teardown()
                self._link = None
            raise
        finally:
            self._lock.release()

    def close(self):
        """Close the service link after any current query has finished."""
        with self._lock:
            if self._link is not None:
                self._link.teardown()
                self._link = None
