"""Console interpreter tests against a fake session (no hardware)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from focdbg.console import CommandInterpreter  # noqa: E402
from focdbg.errors import TargetError  # noqa: E402
from focdbg.models import Breakpoint, ConnectionInfo, Frame, RegisterValue, StopEvent, TargetInfo  # noqa: E402


class FakeSession:
    def __init__(self):
        self.running = False
        self.steps = 0
        self.resets = []
        self.written = []
        self.breakpoints: list[Breakpoint] = []
        self.stopped = StopEvent(reason="breakpoint-hit", breakpoint=1, frame=Frame(0, 0x0800B3A8, "main", "main.c", 114))
        self.connection = ConnectionInfo(backend="fake", device="STM32G431CB")

    # execution
    def halt(self):
        self.running = False
        return self.stopped

    def resume(self):
        self.running = True

    def step(self, *, into=True, instruction=False, count=1):
        self.steps += count
        return self.stopped

    def reset(self, halt=True):
        self.resets.append(halt)

    def wait_for_stop(self, timeout=None):
        return self.stopped

    # memory / registers
    def read_memory(self, address, length):
        return bytes((address + i) & 0xFF for i in range(length))

    def write_memory(self, address, data):
        self.written.append((address, bytes(data)))

    def read_registers(self, names=None):
        return {"pc": RegisterValue("pc", 15, 0x0800B3A4)}

    def write_register(self, name, value):
        self.written.append((name, value))

    # inspection
    def set_breakpoint(self, location, *, hardware=None, condition=None, temporary=False):
        bp = Breakpoint(number=len(self.breakpoints) + 1, addr=0x0800B3A8, func=location)
        self.breakpoints.append(bp)
        return bp

    def remove_breakpoint(self, number):
        self.breakpoints = [b for b in self.breakpoints if b.number != number]

    def list_breakpoints(self):
        return list(self.breakpoints)

    def backtrace(self, max_frames=32):
        return [Frame(0, 0x0800B3A8, "main", "main.c", 114)]

    def evaluate(self, expression):
        return "0x0800b3a4 <pc>"

    def resolve_symbol(self, name):
        if name in ("main", "pc"):
            return 0x0800B3A4
        raise TargetError(f"no symbol {name!r}")

    def threads(self):
        return []

    def select_frame(self, level):
        pass

    def gdb(self, command, timeout=None):
        return "gdb output"

    def target_info(self):
        return TargetInfo(device="STM32G431CB", interface="SWD", speed_khz=4000, state="halted", pc=0x0800B3A4)


def make_interpreter():
    session = FakeSession()
    return session, CommandInterpreter(session)


class TestConsole(unittest.TestCase):
    def test_help(self):
        _, interp = make_interpreter()
        result = interp.execute_line("help")
        self.assertTrue(result.ok)
        self.assertIn("halt", result.text)

    def test_step_handles_symbol_annotation(self):
        _, interp = make_interpreter()
        result = interp.execute_line("step 2")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["pc"], 0x0800B3A4)

    def test_reg(self):
        _, interp = make_interpreter()
        result = interp.execute_line("reg pc")
        self.assertTrue(result.ok)
        self.assertIn("0x0800b3a4", result.text)

    def test_read32(self):
        _, interp = make_interpreter()
        result = interp.execute_line("read32 0x1000 2")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["values"], [0x03020100, 0x07060504])

    def test_write32(self):
        session, interp = make_interpreter()
        result = interp.execute_line("write32 0x2000 0xdeadbeef")
        self.assertTrue(result.ok)
        self.assertEqual(session.written[0][0], 0x2000)
        self.assertEqual(session.written[0][1], b"\xef\xbe\xad\xde")

    def test_break_and_delete(self):
        session, interp = make_interpreter()
        result = interp.execute_line("break main")
        self.assertTrue(result.ok)
        self.assertEqual(len(session.breakpoints), 1)
        result = interp.execute_line("delete 1")
        self.assertTrue(result.ok)
        self.assertEqual(session.breakpoints, [])

    def test_unknown_command(self):
        _, interp = make_interpreter()
        result = interp.execute_line("frobnicate")
        self.assertFalse(result.ok)
        self.assertIn("unknown command", result.error)

    def test_bad_integer(self):
        _, interp = make_interpreter()
        result = interp.execute_line("read32 zzz")
        self.assertFalse(result.ok)

    def test_reset_modes(self):
        session, interp = make_interpreter()
        interp.execute_line("reset run")
        interp.execute_line("reset")
        self.assertEqual(session.resets, [False, True])

    def test_go_and_wait(self):
        session, interp = make_interpreter()
        result = interp.execute_line("go")
        self.assertTrue(result.ok)
        self.assertTrue(session.running)
        result = interp.execute_line("wait")
        self.assertIn("breakpoint 1", result.text)

    def test_json_toggle(self):
        _, interp = make_interpreter()
        result = interp.execute_line("json on")
        self.assertTrue(result.data["json"])

    def test_exit(self):
        _, interp = make_interpreter()
        interp.execute_line("exit")
        self.assertTrue(interp.exit_requested)


if __name__ == "__main__":
    unittest.main()
