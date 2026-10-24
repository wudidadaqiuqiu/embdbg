"""End-to-end MI test against the real ELF, but *without* a target.

GDB is started with the firmware image and queried for symbols/breakpoint
resolution only.  Nothing touches the J-Link, so this is safe to run on CI and
while the board is executing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from focdbg.discovery import find_gdb  # noqa: E402
from focdbg.errors import ToolNotFoundError  # noqa: E402
from focdbg.mi import MIClient  # noqa: E402

PROJECT_ROOT = Path(r"C:\docs\uni\research\PAT\foc\foc_open\software\Source_Project")
ELF = PROJECT_ROOT / "build" / "Debug" / "foc.elf"


@unittest.skipUnless(ELF.is_file(), f"firmware ELF not found at {ELF}")
class TestOfflineGdb(unittest.TestCase):
    client: MIClient

    @classmethod
    def setUpClass(cls):
        try:
            gdb = find_gdb()
        except ToolNotFoundError as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(str(exc))
        cls.client = MIClient(str(gdb), [str(ELF)], timeout=30)
        cls.client.launch()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "client"):
            cls.client.close()

    def test_symbol_resolution(self):
        record = self.client.execute('-data-evaluate-expression "&main"')
        value = str(record.get("value", ""))
        self.assertTrue(value.startswith("0x"), value)

    def test_breakpoint_insert(self):
        record = self.client.execute('-break-insert -f "main"')
        breakpoint = record.get("bkpt")
        self.assertIsNotNone(breakpoint)
        self.assertEqual(breakpoint["func"], "main")
        self.assertTrue(int(breakpoint["addr"], 0) > 0x08000000)

    def test_register_names(self):
        record = self.client.execute("-data-list-register-names")
        names = record.get("register-names") or []
        self.assertIn("pc", names)
        self.assertIn("sp", names)

    def test_console_command(self):
        text, record = self.client.execute_console("info address main")
        self.assertEqual(record.cls, "done")
        self.assertIn("main", text)

    def test_error_is_raised(self):
        from focdbg.errors import GdbError

        with self.assertRaises(GdbError):
            self.client.execute('-data-evaluate-expression "no_such_symbol_xyz"')


if __name__ == "__main__":
    unittest.main()
