"""High-level, front-end agnostic session API.

This is the object a GUI (or pytest, or a Jupyter notebook) should hold::

    from focdbg import Config, DebugSession

    with DebugSession(Config.load()) as session:
        session.reset(halt=True)
        print(session.read_registers(["pc", "sp"]))
        bp = session.set_breakpoint("main")
        session.resume()
        event = session.wait_for_stop()

The CLI is implemented entirely in terms of this class.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterable

from .backend import Backend, create_backend
from .config import Config
from .models import (
    Breakpoint,
    ConnectionInfo,
    FlashResult,
    Frame,
    RegisterValue,
    StopEvent,
    TargetInfo,
    ThreadInfo,
)


class DebugSession:
    def __init__(
        self,
        config: Config | None = None,
        *,
        backend: str = "jlink-gdb",
        autoconnect: bool = False,
    ):
        self.config = config or Config.load()
        self._backend: Backend = create_backend(backend, self.config)
        self.backend_name = backend
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Callable[[Any], None]]] = {"stop": [], "output": []}
        if autoconnect:
            self.connect()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> "DebugSession":
        with self._lock:
            self._backend.open()
        return self

    def disconnect(self) -> None:
        with self._lock:
            self._backend.close()

    def __enter__(self) -> "DebugSession":
        return self.connect()

    def __exit__(self, *exc_info) -> None:
        self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._backend.is_connected

    @property
    def is_running(self) -> bool:
        return self._backend.is_running

    @property
    def connection(self) -> ConnectionInfo:
        return self._backend.connection

    @property
    def last_stop(self) -> StopEvent | None:
        return self._backend.last_stop

    # ------------------------------------------------------------------
    # events (used by the CLI today, by the GUI later)
    # ------------------------------------------------------------------
    def on_stop(self, callback: Callable[[StopEvent], None]) -> None:
        """``callback`` runs on the GDB reader thread -- keep it short."""
        self._backend.add_listener("stop", callback)

    def on_output(self, callback: Callable[[Any], None]) -> None:
        self._backend.add_listener("output", callback)

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def reset(self, halt: bool = True) -> None:
        self._backend.reset(halt=halt)

    def halt(self) -> StopEvent | None:
        return self._backend.halt()

    def resume(self) -> None:
        self._backend.resume()

    def step(self, *, into: bool = True, instruction: bool = False, count: int = 1) -> StopEvent:
        return self._backend.step(into=into, instruction=instruction, count=count)

    def wait_for_stop(self, timeout: float | None = None) -> StopEvent:
        return self._backend.wait_for_stop(timeout)

    # ------------------------------------------------------------------
    # memory / registers
    # ------------------------------------------------------------------
    def read_memory(self, address: int, length: int) -> bytes:
        return self._backend.read_memory(address, length)

    def write_memory(self, address: int, data: bytes) -> None:
        self._backend.write_memory(address, data)

    def read_registers(self, names: Iterable[str] | None = None) -> dict[str, RegisterValue]:
        return self._backend.read_registers(names)

    def write_register(self, name: str, value: int | str) -> None:
        self._backend.write_register(name, value)

    # ------------------------------------------------------------------
    # breakpoints / inspection
    # ------------------------------------------------------------------
    def set_breakpoint(
        self,
        location: str,
        *,
        hardware: bool | None = None,
        condition: str | None = None,
        temporary: bool = False,
    ) -> Breakpoint:
        return self._backend.set_breakpoint(
            location, hardware=hardware, condition=condition, temporary=temporary
        )

    def remove_breakpoint(self, number: int) -> None:
        self._backend.remove_breakpoint(number)

    def list_breakpoints(self) -> list[Breakpoint]:
        return self._backend.list_breakpoints()

    def backtrace(self, max_frames: int = 32) -> list[Frame]:
        return self._backend.backtrace(max_frames)

    def evaluate(self, expression: str) -> str:
        return self._backend.evaluate(expression)

    def resolve_symbol(self, name: str) -> int:
        return self._backend.resolve_symbol(name)

    def threads(self) -> list[ThreadInfo]:
        return self._backend.threads()

    def select_frame(self, level: int) -> None:
        self._backend.select_frame(level)

    def gdb(self, command: str, timeout: float | None = None) -> str:
        """Escape hatch: run a raw GDB CLI command."""
        return self._backend.console_command(command, timeout=timeout)

    # ------------------------------------------------------------------
    # programming
    # ------------------------------------------------------------------
    def flash(
        self,
        path: str,
        *,
        base: int | None = None,
        run: bool | None = None,
        verify: bool | None = None,
    ) -> FlashResult:
        return self._backend.flash(path, base=base, run=run, verify=verify)

    def target_info(self) -> TargetInfo:
        return self._backend.target_info()
