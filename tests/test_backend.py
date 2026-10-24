"""Backend parsing tests with a fake MI channel (no GDB, no hardware).

These cover the translation from MI records to the public models -- the layer
that a hardware smoke test would otherwise be the only way to exercise.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.backend import JLinkGdbBackend  # noqa: E402
from embdbg.config import Config  # noqa: E402
from embdbg.mi import MIRecord  # noqa: E402


def done(payload=None):
    return MIRecord(None, "result", "done", payload or {})


class FakeMI:
    def __init__(self):
        self.commands: list[str] = []
        self.is_running = False
        self.is_closed = False
        self.last_stop = None
        self.memory_chunks = [
            {"begin": "0x20000000", "offset": "0x0", "contents": "0102"},
            {"begin": "0x20000000", "offset": "0x2", "contents": "0304"},
        ]

    def execute(self, command, timeout=None, check=True):
        self.commands.append(command)
        if command.startswith("-data-list-register-names"):
            return done({"register-names": ["r0", "r1", "pc"]})
        if command.startswith("-data-list-register-values"):
            return done(
                {
                    "register-values": [
                        {"number": "0", "value": "0x1"},
                        {"number": "1", "value": "0x2"},
                        {"number": "2", "value": "0x0800b3a4"},
                    ]
                }
            )
        if command.startswith("-data-evaluate-expression"):
            return done({"value": "0x20000000 <counter>"})
        if command.startswith("-data-read-memory-bytes"):
            return done({"memory": list(self.memory_chunks)})
        if command.startswith("-data-write-memory-bytes"):
            return done()
        if command.startswith("-break-list"):
            return done(
                {
                    "BreakpointTable": {
                        "body": [
                            {
                                "bkpt": {
                                    "number": "1",
                                    "type": "breakpoint",
                                    "enabled": "y",
                                    "addr": "0x0800b3a8",
                                    "func": "main",
                                    "file": "main.c",
                                    "line": "114",
                                    "times": "3",
                                }
                            }
                        ]
                    }
                }
            )
        if command.startswith("-stack-list-frames"):
            return done(
                {
                    "stack": [
                        {"frame": {"level": "0", "addr": "0x0800b3a8", "func": "main", "file": "main.c", "line": "114"}},
                        {"frame": {"level": "1", "addr": "0x0800b200", "func": "Reset_Handler", "file": "startup.c", "line": "80"}},
                    ]
                }
            )
        return done()

    def execute_console(self, command, timeout=None, check=True):
        self.commands.append(f"console:{command}")
        return "", done()

    def wait_for_stop(self, timeout=None):
        return None

    def drain_stops(self):
        return []

    def interrupt(self):
        pass


class TestBackendParsing(unittest.TestCase):
    def setUp(self):
        self.config = Config(device="STM32G431CB", verify=False)
        self.backend = JLinkGdbBackend(self.config)
        self.mi = FakeMI()
        self.backend.mi = self.mi  # bypass open(): no probe needed

    def test_read_memory_assembles_offsets(self):
        data = self.backend.read_memory(0x20000000, 4)
        self.assertEqual(data, b"\x01\x02\x03\x04")

    def test_write_memory_hex_encoding(self):
        self.backend.write_memory(0x20000000, b"\xde\xad\xbe\xef")
        self.assertIn("-data-write-memory-bytes 0x20000000 deadbeef", self.mi.commands)

    def test_read_registers(self):
        registers = self.backend.read_registers()
        self.assertEqual(registers["r0"].value, 1)
        self.assertEqual(registers["pc"].value, 0x0800B3A4)
        self.assertEqual(registers["pc"].hex, "0x0800b3a4")

    def test_list_breakpoints(self):
        breakpoints = self.backend.list_breakpoints()
        self.assertEqual(len(breakpoints), 1)
        bp = breakpoints[0]
        self.assertEqual(bp.number, 1)
        self.assertEqual(bp.func, "main")
        self.assertEqual(bp.line, 114)
        self.assertTrue(bp.enabled)
        self.assertEqual(bp.hit_count, 3)

    def test_backtrace(self):
        frames = self.backend.backtrace()
        self.assertEqual([f.level for f in frames], [0, 1])
        self.assertEqual(frames[1].func, "Reset_Handler")

    def test_resolve_symbol(self):
        self.assertEqual(self.backend.resolve_symbol("0x20000000"), 0x20000000)
        self.assertEqual(self.backend.resolve_symbol("counter"), 0x20000000)

    def test_flash_control_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "firmware.elf"
            image.write_bytes(b"\x00" * 16)
            result = self.backend.flash(str(image), run=False, verify=False)
        self.assertFalse(result.ran_after)
        joined = "\n".join(self.mi.commands)
        self.assertIn("-file-exec-and-symbols", joined)
        self.assertIn("-target-download", joined)
        self.assertIn("console:monitor reset", joined)
        self.assertNotIn("-exec-continue", joined)

    def test_target_info(self):
        info = self.backend.target_info()
        self.assertEqual(info.device, "STM32G431CB")
        self.assertEqual(info.state, "halted")
        self.assertEqual(info.pc, 0x20000000)


if __name__ == "__main__":
    unittest.main()
