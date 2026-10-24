"""GDB/MI protocol parser tests (no hardware, no GDB)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.mi import parse_mi_record  # noqa: E402


class TestMIParser(unittest.TestCase):
    def test_done_with_value(self):
        record = parse_mi_record('1^done,value="0x800b3a4 <main>"')
        self.assertEqual(record.token, 1)
        self.assertEqual(record.kind, "result")
        self.assertEqual(record.cls, "done")
        self.assertEqual(record.get("value"), "0x800b3a4 <main>")

    def test_untokenised_done(self):
        record = parse_mi_record("^done")
        self.assertIsNone(record.token)
        self.assertEqual(record.cls, "done")
        self.assertEqual(record.payload, {})

    def test_stdout_stream(self):
        record = parse_mi_record('~"hello\\nworld"')
        self.assertEqual(record.kind, "stream")
        self.assertEqual(record.cls, "console")
        self.assertEqual(record.payload, "hello\nworld")

    def test_stopped_with_frame(self):
        line = (
            '*stopped,reason="breakpoint-hit",bkptno="1",thread-id="1",'
            'frame={addr="0x0800b3a8",func="main",args=[],file="main.c",'
            'fullname="C:\\\\x\\\\main.c",line="114",arch="armv7e-m"}'
        )
        record = parse_mi_record(line)
        self.assertEqual(record.kind, "async")
        self.assertEqual(record.cls, "stopped")
        frame = record.get("frame")
        self.assertEqual(frame["func"], "main")
        self.assertEqual(frame["line"], "114")
        self.assertEqual(frame["fullname"], "C:\\x\\main.c")
        self.assertEqual(frame["args"], [])

    def test_memory_list(self):
        line = (
            '^done,memory=[{begin="0x20000000",offset="0x0",contents="00010203"},'
            '{begin="0x20000010",offset="0x0",contents="ff"}]'
        )
        record = parse_mi_record(line)
        memory = record.get("memory")
        self.assertEqual(len(memory), 2)
        self.assertEqual(memory[0]["contents"], "00010203")

    def test_breakpoint_table(self):
        line = (
            '^done,BreakpointTable={nr_rows="1",nr_cols="6",hdr=[],'
            'body=[bkpt={number="1",type="breakpoint",enabled="y",'
            'addr="0x0800b3a8",func="main",file="main.c",line="114",times="0"}]}'
        )
        record = parse_mi_record(line)
        table = record.get("BreakpointTable")
        self.assertEqual(table["nr_rows"], "1")
        body = table["body"]
        self.assertEqual(body[0]["bkpt"]["func"], "main")

    def test_error_with_quotes(self):
        record = parse_mi_record('2^error,msg="No symbol \\"foo\\" in current context."')
        self.assertEqual(record.cls, "error")
        self.assertIn('"foo"', record.get("msg"))

    def test_async_class(self):
        record = parse_mi_record('=thread-group-added,id="i1"')
        self.assertEqual(record.kind, "async")
        self.assertEqual(record.cls, "thread-group-added")
        self.assertEqual(record.get("id"), "i1")

    def test_comma_inside_string(self):
        record = parse_mi_record('^done,value="{a=1, b=2}"')
        self.assertEqual(record.get("value"), "{a=1, b=2}")

    def test_register_values(self):
        line = '^done,register-values=[{number="0",value="0x1"},{number="15",value="0x800b3a4"}]'
        record = parse_mi_record(line)
        values = record.get("register-values")
        self.assertEqual(len(values), 2)
        self.assertEqual(values[1]["value"], "0x800b3a4")

    def test_rejects_garbage(self):
        from embdbg.errors import GdbProtocolError

        with self.assertRaises(GdbProtocolError):
            parse_mi_record("not mi at all")


if __name__ == "__main__":
    unittest.main()
