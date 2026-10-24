"""Non-intrusive observation helpers: periodic memory sampling and buffer dumps.

Why memory and not GDB watchpoints: breakpoints/watchpoints stop the core, and
stopping a FOC current loop even for a few hundred microseconds is disruptive.
The robust pattern for motor control is:

    MCU: ISR writes samples into a RAM ring buffer (no debugger involvement)
    host: reads the buffer over SWD at low rate

This module implements the host half: decoding of a binary record layout and a
rate-limited sampler that can run with or without halting the target.
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from .errors import ConfigError

_TYPE_INFO: dict[str, tuple[str, int]] = {
    "u8": ("<B", 1),
    "i8": ("<b", 1),
    "u16": ("<H", 2),
    "i16": ("<h", 2),
    "u32": ("<I", 4),
    "i32": ("<i", 4),
    "u64": ("<Q", 8),
    "i64": ("<q", 8),
    "f32": ("<f", 4),
    "f64": ("<d", 8),
}

@dataclass
class Field:
    name: str
    type: str
    offset: int

    @property
    def size(self) -> int:
        return _TYPE_INFO[self.type][1]

    @property
    def format(self) -> str:
        return _TYPE_INFO[self.type][0]

    def decode(self, data: bytes) -> float | int:
        (value,) = struct.unpack_from(self.format, data, self.offset)
        return value


def parse_fields(spec: str) -> list[Field]:
    """Parse ``"iq:i32,id:i32,ud:f32"`` or ``"iq:i32@0,ud:f32@8"``.

    Fields without an explicit ``@offset`` are packed sequentially after the
    previous field, so the common case stays terse.
    """
    if not spec or not spec.strip():
        raise ConfigError("empty field specification")
    fields: list[Field] = []
    cursor = 0
    seen: set[str] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        offset: int | None = None
        if "@" in part:
            part, raw_offset = part.rsplit("@", 1)
            try:
                offset = int(raw_offset, 0)
            except ValueError as exc:
                raise ConfigError(f"invalid offset {raw_offset!r} in field {part!r}") from exc
        name, sep, type_name = part.partition(":")
        name = name.strip()
        type_name = type_name.strip().lower()
        if not sep or not name or not type_name:
            raise ConfigError(f"invalid field {part!r}; expected name:type[@offset]")
        if type_name not in _TYPE_INFO:
            raise ConfigError(
                f"unknown type {type_name!r} for field {name!r} (known: {', '.join(sorted(_TYPE_INFO))})"
            )
        if name in seen:
            raise ConfigError(f"duplicate field name {name!r}")
        seen.add(name)
        if offset is None:
            offset = cursor
        fields.append(Field(name=name, type=type_name, offset=offset))
        cursor = offset + _TYPE_INFO[type_name][1]
    if not fields:
        raise ConfigError("empty field specification")
    return fields


def fields_size(fields: Iterable[Field]) -> int:
    fields = list(fields)
    return max(f.offset + f.size for f in fields)


def decode_fields(data: bytes, fields: Iterable[Field]) -> dict[str, float | int]:
    values: dict[str, float | int] = {}
    for field in fields:
        if field.offset + field.size > len(data):
            raise ConfigError(
                f"field {field.name!r} needs {field.offset + field.size} bytes, only {len(data)} available"
            )
        values[field.name] = field.decode(data)
    return values


@dataclass
class Sample:
    index: int
    t: float
    address: int
    data: bytes
    values: dict[str, float | int] = field(default_factory=dict)


@dataclass
class SampleStats:
    samples: int = 0
    elapsed_s: float = 0.0
    overruns: int = 0

    @property
    def rate_hz(self) -> float:
        return self.samples / self.elapsed_s if self.elapsed_s > 0 else 0.0


class SampleTarget(Protocol):
    """Subset of DebugSession the sampler needs (keeps tests hardware-free)."""

    @property
    def is_running(self) -> bool: ...

    def halt(self): ...

    def resume(self) -> None: ...

    def read_memory(self, address: int, length: int) -> bytes: ...


class Sampler:
    def __init__(
        self,
        target: SampleTarget,
        address: int,
        size: int,
        fields: list[Field] | None = None,
        *,
        rate_hz: float | None = None,
        duration: float | None = None,
        count: int | None = None,
        halt: bool = True,
        stop_event: threading.Event | None = None,
    ):
        if size <= 0:
            raise ValueError("size must be positive")
        if rate_hz is not None and rate_hz <= 0:
            raise ValueError("rate must be positive")
        self.target = target
        self.address = int(address)
        self.size = int(size)
        self.fields = list(fields or [])
        self.rate_hz = rate_hz
        self.duration = duration
        self.count = count
        self.halt = halt
        self.stop_event = stop_event or threading.Event()
        self.stats = SampleStats()

    def run(self, callback: Callable[[Sample], None]) -> SampleStats:
        """Sample until count/duration/stop_event.  Ctrl-C stops gracefully."""
        target = self.target
        period = (1.0 / self.rate_hz) if self.rate_hz else 0.0
        start = time.perf_counter()
        next_deadline = start
        index = 0
        try:
            while not self.stop_event.is_set():
                if self.duration is not None and time.perf_counter() - start >= self.duration:
                    break
                if self.count is not None and index >= self.count:
                    break
                now = time.perf_counter()
                if now < next_deadline:
                    if not self._wait_until(next_deadline):
                        break
                    continue
                halted_by_us = False
                try:
                    # Only resume a core we halted ourselves: sampling must not
                    # silently continue past a breakpoint the user is inspecting.
                    if self.halt and target.is_running:
                        target.halt()
                        halted_by_us = True
                    data = target.read_memory(self.address, self.size)
                finally:
                    if halted_by_us:
                        target.resume()
                values = decode_fields(data, self.fields) if self.fields else {}
                callback(Sample(index=index, t=time.perf_counter() - start, address=self.address, data=data, values=values))
                index += 1
                if period:
                    next_deadline += period
                    if next_deadline < time.perf_counter():
                        # We could not keep up; resynchronise instead of bursting.
                        self.stats.overruns += 1
                        next_deadline = time.perf_counter()
                else:
                    next_deadline = time.perf_counter()
        except KeyboardInterrupt:
            pass
        self.stats.samples = index
        self.stats.elapsed_s = max(time.perf_counter() - start, 1e-9)
        return self.stats

    def _wait_until(self, deadline: float) -> bool:
        """Sleep until *deadline* (monotonic seconds), interruptible by stop_event.

        Two Windows traps are avoided here:
          * ``threading.Event.wait()`` has ~14.5 ms granularity;
          * ``time.monotonic()`` has ~15.6 ms granularity (GetTickCount64).
        Pacing therefore uses ``time.perf_counter()`` (QPC) plus ``time.sleep``
        for the bulk of the wait and a short spin for the last millisecond.
        """
        while not self.stop_event.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return True
            if remaining > 0.0008:
                # time.sleep overshoots by ~0.4-0.5 ms on Windows; leave that
                # much for the spin so we do not burn a core for the whole wait.
                time.sleep(remaining - 0.0005)
            else:
                while time.perf_counter() < deadline:
                    if self.stop_event.is_set():
                        return False
                return True
        return False

    def run_readonly(self, callback: Callable[[Sample], None], duration: float | None = None) -> SampleStats:
        """Sample without halting; requires the backend to support running reads."""
        self.halt = False
        self.duration = duration
        return self.run(callback)


# ----------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------
def csv_header(fields: list[Field]) -> str:
    names = ["t_s", "raw"] + [f.name for f in fields]
    return ",".join(names)


def _format_value(value: float | int) -> str:
    if isinstance(value, float):
        return f"{value:.9g}"
    return str(value)


def format_sample(sample: Sample, fields: list[Field], fmt: str = "csv") -> str:
    if fmt == "jsonl":
        import json

        return json.dumps(
            {
                "i": sample.index,
                "t": round(sample.t, 6),
                "raw": sample.data.hex(),
                **{name: (round(v, 9) if isinstance(v, float) else v) for name, v in sample.values.items()},
            },
            ensure_ascii=False,
        )
    values = [_format_value(sample.values[f.name]) for f in fields]
    return ",".join([f"{sample.t:.6f}", sample.data.hex(), *values])
