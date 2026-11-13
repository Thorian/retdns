import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.environ.get("RETDNS_NETWORK_TEST") == "1",
    "Set RETDNS_NETWORK_TEST=1 for real Reticulum test",
)
class NetworkTests(unittest.TestCase):
    def test_announcements_forward_reverse_ttl_and_use_of_resolved_destination(self):
        """
        Resolve and cache a real service name before connecting to its application.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            for mode in ("server", "client"):
                (root / mode).mkdir()
            common = "[reticulum]\n  share_instance = No\n[interfaces]\n"
            (root / "server" / "config").write_text(
                common
                + f"  [[DNS listener]]\n    type = TCPServerInterface\n    enabled = Yes\n    listen_ip = 127.0.0.1\n    listen_port = {port}\n"
            )
            (root / "client" / "config").write_text(
                common
                + f"  [[DNS client]]\n    type = TCPClientInterface\n    enabled = Yes\n    target_host = 127.0.0.1\n    target_port = {port}\n"
            )
            fixture = Path(__file__).with_name("network_peer.py")
            with (root / "server.log").open("w+") as log:
                server = subprocess.Popen(
                    [sys.executable, "-u", str(fixture), "server", str(root)],
                    stdout=log,
                    stderr=log,
                )
                try:
                    deadline = time.monotonic() + 20
                    while not (root / "ready.json").exists():
                        if server.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            self.fail(log.read())
                        time.sleep(0.1)
                    result = subprocess.run(
                        [sys.executable, "-u", str(fixture), "client", str(root)],
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertIn("RETDNS_NETWORK_OK", result.stdout)
                finally:
                    (root / "stop").touch()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
