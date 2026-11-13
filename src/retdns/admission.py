"""Nonblocking admission limits for application request handlers."""

import math
import threading
import time
from contextlib import contextmanager


class RequestGate:
    def __init__(self, max_requests=8, rate=8, burst=16, *, clock=time.monotonic):
        """Bound concurrent work and sustained admissions without keeping peer state."""
        if type(max_requests) is not int or max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be finite and positive")
        if type(burst) is not int or burst <= 0:
            raise ValueError("burst must be a positive integer")
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens, self.updated = float(burst), clock()
        self.slots = threading.BoundedSemaphore(max_requests)
        self.lock = threading.Lock()

    @contextmanager
    def admit(self):
        """Yield whether work may run, releasing its slot even after an exception."""
        accepted = False
        with self.lock:
            now = self.clock()
            self.tokens = min(
                self.burst, self.tokens + max(0, now - self.updated) * self.rate
            )
            self.updated = now
            if self.tokens >= 1 and self.slots.acquire(blocking=False):
                self.tokens -= 1
                accepted = True
        try:
            yield accepted
        finally:
            if accepted:
                self.slots.release()
