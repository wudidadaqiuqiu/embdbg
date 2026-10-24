"""CLI tests for the plugin and serial commands (loopback only)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.cli import main  # noqa: E402


def run_cli(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class TestPluginCli(unittest.TestCase):
    def test_plugins_list_json(self):
        code, out, _ = run_cli(["plugins", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        names = [entry["name"] for entry in payload["plugins"]]
        self.assertIn("serial", names)

    def test_serial_send_expect(self):
        code, out, _ = run_cli(
            ["serial", "send", "hello", "--port", "loop://", "--expect", "hello", "--json"]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["matched"])
        self.assertIn("hello", payload["received"])

    def test_serial_send_expect_timeout(self):
        code, out, _ = run_cli(
            [
                "serial",
                "send",
                "hello",
                "--port",
                "loop://",
                "--expect",
                "nope",
                "--expect-timeout",
                "0.2",
                "--json",
            ]
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])

    def test_serial_hex_send(self):
        code, out, _ = run_cli(["serial", "send", "DEADBEEF", "--port", "loop://", "--hex", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["written"], 4)

    def test_serial_monitor_duration(self):
        code, _, _ = run_cli(["serial", "monitor", "--port", "loop://", "--duration", "0.2"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
