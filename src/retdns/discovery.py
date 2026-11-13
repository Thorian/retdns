import math
import threading
import time
from dataclasses import dataclass

from .models import normalize_address
from .protocol import ASPECTS, VERSION


@dataclass(frozen=True)
class Service:
    address: str
    name: str
    expires_at: float


class Discovery:
    """Bounded service discovery; candidates are not automatically trusted."""

    aspect_filter = ".".join(ASPECTS)
    receive_path_responses = True

    def __init__(self, *, ttl=3600, max_services=256, clock=time.time):
        """Configure the lifetime and bound of discovered service candidates."""
        if (
            not math.isfinite(ttl)
            or ttl <= 0
            or type(max_services) is not int
            or max_services <= 0
        ):
            raise ValueError("Discovery limits must be positive")
        self.ttl, self.max_services, self.clock = ttl, max_services, clock
        self._services, self._lock = {}, threading.Lock()

    def start(self):
        """Register this namespace-filtered handler with Reticulum transport."""
        import RNS

        RNS.Transport.register_announce_handler(self)
        return self

    def close(self):
        """Stop receiving announcements through this discovery handler."""
        import RNS

        RNS.Transport.deregister_announce_handler(self)

    def received_announce(self, destination_hash, announced_identity, app_data):
        """Validate a service announcement before refreshing its local expiry."""
        import RNS
        import RNS.vendor.umsgpack as msgpack

        try:
            if not isinstance(app_data, bytes) or len(app_data) > 256:
                return
            if destination_hash != RNS.Destination.hash(announced_identity, *ASPECTS):
                return
            data = msgpack.unpackb(app_data)
            name = data["name"]
            if (
                type(data["version"]) is not int
                or data["version"] != VERSION
                or not isinstance(name, str)
                or not 1 <= len(name.encode("utf-8")) <= 128
                or any(ord(c) < 32 for c in name)
            ):
                return
            address, now = normalize_address(destination_hash), self.clock()
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            msgpack.UnpackException,
        ):
            return
        with self._lock:
            self._prune(now)
            self._services[address] = Service(address, name, now + self.ttl)
            while len(self._services) > self.max_services:
                oldest = min(
                    self._services, key=lambda key: self._services[key].expires_at
                )
                del self._services[oldest]

    def _prune(self, now):
        """Discard expired candidates and entries invalidated by a clock rollback."""
        self._services = {
            key: value
            for key, value in self._services.items()
            if now < value.expires_at <= now + self.ttl
        }

    def services(self):
        """Return live service candidates in a stable display order."""
        with self._lock:
            self._prune(self.clock())
            return sorted(
                self._services.values(), key=lambda value: (value.name, value.address)
            )
