"""Security regressions for bounded application admission."""

import unittest

from retdns.admission import RequestGate


class AdmissionTests(unittest.TestCase):
    def test_concurrency_release_and_rate_refill(self):
        """Reject excess work, release failed handlers, and refill only over time."""
        now = [0.0]
        gate = RequestGate(1, 1, 2, clock=lambda: now[0])
        with self.assertRaises(RuntimeError):
            with gate.admit() as accepted:
                self.assertTrue(accepted)
                with gate.admit() as excess:
                    self.assertFalse(excess)
                raise RuntimeError("application failed")
        with gate.admit() as accepted:
            self.assertTrue(accepted)
        with gate.admit() as accepted:
            self.assertFalse(accepted)
        now[0] = 1
        with gate.admit() as accepted:
            self.assertTrue(accepted)

    def test_transport_rejects_before_application_work(self):
        """An exhausted service gate never invokes its application or registry."""
        from unittest.mock import Mock

        from retdns.transport import Server

        service = object.__new__(Server)
        service._gate = RequestGate(1, 1, 1, clock=lambda: 0)
        service._handle_request = Mock()
        with service._gate.admit():
            response = service._respond(None, None, None, None, None, None)
        self.assertEqual(response["status"], "server_busy")
        service._handle_request.assert_not_called()


class HistoryTests(unittest.TestCase):
    def test_history_stays_bounded_and_trims_on_upgrade(self):
        """Repeated valid lookups cannot grow history beyond the configured row limit."""
        import tempfile
        from pathlib import Path

        from retdns import Registry
        from retdns.protocol import answer

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "registry.sqlite"
            registry = Registry(path, max_history=3)
            registry.register("test.ret", "a" * 32)
            for _ in range(10):
                self.assertEqual(
                    answer(
                        registry, {"version": 1, "kind": "name", "value": "test.ret"}
                    )["status"],
                    "ok",
                )
            with registry.connect() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM lookup_events").fetchone()[0], 3
                )
            upgraded = Registry(path, max_history=2)
            with upgraded.connect() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM lookup_events").fetchone()[0], 2
                )
            self.assertEqual(upgraded.top_records()[0].name, "test.ret")
