import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CLITests(unittest.TestCase):
    def test_local_registry_administration_and_conflict_exit_status(self):
        """Check command-line persistence and a nonzero status for conflicting pairs."""
        with tempfile.TemporaryDirectory() as temp:
            database = str(Path(temp) / "records.sqlite")

            def command(*args):
                """
                Run a registry command against the fixture database and capture its
                result.
                """
                return subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "retdns",
                        "records",
                        "--database",
                        database,
                        *args,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            first = command("add", "HOME.ret", "a" * 32, "--ttl", "20")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["name"], "home.ret")
            conflict = command("add", "other.ret", "a" * 32)
            self.assertEqual(conflict.returncode, 1)
            replaced = command("add", "other.ret", "a" * 32, "--replace")
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertEqual(json.loads(replaced.stdout)["ttl"], 86400)
            records = json.loads(command("list").stdout)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["name"], "other.ret")
            self.assertTrue(
                json.loads(command("remove", "other.ret").stdout)["removed"]
            )
            self.assertEqual(json.loads(command("list").stdout), [])
