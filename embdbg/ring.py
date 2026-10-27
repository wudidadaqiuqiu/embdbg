"""Ring-buffer capture: drain a firmware trace ring through any probe.

Why this exists: a single sample costs one USB round trip (~0.62 ms on an
ST-Link), so host polling tops out near 1.5 kHz.  A ring buffer moves the
sampling clock into the firmware -- the control ISR appends a record at full
rate, and the host drains it in *blocks*, which run at ~57 KB/s.  At 1 kHz a
12-byte record is 12 KB/s, i.e. ~20 % of the block-read bandwidth.

The firmware side is deliberately tiny (``docs/ring.md``)::

    typedef struct { uint32_t t; int32_t iq; int32_t id; } trace_rec_t;
    static trace_rec_t       g_trace_buf[2048];
    static volatile uint32_t g_trace_head;      /* monotonically increasing */

    /* in the control ISR */
    g_trace_buf[g_trace_head & 2047] = rec;
    __DMB();                                    /* publish after the store */
    g_trace_head++;

The reader never writes and never halts the core: it reads ``head``, then
fetches ``[last, head)`` in one or two block reads (one if the span does not
wrap).  If ``head`` jumped by more than ``capacity`` since the previous poll,
the firmware overwrote records we never saw -- that is reported as ``dropped``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError

_U32 = 0xFFFFFFFF


@dataclass
class RingLayout:
    """Where the ring lives and how big one record is."""

    buffer_address: int
    head_address: int
    capacity: int
    record_size: int

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ConfigError("ring capacity must be positive (pass --capacity)")
        if self.record_size <= 0:
            raise ConfigError("ring record size must be positive (pass --fields/--record-size)")
        self.buffer_address = int(self.buffer_address)
        self.head_address = int(self.head_address)
        self.capacity = int(self.capacity)
        self.record_size = int(self.record_size)

    @property
    def buffer_bytes(self) -> int:
        return self.capacity * self.record_size

    @property
    def record_mask(self) -> int:
        return self.capacity - 1


@dataclass
class RingPoll:
    """One drain: the records read, the sequence number of the first one."""

    records: list[bytes] = field(default_factory=list)
    first_seq: int = 0
    dropped: int = 0

    @property
    def empty(self) -> bool:
        return not self.records


class RingReader:
    """Incremental consumer for a firmware ring buffer."""

    def __init__(
        self,
        probe: Any,
        layout: RingLayout,
        *,
        start_at_head: bool = True,
    ):
        self.probe = probe
        self.layout = layout
        self.start_at_head = start_at_head
        self._next: int | None = None  # absolute sequence number to read next
        self.total_read = 0
        self.total_dropped = 0
        self.polls = 0

    # ------------------------------------------------------------------
    # head helpers
    # ------------------------------------------------------------------
    def read_head(self) -> int:
        raw = self.probe.read_memory(self.layout.head_address, 4)
        if len(raw) != 4:
            raise ConfigError("could not read the ring head counter")
        return int.from_bytes(raw, "little")

    # ------------------------------------------------------------------
    def poll(self) -> RingPoll:
        """Drain everything written since the previous call."""
        self.polls += 1
        head = self.read_head()
        if self._next is None:
            if self.start_at_head:
                self._next = head
            else:
                # replay what is still in the buffer (never more than capacity)
                self._next = (head - self.layout.capacity) & _U32 if head >= self.layout.capacity else 0

        pending = (head - self._next) & _U32
        if pending == 0:
            return RingPoll()

        dropped = 0
        if pending > self.layout.capacity:
            # The writer lapped us: the oldest pending records are gone.
            dropped = pending - self.layout.capacity
            self._next = (head - self.layout.capacity) & _U32
            pending = self.layout.capacity

        data = self._read_span(self._next % self.layout.capacity, pending)
        records = [
            data[offset : offset + self.layout.record_size]
            for offset in range(0, len(data), self.layout.record_size)
        ]
        first_seq = self._next
        self._next = head
        self.total_read += len(records)
        self.total_dropped += dropped
        return RingPoll(records=records, first_seq=first_seq, dropped=dropped)

    # ------------------------------------------------------------------
    def _read_span(self, index: int, count: int) -> bytes:
        """Read ``count`` records starting at ring slot ``index`` (may wrap)."""
        layout = self.layout
        size = layout.record_size
        first = min(count, layout.capacity - index)
        data = self.probe.read_memory(layout.buffer_address + index * size, first * size)
        remaining = count - first
        if remaining > 0:
            data += self.probe.read_memory(layout.buffer_address, remaining * size)
        if len(data) != count * size:
            raise ConfigError(
                f"short read: wanted {count * size} bytes, got {len(data)}"
            )
        return data


def decode_record(data: bytes, fields) -> dict[str, Any]:
    """Decode one raw record with a list of :class:`embdbg.sampler.Field`."""
    from .sampler import decode_fields

    return decode_fields(data, fields)
