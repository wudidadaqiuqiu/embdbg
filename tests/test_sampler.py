"""Sampler tests: field layouts, decoding and the sampling loop (no hardware)."""

from __future__ import annotations

import json
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.errors import ConfigError  # noqa: E402
from embdbg.sampler import (  # noqa: E402
    Sample,
    Sampler,
    csv_header,
    decode_fields,
    fields_size,
    format_sample,
    parse_fields,
)


class FakeTarget:
    def __init__(self):
        self.is_running = True
        self.halts = 0
        self.resumes = 0
        self.reads = 0
        self.data = struct.pack("<iif", 1, 2, 3.5)

    def halt(self):
        self.halts += 1
        self.is_running = False
        return None

    def resume(self):
        self.resumes += 1
        self.is_running = True

    def read_memory(self, address, length):
        self.reads += 1
        return self.data[:length]


class TestFieldLayout(unittest.TestCase):
    def test_sequential_offsets(self):
        fields = parse_fields("iq:i32,id:i32,ud:f32")
        self.assertEqual([f.offset for f in fields], [0, 4, 8])
        self.assertEqual(fields_size(fields), 12)

    def test_explicit_offsets(self):
        fields = parse_fields("a:u8@0,b:u32@4")
        self.assertEqual(fields[0].offset, 0)
        self.assertEqual(fields[1].offset, 4)
        self.assertEqual(fields_size(fields), 8)

    def test_mixed_offsets(self):
        fields = parse_fields("a:u8,b:u16@8,c:u8")
        self.assertEqual([f.offset for f in fields], [0, 8, 10])

    def test_invalid_specs(self):
        for bad in ("", "   ", "iq", "iq:bad", "iq:i32,iq:i32", "iq:u8@x"):
            with self.assertRaises(ConfigError):
                parse_fields(bad)

    def test_decode(self):
        fields = parse_fields("iq:i32,id:i32,ud:f32")
        values = decode_fields(struct.pack("<iif", -7, 9, 0.25), fields)
        self.assertEqual(values, {"iq": -7, "id": 9, "ud": 0.25})

    def test_decode_short_buffer(self):
        fields = parse_fields("a:u32")
        with self.assertRaises(ConfigError):
            decode_fields(b"\x00\x01", fields)


class TestSampler(unittest.TestCase):
    def test_halts_around_each_sample(self):
        target = FakeTarget()
        samples = []
        sampler = Sampler(
            target,
            0x20000000,
            12,
            parse_fields("iq:i32,id:i32,ud:f32"),
            rate_hz=1000,
            count=5,
        )
        stats = sampler.run(samples.append)
        self.assertEqual(stats.samples, 5)
        self.assertEqual(len(samples), 5)
        self.assertEqual(target.halts, 5)
        self.assertEqual(target.resumes, 5)
        self.assertTrue(target.is_running)
        self.assertEqual(samples[0].values, {"iq": 1, "id": 2, "ud": 3.5})

    def test_already_halted_target_is_not_resumed(self):
        target = FakeTarget()
        target.is_running = False
        sampler = Sampler(target, 0x20000000, 12, count=3)
        sampler.run(lambda sample: None)
        self.assertEqual(target.halts, 0)
        self.assertEqual(target.resumes, 0)

    def test_no_halt_mode(self):
        target = FakeTarget()
        sampler = Sampler(target, 0x20000000, 12, halt=False, count=3)
        sampler.run(lambda sample: None)
        self.assertEqual(target.halts, 0)
        self.assertEqual(target.reads, 3)

    def test_sees_updated_values(self):
        target = FakeTarget()
        seen = []

        def on_sample(sample):
            seen.append(sample.values["iq"])
            target.data = struct.pack("<iif", sample.values["iq"] + 1, 0, 0.0)

        sampler = Sampler(target, 0x20000000, 12, parse_fields("iq:i32"), halt=False, count=4)
        sampler.run(on_sample)
        self.assertEqual(seen, [1, 2, 3, 4])

    def test_requested_rate_is_honoured(self):
        target = FakeTarget()
        sampler = Sampler(target, 0x20000000, 4, rate_hz=200, count=20, halt=False)
        stats = sampler.run(lambda sample: None)
        self.assertGreater(stats.rate_hz, 150)
        self.assertLess(stats.rate_hz, 260)

    def test_stop_event(self):
        import threading

        target = FakeTarget()
        stop = threading.Event()
        stop.set()
        sampler = Sampler(target, 0x20000000, 4, stop_event=stop)
        stats = sampler.run(lambda sample: None)
        self.assertEqual(stats.samples, 0)


class TestFormatting(unittest.TestCase):
    def setUp(self):
        self.fields = parse_fields("iq:i32,ud:f32")
        self.sample = Sample(
            index=0,
            t=0.5,
            address=0x20000000,
            data=struct.pack("<if", -3, 1.25),
            values={"iq": -3, "ud": 1.25},
        )

    def test_csv_header(self):
        self.assertEqual(csv_header(self.fields), "t_s,raw,iq,ud")

    def test_csv_row(self):
        line = format_sample(self.sample, self.fields, "csv")
        self.assertEqual(line, "0.500000,fdffffff0000a03f,-3,1.25")

    def test_jsonl_row(self):
        payload = json.loads(format_sample(self.sample, self.fields, "jsonl"))
        self.assertEqual(payload["i"], 0)
        self.assertEqual(payload["iq"], -3)
        self.assertEqual(payload["ud"], 1.25)
        self.assertEqual(payload["raw"], self.sample.data.hex())


if __name__ == "__main__":
    unittest.main()
