"""Utility tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from focdbg.util import find_free_ports, hexdump, mi_quote, parse_int  # noqa: E402


class TestParseInt(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_int("42"), 42)
        self.assertEqual(parse_int("0x2a"), 42)
        self.assertEqual(parse_int("0b101010"), 42)
        self.assertEqual(parse_int("0o52"), 42)
        self.assertEqual(parse_int("1_000"), 1000)
        self.assertEqual(parse_int("-0x10"), -16)

    def test_rejects(self):
        for bad in ("", "12abc", "0x", "foo", None):
            with self.assertRaises(ValueError):
                parse_int(bad)


class TestMiQuote(unittest.TestCase):
    def test_quotes_and_backslashes(self):
        self.assertEqual(mi_quote("main"), '"main"')
        self.assertEqual(mi_quote('a"b'), '"a\\"b"')
        self.assertEqual(mi_quote("C:\\x"), '"C:\\\\x"')


class TestHexdump(unittest.TestCase):
    def test_shape(self):
        text = hexdump(bytes(range(16)))
        self.assertIn("00 01 02 03 04 05 06 07  08 09 0a 0b 0c 0d 0e 0f", text)
        self.assertIn("|................|", text)

    def test_base_address(self):
        text = hexdump(b"\x00", base=0x20000000)
        self.assertTrue(text.startswith("20000000"))


class TestFreePorts(unittest.TestCase):
    def test_returns_usable_base(self):
        port = find_free_ports(32000, 3)
        self.assertGreaterEqual(port, 32000)


if __name__ == "__main__":
    unittest.main()
