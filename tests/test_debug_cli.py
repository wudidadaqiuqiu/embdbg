"""Tests for the interactive `embdbg debug` session (no probe, no GDB).

The REPL is a thin layer over ``DebugSession``; a fake session is enough to
pin down the command surface (break / run / step / bt / locals / print / ...)
and the exit behaviour.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.cli import _console_text, _debug_exec  # noqa: E402
from embdbg.errors import GdbTimeoutError  # noqa: E402
from embdbg.models import (  # noqa: E402
    Breakpoint,
    Frame,
    RegisterValue,
    StopEvent,
    TargetInfo,
)


class FakeSession:
    backend_name = "openocd-gdb"

    def __init__(self):
        self.calls: list[tuple] = []
        self.running = False
        self.frames = [
            Frame(level=0, func="rt_pin_write", file="pin.c", line=145),
            Frame(level=1, func="main", file="main.c", line=28),
        ]
        self.breakpoints = [Breakpoint(number=1, func="rt_pin_write", file="pin.c", line=145)]

    # -- used by the REPL ----------------------------------------------
    def set_breakpoint(self, location, *, hardware=None, condition=None, temporary=False):
        self.calls.append(("set_breakpoint", location, hardware))
        return Breakpoint(number=2, func=str(location), file="main.c", line=26)

    def list_breakpoints(self):
        return list(self.breakpoints)

    def remove_breakpoint(self, number):
        self.calls.append(("remove_breakpoint", number))
        self.breakpoints = [b for b in self.breakpoints if b.number != number]

    def resume(self):
        self.calls.append(("resume",))
        self.running = True

    def wait_for_stop(self, timeout=None):
        self.calls.append(("wait_for_stop", timeout))
        self.running = False
        return StopEvent(reason="breakpoint-hit", breakpoint=1, frame=self.frames[0])

    def step(self, *, into=True, instruction=False, count=1):
        self.calls.append(("step", into, instruction, count))
        return StopEvent(reason="end-stepping-range", frame=self.frames[0])

    def halt(self):
        self.calls.append(("halt",))
        return StopEvent(reason="signal-received")

    def backtrace(self, max_frames=32):
        return self.frames[:max_frames]

    def select_frame(self, level):
        self.calls.append(("select_frame", level))

    def gdb(self, command, timeout=None):
        self.calls.append(("gdb", command))
        return f"{command}\ncount = 7\n(gdb)"

    def evaluate(self, expression):
        self.calls.append(("evaluate", expression))
        return "42"

    def read_registers(self, names=None):
        return {
            "pc": RegisterValue(name="pc", number=15, value=0x90000B04),
            "sp": RegisterValue(name="sp", number=13, value=0x24001DB0),
        }

    def target_info(self):
        return TargetInfo(state="halted", halted=True, pc=0x90000B04, voltage=3.27)


class TestDebugSession(unittest.TestCase):
    def run_cmd(self, session, line):
        buf = io.StringIO()
        with redirect_stdout(buf):
            keep = _debug_exec(session, line, timeout=5)
        return keep, buf.getvalue()

    def test_break_sets_hardware_breakpoint(self):
        session = FakeSession()
        keep, out = self.run_cmd(session, "break rt_pin_write")
        self.assertTrue(keep)
        self.assertEqual(session.calls, [("set_breakpoint", "rt_pin_write", True)])
        self.assertIn("rt_pin_write", out)

    def test_bps_and_delete(self):
        session = FakeSession()
        _, out = self.run_cmd(session, "bps")
        self.assertIn("pin.c:145", out)
        _, out = self.run_cmd(session, "delete 1")
        self.assertIn("deleted breakpoint 1", out)
        self.assertEqual(session.breakpoints, [])

    def test_run_reports_stop_and_location(self):
        session = FakeSession()
        _, out = self.run_cmd(session, "run")
        self.assertIn("breakpoint 1: rt_pin_write", out)
        self.assertIn("at rt_pin_write at pin.c:145", out)
        self.assertIn(("resume",), session.calls)

    def test_run_timeout_does_not_raise(self):
        session = FakeSession()

        def boom(timeout=None):
            raise GdbTimeoutError("nope")

        session.wait_for_stop = boom
        keep, out = self.run_cmd(session, "continue")
        self.assertTrue(keep)
        self.assertIn("still running", out)

    def test_step_and_next(self):
        session = FakeSession()
        self.run_cmd(session, "step")
        self.run_cmd(session, "next 3")
        self.assertEqual(session.calls[0], ("step", True, False, 1))
        self.assertEqual(session.calls[1], ("step", False, False, 3))

    def test_bt(self):
        session = FakeSession()
        _, out = self.run_cmd(session, "bt")
        self.assertIn("#0", out)
        self.assertIn("main at main.c:28", out)

    def test_frame_and_locals(self):
        session = FakeSession()
        self.run_cmd(session, "frame 1")
        self.assertIn(("select_frame", 1), session.calls)
        _, out = self.run_cmd(session, "locals")
        self.assertIn("count = 7", out)
        self.assertNotIn("(gdb)", out)

    def test_print_and_regs(self):
        session = FakeSession()
        _, out = self.run_cmd(session, "print rt_tick")
        self.assertIn("rt_tick = 42", out)
        _, out = self.run_cmd(session, "regs pc")
        self.assertIn("0x90000b04", out)

    def test_quit_returns_false(self):
        keep, _ = self.run_cmd(FakeSession(), "quit")
        self.assertFalse(keep)

    def test_unknown_command_is_reported(self):
        _, out = self.run_cmd(FakeSession(), "frobnicate")
        self.assertIn("unknown command", out)


class TestConsoleText(unittest.TestCase):
    def test_strips_prompt_and_blank_lines(self):
        self.assertEqual(_console_text("count = 1\n(gdb)\n\n"), "count = 1")


if __name__ == "__main__":
    unittest.main()
