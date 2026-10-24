"""Plugin manager and serial plugin tests (loopback, no hardware)."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.config import Config  # noqa: E402
from embdbg.plugins.base import Plugin, PluginError, PluginManager  # noqa: E402
from embdbg.plugins.serial import SerialPlugin, encode_payload  # noqa: E402


class CountingPlugin(Plugin):
    name = "counting"
    description = "test plugin"

    def __init__(self, options=None, context=None):
        super().__init__(options, context)
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class TestPluginManager(unittest.TestCase):
    def test_serial_is_builtin(self):
        self.assertIn("serial", PluginManager.available())

    def test_load_custom_spec_and_lifecycle(self):
        manager = PluginManager()
        plugin = manager.load(f"{__name__}:CountingPlugin", {"answer": 42})
        self.assertEqual(plugin.name, "counting")
        self.assertEqual(plugin.options["answer"], 42)
        manager.start_all()
        manager.stop_all()
        self.assertEqual(plugin.started, 1)
        self.assertEqual(plugin.stopped, 1)

    def test_unknown_plugin(self):
        with self.assertRaises(PluginError):
            PluginManager().load("does-not-exist")

    def test_load_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embdbg.toml"
            path.write_text(
                "[embdbg]\ndevice = \"STM32G431CB\"\n\n"
                "[plugins.serial]\nport = \"loop://\"\nmode = \"idle\"\n",
                encoding="utf-8",
            )
            config = Config.load(path)
            manager = PluginManager()
            loaded = manager.load_from_config(config)
            self.assertEqual([p.name for p in loaded], ["serial"])
            manager.start_all()
            self.assertTrue(manager.get("serial").is_open)
            manager.stop_all()
            self.assertFalse(manager.get("serial").is_open)

    def test_disabled_plugin_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embdbg.toml"
            path.write_text(
                "[plugins.serial]\nport = \"loop://\"\nenabled = false\n",
                encoding="utf-8",
            )
            config = Config.load(path)
            self.assertEqual(PluginManager().load_from_config(config), [])


class TestSerialPlugin(unittest.TestCase):
    def test_loopback_roundtrip(self):
        with SerialPlugin({"port": "loop://", "baudrate": 115200}) as plugin:
            plugin.write(b"ping")
            matched, received = plugin.expect(b"ping", timeout=1.0)
        self.assertTrue(matched)
        self.assertEqual(received, b"ping")

    def test_expect_timeout(self):
        with SerialPlugin({"port": "loop://"}) as plugin:
            plugin.write(b"aaa")
            matched, received = plugin.expect(b"zzz", timeout=0.2)
        self.assertFalse(matched)
        self.assertEqual(received, b"aaa")

    def test_monitor_text_lines(self):
        plugin = SerialPlugin({"port": "loop://", "timestamp": False})
        plugin.open()
        try:
            plugin.write(b"line1\nline2\n")
            sink = io.StringIO()
            plugin.monitor(duration=0.3, out=sink)
        finally:
            plugin.close()
        self.assertIn("line1", sink.getvalue())
        self.assertIn("line2", sink.getvalue())

    def test_monitor_hex(self):
        plugin = SerialPlugin({"port": "loop://", "timestamp": False, "hex": True})
        plugin.open()
        try:
            plugin.write(b"\x01\x02")
            sink = io.StringIO()
            plugin.monitor(duration=0.3, out=sink)
        finally:
            plugin.close()
        self.assertIn("01 02", sink.getvalue())

    def test_invalid_parameters(self):
        with self.assertRaises(PluginError):
            SerialPlugin({"port": "loop://", "parity": "X"})
        with self.assertRaises(PluginError):
            SerialPlugin({})


class TestPayloadEncoding(unittest.TestCase):
    def test_text_with_newline(self):
        self.assertEqual(encode_payload("hi"), b"hi\n")
        self.assertEqual(encode_payload("hi", newline="crlf"), b"hi\r\n")
        self.assertEqual(encode_payload("hi", newline="none"), b"hi")

    def test_hex(self):
        self.assertEqual(encode_payload("de ad be ef", hex_mode=True, newline="none"), b"\xde\xad\xbe\xef")
        self.assertEqual(encode_payload("DEADBEEF", hex_mode=True, newline="none"), b"\xde\xad\xbe\xef")

    def test_bad_hex(self):
        with self.assertRaises(PluginError):
            encode_payload("xyz", hex_mode=True)


if __name__ == "__main__":
    unittest.main()
