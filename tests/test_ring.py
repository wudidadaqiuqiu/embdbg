"""Ring-buffer reader tests: no probe, no hardware.

The reader is the host half of the "ring buffer + block read" sampling path;
its job is to turn a monotonically increasing head counter plus a circular
array into an ordered record stream, and to notice when the writer lapped it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.ring import RingLayout, RingReader  # noqa: E402


class FakeRing:
    """A firmware ring: ``capacity`` records plus a u32 head counter."""

    def __init__(self, capacity: int, record_size: int, base: int = 0x24000000):
        self.capacity = capacity
        self.record_size = record_size
        self.base = base
        self.head_address = base + capacity * record_size
        self.data = bytearray(capacity * record_size)
        self.head = 0
        self.reads: list[tuple[int, int]] = []

    def push(self, payload: bytes) -> None:
        assert len(payload) == self.record_size
        slot = self.head % self.capacity
        start = slot * self.record_size
        self.data[start : start + self.record_size] = payload
        self.head += 1

    # -- the probe surface the reader uses ------------------------------
    def read_memory(self, address: int, length: int) -> bytes:
        self.reads.append((address, length))
        if address == self.head_address:
            return (self.head & 0xFFFFFFFF).to_bytes(4, "little")
        offset = address - self.base
        if offset < 0 or offset + length > len(self.data):
            raise AssertionError(f"read out of range: {address:#x}+{length}")
        return bytes(self.data[offset : offset + length])


def reader_for(ring: FakeRing, *, start_at_head: bool = True) -> RingReader:
    layout = RingLayout(
        buffer_address=ring.base,
        head_address=ring.head_address,
        capacity=ring.capacity,
        record_size=ring.record_size,
    )
    return RingReader(ring, layout, start_at_head=start_at_head)


class TestRingReader(unittest.TestCase):
    def test_only_new_records_by_default(self):
        ring = FakeRing(8, 4)
        for i in range(3):
            ring.push(bytes([i, 0, 0, 0]))

        reader = reader_for(ring)
        self.assertTrue(reader.poll().empty)          # start at the current head

        ring.push(bytes([9, 0, 0, 0]))
        poll = reader.poll()
        self.assertEqual(len(poll.records), 1)
        self.assertEqual(poll.records[0], bytes([9, 0, 0, 0]))

    def test_reads_a_contiguous_block(self):
        ring = FakeRing(16, 4)
        for i in range(6):
            ring.push(bytes([i, i, i, i]))

        reader = reader_for(ring, start_at_head=False)
        poll = reader.poll()
        self.assertEqual(len(poll.records), 6)
        self.assertEqual(poll.first_seq, 0)
        self.assertEqual(poll.records[5], bytes([5, 5, 5, 5]))
        # one head read + one block read, not six
        self.assertEqual(len(ring.reads), 2)

    def test_wrap_is_stitched_from_two_reads(self):
        ring = FakeRing(8, 4)
        for i in range(10):                            # wraps once
            ring.push(bytes([i, 0, 0, 0]))

        reader = reader_for(ring, start_at_head=False)
        poll = reader.poll()
        self.assertEqual(len(poll.records), 8)         # capacity limits the replay
        self.assertEqual(poll.first_seq, 2)
        self.assertEqual([r[0] for r in poll.records], list(range(2, 10)))
        # head + two block reads (tail of the buffer, then the wrapped head)
        self.assertEqual(len(ring.reads), 3)

    def test_overrun_is_reported(self):
        ring = FakeRing(8, 4)
        reader = reader_for(ring)
        reader.poll()                                  # establish the baseline

        for i in range(30):                            # writer laps the reader
            ring.push(bytes([i & 0xFF, 0, 0, 0]))

        poll = reader.poll()
        self.assertEqual(poll.dropped, 30 - 8)
        self.assertEqual(len(poll.records), 8)
        self.assertEqual([r[0] for r in poll.records], list(range(22, 30)))
        self.assertEqual(reader.total_dropped, 22)

    def test_sequence_continues_across_polls(self):
        ring = FakeRing(16, 4)
        reader = reader_for(ring)

        for i in range(5):
            ring.push(bytes([i, 0, 0, 0]))
        first = reader.poll()
        for i in range(5, 12):
            ring.push(bytes([i, 0, 0, 0]))
        second = reader.poll()

        self.assertEqual(first.first_seq, 0)
        self.assertEqual(second.first_seq, 5)
        self.assertEqual([r[0] for r in second.records], list(range(5, 12)))

    def test_head_wraparound(self):
        ring = FakeRing(4, 2)
        ring.head = 0xFFFFFFFE
        reader = reader_for(ring)
        reader.poll()                                   # baseline at 0xFFFFFFFE
        ring.push(b"\x01\x00")                          # head -> 0xFFFFFFFF
        ring.push(b"\x02\x00")                          # head -> 0x00000000 (wrap)
        poll = reader.poll()
        self.assertEqual(len(poll.records), 2)
        self.assertEqual(poll.records[0], b"\x01\x00")
        self.assertEqual(poll.records[1], b"\x02\x00")

    def test_layout_validation(self):
        with self.assertRaises(Exception):
            RingLayout(buffer_address=0, head_address=4, capacity=0, record_size=4)
        with self.assertRaises(Exception):
            RingLayout(buffer_address=0, head_address=4, capacity=8, record_size=0)


if __name__ == "__main__":
    unittest.main()
