"""Plain data models shared by the backend, the CLI and (later) the GUI.

All models are JSON-serialisable through :func:`to_dict`, so the CLI ``--json``
mode, automation scripts and a future GUI can consume exactly the same objects.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


def to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses / lists / dicts to plain JSON types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


@dataclass
class ProbeInfo:
    index: int
    serial: str = ""
    product: str = ""
    connection: str = "USB"


@dataclass
class Frame:
    level: int
    addr: int | None = None
    func: str | None = None
    file: str | None = None
    line: int | None = None
    arch: str | None = None

    @property
    def short(self) -> str:
        """One-line GDB-style frame description."""
        loc = ""
        if self.func:
            loc = self.func
        elif self.addr is not None:
            loc = f"0x{self.addr:08x}"
        where = ""
        if self.file:
            where = self.file.replace("\\", "/").rsplit("/", 1)[-1]
            if self.line:
                where += f":{self.line}"
        if loc and where:
            return f"{loc} at {where}"
        return loc or where or "??"


@dataclass
class StopEvent:
    """Why the target stopped."""

    reason: str = "unknown"
    signal: str | None = None
    frame: Frame | None = None
    breakpoint: int | None = None
    thread_id: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        r = self.reason
        if r == "breakpoint-hit" and self.breakpoint is not None:
            if self.frame is not None:
                return f"breakpoint {self.breakpoint}: {self.frame.short}"
            return f"breakpoint {self.breakpoint}"
        if r in ("end-stepping-range", "function-finished"):
            return f"{r} at {self.frame.short}" if self.frame else r
        if r == "signal-received" and self.signal:
            return f"signal {self.signal}" + (f" at {self.frame.short}" if self.frame else "")
        if self.frame is not None:
            return f"{r} at {self.frame.short}"
        return r


@dataclass
class Breakpoint:
    number: int
    type: str = "breakpoint"
    enabled: bool = True
    addr: int | None = None
    func: str | None = None
    file: str | None = None
    line: int | None = None
    condition: str | None = None
    hit_count: int = 0

    def describe(self) -> str:
        where = self.func or (f"0x{self.addr:08x}" if self.addr is not None else "?")
        loc = ""
        if self.file:
            loc = f" ({self.file.replace(chr(92), '/').rsplit('/', 1)[-1]}:{self.line})"
        state = "" if self.enabled else " [disabled]"
        cond = f" if {self.condition}" if self.condition else ""
        return f"{self.number}\t{where}{loc}{cond}{state}"


@dataclass
class RegisterValue:
    name: str
    number: int
    value: int | str

    @property
    def hex(self) -> str:
        return f"0x{self.value:08x}" if isinstance(self.value, int) else str(self.value)


@dataclass
class TargetInfo:
    device: str | None = None
    interface: str | None = None
    speed_khz: int | None = None
    voltage: float | None = None
    serial: str | None = None
    firmware: str | None = None
    halted: bool | None = None
    pc: int | None = None
    state: str | None = None


@dataclass
class ConnectionInfo:
    backend: str = "jlink-gdb"
    server_path: str | None = None
    server_pid: int | None = None
    server_port: int | None = None
    gdb_path: str | None = None
    gdb_pid: int | None = None
    device: str | None = None
    interface: str | None = None
    speed_khz: int | None = None
    elf: str | None = None
    probe_serial: str | None = None

    @property
    def target_spec(self) -> str:
        return f"127.0.0.1:{self.server_port}"


@dataclass
class FlashResult:
    path: str
    duration_s: float = 0.0
    bytes_downloaded: int | None = None
    verified: bool | None = None
    mismatch_sections: list[str] = field(default_factory=list)
    ran_after: bool = False


@dataclass
class ThreadInfo:
    id: str
    target_id: str | None = None
    name: str | None = None
    state: str | None = None
    frame: Frame | None = None


@dataclass
class CommandResult:
    """Result of one console command (used by CLI and GUI)."""

    ok: bool = True
    text: str = ""
    data: Any = None
    error: str | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"ok": self.ok}
        if self.data is not None:
            out["data"] = to_dict(self.data)
        if self.text:
            out["text"] = self.text
        if self.error:
            out["error"] = self.error
        return out
