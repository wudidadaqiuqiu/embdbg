"""Debug backends.

``Backend`` is the extension point: the CLI and any future GUI talk to a
``Backend`` only, never to GDB or J-Link directly.  Today there is one concrete
implementation, :class:`JLinkGdbBackend` (J-Link GDB server + arm-none-eabi-gdb),
but pyOCD/OpenOCD backends can be added without touching the front-ends.
"""

from __future__ import annotations

import abc
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import Config
from .discovery import find_gdb, find_jlink_server
from .errors import ConfigError, GdbError, ServerError, TargetError
from .gdbserver import JLinkGDBServer
from .mi import MIClient, MIRecord
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
from .util import mi_quote, parse_int


class Backend(abc.ABC):
    """Contract every debug backend implements."""

    name = "base"

    # lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def open(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abc.abstractmethod
    def is_running(self) -> bool: ...

    @property
    @abc.abstractmethod
    def connection(self) -> ConnectionInfo: ...

    @abc.abstractmethod
    def target_info(self) -> TargetInfo: ...

    # execution control -------------------------------------------------
    @abc.abstractmethod
    def reset(self, halt: bool = True) -> None: ...

    @abc.abstractmethod
    def halt(self) -> StopEvent | None: ...

    @abc.abstractmethod
    def resume(self) -> None: ...

    @abc.abstractmethod
    def step(self, *, into: bool = True, instruction: bool = False, count: int = 1) -> StopEvent: ...

    @abc.abstractmethod
    def wait_for_stop(self, timeout: float | None = None) -> StopEvent: ...

    @property
    @abc.abstractmethod
    def last_stop(self) -> StopEvent | None: ...

    # memory / registers ------------------------------------------------
    @abc.abstractmethod
    def read_memory(self, address: int, length: int) -> bytes: ...

    @abc.abstractmethod
    def write_memory(self, address: int, data: bytes) -> None: ...

    @abc.abstractmethod
    def read_registers(self, names: Iterable[str] | None = None) -> dict[str, RegisterValue]: ...

    @abc.abstractmethod
    def write_register(self, name: str, value: int | str) -> None: ...

    # breakpoints / inspection -----------------------------------------
    @abc.abstractmethod
    def set_breakpoint(
        self,
        location: str,
        *,
        hardware: bool | None = None,
        condition: str | None = None,
        temporary: bool = False,
    ) -> Breakpoint: ...

    @abc.abstractmethod
    def remove_breakpoint(self, number: int) -> None: ...

    @abc.abstractmethod
    def list_breakpoints(self) -> list[Breakpoint]: ...

    @abc.abstractmethod
    def backtrace(self, max_frames: int = 32) -> list[Frame]: ...

    @abc.abstractmethod
    def evaluate(self, expression: str) -> str: ...

    @abc.abstractmethod
    def resolve_symbol(self, name: str) -> int: ...

    @abc.abstractmethod
    def console_command(self, command: str, timeout: float | None = None) -> str: ...

    @abc.abstractmethod
    def threads(self) -> list[ThreadInfo]: ...

    @abc.abstractmethod
    def select_frame(self, level: int) -> None: ...

    # programming -------------------------------------------------------
    @abc.abstractmethod
    def flash(
        self,
        path: str,
        *,
        base: int | None = None,
        run: bool | None = None,
        verify: bool | None = None,
        progress: Callable[[dict], None] | None = None,
    ) -> FlashResult: ...

    # events ------------------------------------------------------------
    @abc.abstractmethod
    def add_listener(self, kind: str, callback: Callable[[Any], None]) -> None: ...


class JLinkGdbBackend(Backend):
    """J-Link probe -> JLinkGDBServerCL -> GDB/MI -> Python."""

    name = "jlink-gdb"

    def __init__(self, config: Config):
        self.config = config
        self.server: JLinkGDBServer | None = None
        self.mi: MIClient | None = None
        self._connection = ConnectionInfo(
            backend=self.name,
            device=config.device,
            interface=config.interface,
            speed_khz=config.speed_khz,
            elf=config.elf,
            probe_serial=config.probe_serial,
        )
        self._lock = threading.RLock()
        self._register_names: list[str] | None = None
        self._elf_path: str | None = None
        self._listeners: dict[str, list[Callable[[Any], None]]] = {
            "stop": [],
            "output": [],
            "download": [],
        }

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def open(self) -> None:
        with self._lock:
            if self.mi is not None:
                return
            cfg = self.config
            if not cfg.device:
                raise ConfigError(
                    "no target device configured; pass --device STM32G431CB or add "
                    "`device = \"STM32G431CB\"` to embdbg.toml"
                )
            gdb = find_gdb(cfg.gdb)
            server_exe = find_jlink_server(cfg.jlink_server)
            self._connection.gdb_path = str(gdb)
            self._connection.server_path = str(server_exe)

            last_error: Exception | None = None
            for _ in range(2):
                server = JLinkGDBServer(
                    server_exe,
                    device=cfg.device,
                    interface=cfg.interface,
                    speed_khz=cfg.speed_khz,
                    port=None,
                    base_port=cfg.base_port,
                    singlerun=cfg.singlerun,
                    verify=cfg.verify,
                    probe_serial=cfg.probe_serial,
                    log_file=self._log_file(),
                    extra_args=cfg.extra_server_args,
                    startup_grace=0.3,
                )
                server.start()
                self.server = server
                self._connection.server_pid = server.pid
                self._connection.server_port = server.port
                try:
                    self._connect_gdb(gdb)
                    return
                except Exception as exc:  # retry once with a fresh port
                    last_error = exc
                    self._teardown()
            raise ServerError(f"could not attach GDB to the J-Link server: {last_error}")

    def _connect_gdb(self, gdb: Path) -> None:
        assert self.server is not None
        cfg = self.config
        args: list[str] = []
        if cfg.elf:
            args.append(str(Path(cfg.elf)))
            self._elf_path = str(Path(cfg.elf))
        mi = MIClient(
            str(gdb),
            args,
            timeout=cfg.command_timeout,
            on_stop=self._emit_stop,
            on_stream=self._emit_stream,
            on_download=self._emit_download,
        )
        mi.launch()
        self.mi = mi
        self._connection.gdb_pid = mi.pid

        target = f"127.0.0.1:{self.server.port}"
        deadline = time.monotonic() + cfg.connect_timeout
        while True:
            started = time.monotonic()
            try:
                mi.execute(f"-target-select extended-remote {target}", timeout=cfg.connect_timeout)
                break
            except GdbError as exc:
                elapsed = time.monotonic() - started
                if not (self.server and self.server.is_alive()):
                    raise ServerError(
                        f"J-Link GDB server exited while attaching: {exc}",
                        detail=self.server.output_text() if self.server else None,
                    ) from exc
                # Retry only fast failures (connection refused = server not
                # listening yet). A slow failure means the target handshake is
                # stuck, and retrying would leave commands in flight.
                if elapsed > 2.0 or time.monotonic() >= deadline:
                    raise ServerError(
                        "could not attach GDB to the J-Link GDB server: "
                        + str(exc)
                        + chr(10)
                        + "hint: is the probe used by Ozone/J-Flash? power on the board? "
                        + "re-run with --server-log for the server log",
                        detail=self.server.output_text() if self.server else None,
                    ) from exc
                time.sleep(0.2)
        # A stop record may already have arrived; keep it, do not block.
        mi.drain_stops()
        # A previous session (or a crashed one) may have left hardware
        # breakpoints in the Cortex-M FPB; they survive reset and would fire
        # unexpectedly, so start from a clean slate.
        self._clear_hw_breakpoints()

    def close(self) -> None:
        with self._lock:
            mi = self.mi
            if mi is not None and not mi.is_closed and not mi.is_running:
                try:
                    self._clear_hw_breakpoints()
                except Exception:  # best effort -- never mask the disconnect
                    pass
            self._teardown()

    def _teardown(self) -> None:
        mi, self.mi = self.mi, None
        if mi is not None:
            try:
                mi.close()
            except Exception:  # pragma: no cover - best effort
                pass
        server, self.server = self.server, None
        if server is not None:
            try:
                server.stop()
            except Exception:  # pragma: no cover - best effort
                pass
        self._register_names = None
        self._elf_path = None

    def __enter__(self) -> "JLinkGdbBackend":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _clear_hw_breakpoints(self) -> None:
        """Zero the Cortex-M FPB comparators (0xE0002008 + 4*i).

        J-Link's GDB server does not reliably clear hardware breakpoints when
        it exits; the comparators live in debug registers, survive a system
        reset, and would silently stop the core at an old address.  Writing 0
        disables a comparator (bit0 is the enable bit).
        """
        try:
            self.read_memory(0xE0002000, 4)  # FP_CTRL: proves the access works
        except (GdbError, TargetError):
            return
        for index in range(8):  # covers the largest Cortex-M FPB
            try:
                self.write_memory(0xE0002008 + 4 * index, b"\x00\x00\x00\x00")
            except (GdbError, TargetError):
                break

    def _log_file(self) -> str | None:
        cfg = self.config
        if not (cfg.log_dir or cfg.keep_server_log):
            return None
        if cfg.log_dir:
            directory = Path(cfg.log_dir)
        else:
            import tempfile

            directory = Path(tempfile.mkdtemp(prefix="embdbg-"))
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory / f"jlink-gdbserver-{int(time.time())}.log")

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self.mi is not None and not self.mi.is_closed

    @property
    def is_running(self) -> bool:
        return self.mi is not None and self.mi.is_running

    @property
    def connection(self) -> ConnectionInfo:
        return self._connection

    @property
    def last_stop(self) -> StopEvent | None:
        return self.mi.last_stop if self.mi else None

    def _require_mi(self) -> MIClient:
        if self.mi is None:
            raise TargetError("not connected to a target")
        return self.mi

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def add_listener(self, kind: str, callback: Callable[[Any], None]) -> None:
        if kind not in self._listeners:
            raise ValueError(f"unknown listener kind: {kind}")
        self._listeners[kind].append(callback)

    def _emit_stop(self, event: StopEvent) -> None:
        for cb in list(self._listeners["stop"]):
            try:
                cb(event)
            except Exception:  # pragma: no cover - never break the reader
                pass

    def _emit_stream(self, record: MIRecord) -> None:
        for cb in list(self._listeners["output"]):
            try:
                cb(record)
            except Exception:  # pragma: no cover
                pass

    def _emit_download(self, payload: dict) -> None:
        for cb in list(self._listeners["download"]):
            try:
                cb(payload)
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # execution control
    # ------------------------------------------------------------------
    def reset(self, halt: bool = True) -> None:
        mi = self._require_mi()
        with self._lock:
            if mi.is_running:
                self.halt()
            mi.drain_stops()
            mi.execute_console("monitor reset")
            if not halt:
                mi.execute("-exec-continue")
            mi.drain_stops()

    def halt(self) -> StopEvent | None:
        mi = self._require_mi()
        if not mi.is_running:
            return mi.last_stop
        mi.interrupt()
        return mi.wait_for_stop(self.config.command_timeout)

    def resume(self) -> None:
        mi = self._require_mi()
        with self._lock:
            if mi.is_running:
                return
            mi.drain_stops()
            mi.execute("-exec-continue")

    def step(self, *, into: bool = True, instruction: bool = False, count: int = 1) -> StopEvent:
        mi = self._require_mi()
        command = "-exec-step-instruction" if instruction else ("-exec-step" if into else "-exec-next")
        event: StopEvent | None = None
        for _ in range(max(1, count)):
            mi.execute(command)
            event = mi.wait_for_stop(self.config.command_timeout)
        assert event is not None
        return event

    def wait_for_stop(self, timeout: float | None = None) -> StopEvent:
        return self._require_mi().wait_for_stop(timeout)

    # ------------------------------------------------------------------
    # memory / registers
    # ------------------------------------------------------------------
    def read_memory(self, address: int, length: int) -> bytes:
        mi = self._require_mi()
        address = int(address)
        if length < 0:
            raise ValueError("length must be >= 0")
        out = bytearray()
        remaining = length
        current = address
        while remaining > 0:
            chunk_size = min(remaining, 1024)
            record = mi.execute(f"-data-read-memory-bytes {current:#x} {chunk_size}")
            for chunk in record.get("memory") or []:
                begin = int(str(chunk.get("begin", current)), 0)
                offset = int(str(chunk.get("offset", "0x0")), 0)
                start = begin + offset
                contents = bytes.fromhex(str(chunk.get("contents", "")))
                rel = start - address
                if rel < 0:
                    contents = contents[-rel:]
                    rel = 0
                if rel > len(out):
                    out.extend(b"\x00" * (rel - len(out)))
                out[rel : rel + len(contents)] = contents
            current += chunk_size
            remaining -= chunk_size
        if len(out) < length:
            out.extend(b"\x00" * (length - len(out)))
        return bytes(out[:length])

    def write_memory(self, address: int, data: bytes) -> None:
        mi = self._require_mi()
        address = int(address)
        view = memoryview(bytes(data))
        offset = 0
        while offset < len(view):
            chunk = view[offset : offset + 1024]
            mi.execute(f"-data-write-memory-bytes {address + offset:#x} {chunk.hex()}")
            offset += len(chunk)

    def _register_name_list(self) -> list[str]:
        if self._register_names is None:
            record = self._require_mi().execute("-data-list-register-names")
            self._register_names = list(record.get("register-names") or [])
        return self._register_names

    def read_registers(self, names: Iterable[str] | None = None) -> dict[str, RegisterValue]:
        mi = self._require_mi()
        all_names = self._register_name_list()
        record = mi.execute("-data-list-register-values x")
        wanted = {n.lower() for n in names} if names else None
        result: dict[str, RegisterValue] = {}
        for item in record.get("register-values") or []:
            try:
                number = int(item["number"])
            except (KeyError, TypeError, ValueError):
                continue
            name = all_names[number] if number < len(all_names) else f"r{number}"
            if not name:
                continue
            if wanted is not None and name.lower() not in wanted:
                continue
            raw = item.get("value")
            try:
                value: int | str = int(str(raw), 0)
            except (TypeError, ValueError):
                value = str(raw)
            result[name] = RegisterValue(name=name, number=number, value=value)
        return result

    def write_register(self, name: str, value: int | str) -> None:
        mi = self._require_mi()
        mi.execute_console(f"set ${name} = {value}")

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
        mi = self._require_mi()
        use_hw = self.config.hw_breakpoints if hardware is None else hardware
        command = "-break-insert -f"
        if use_hw:
            command += " -h"
        if temporary:
            command += " -t"
        if condition:
            command += f" -c {mi_quote(condition)}"
        command += f" {mi_quote(str(location))}"
        record = mi.execute(command)
        bkpt = record.get("bkpt")
        if not isinstance(bkpt, dict):
            raise TargetError(f"could not set breakpoint at {location!r}")
        return _breakpoint_from_dict(bkpt)

    def remove_breakpoint(self, number: int) -> None:
        self._require_mi().execute(f"-break-delete {int(number)}")

    def list_breakpoints(self) -> list[Breakpoint]:
        record = self._require_mi().execute("-break-list")
        table = record.get("BreakpointTable") or {}
        body = table.get("body") if isinstance(table, dict) else None
        out: list[Breakpoint] = []
        for item in body or []:
            raw = item.get("bkpt") if isinstance(item, dict) else None
            if isinstance(raw, dict):
                out.append(_breakpoint_from_dict(raw))
        return out

    def backtrace(self, max_frames: int = 32) -> list[Frame]:
        record = self._require_mi().execute(f"-stack-list-frames 0 {max(1, max_frames) - 1}")
        frames: list[Frame] = []
        for item in record.get("stack") or []:
            raw = item.get("frame") if isinstance(item, dict) and "frame" in item else item
            if not isinstance(raw, dict):
                continue
            frames.append(
                Frame(
                    level=int(raw.get("level", len(frames))),
                    addr=_int(raw.get("addr")),
                    func=raw.get("func"),
                    file=raw.get("fullname") or raw.get("file"),
                    line=_int(raw.get("line")),
                    arch=raw.get("arch"),
                )
            )
        return frames

    def evaluate(self, expression: str) -> str:
        record = self._require_mi().execute(f"-data-evaluate-expression {mi_quote(expression)}")
        return str(record.get("value", ""))

    def resolve_symbol(self, name: str) -> int:
        try:
            return parse_int(name)
        except ValueError:
            pass
        text = self.evaluate(f"&({name})")
        token = text.split(" ", 1)[0]
        try:
            return int(token, 0)
        except ValueError as exc:
            raise TargetError(f"cannot resolve symbol {name!r}: {text or 'no value'}") from exc

    def console_command(self, command: str, timeout: float | None = None) -> str:
        text, _ = self._require_mi().execute_console(command, timeout=timeout)
        return text

    def threads(self) -> list[ThreadInfo]:
        record = self._require_mi().execute("-thread-info")
        out: list[ThreadInfo] = []
        for raw in record.get("threads") or []:
            if not isinstance(raw, dict):
                continue
            frame = None
            raw_frame = raw.get("frame")
            if isinstance(raw_frame, dict):
                frame = Frame(
                    level=_int(raw_frame.get("level")) or 0,
                    addr=_int(raw_frame.get("addr")),
                    func=raw_frame.get("func"),
                    file=raw_frame.get("fullname") or raw_frame.get("file"),
                    line=_int(raw_frame.get("line")),
                )
            out.append(
                ThreadInfo(
                    id=str(raw.get("id", "?")),
                    target_id=raw.get("target-id"),
                    name=raw.get("name"),
                    state=raw.get("state"),
                    frame=frame,
                )
            )
        return out

    def select_frame(self, level: int) -> None:
        self._require_mi().execute(f"-stack-select-frame {int(level)}")

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
        progress: Callable[[dict], None] | None = None,
    ) -> FlashResult:
        mi = self._require_mi()
        cfg = self.config
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            raise ConfigError(f"firmware file not found: {file_path}")
        run = cfg.run_after_flash if run is None else run
        verify = cfg.verify if verify is None else verify

        if progress is not None:
            self.add_listener("download", progress)
        try:
            if mi.is_running:
                self.halt()
            mi.drain_stops()
            started = time.monotonic()

            suffix = file_path.suffix.lower()
            if suffix == ".bin":
                if base is None:
                    raise ConfigError("--base is required when flashing a .bin file")
                mi.execute_console(
                    f'restore "{file_path.as_posix()}" binary {int(base):#x}',
                    timeout=cfg.flash_timeout,
                )
            elif suffix == ".hex":
                mi.execute_console(f'load "{file_path.as_posix()}"', timeout=cfg.flash_timeout)
            else:
                if self._elf_path != str(file_path):
                    mi.execute(f"-file-exec-and-symbols {mi_quote(str(file_path))}")
                    self._elf_path = str(file_path)
                mi.execute("-target-download", timeout=cfg.flash_timeout)

            duration = time.monotonic() - started
            verified: bool | None = None
            mismatches: list[str] = []
            if verify and suffix != ".bin":
                verified, mismatches = self._verify_sections()

            mi.execute_console("monitor reset")
            if run:
                mi.execute("-exec-continue")
            return FlashResult(
                path=str(file_path),
                duration_s=duration,
                verified=verified,
                mismatch_sections=mismatches,
                ran_after=bool(run),
            )
        finally:
            if progress is not None:
                try:
                    self._listeners["download"].remove(progress)
                except ValueError:
                    pass

    def _verify_sections(self) -> tuple[bool | None, list[str]]:
        try:
            text = self.console_command("compare-sections -r", timeout=self.config.flash_timeout)
        except GdbError:
            return None, []
        mismatches = [line.strip() for line in text.splitlines() if "MISMATCH" in line.upper()]
        return (len(mismatches) == 0), mismatches

    # ------------------------------------------------------------------
    # target info
    # ------------------------------------------------------------------
    def target_info(self) -> TargetInfo:
        mi = self._require_mi()
        info = TargetInfo(
            device=self.config.device,
            interface=self.config.interface,
            speed_khz=self.config.speed_khz,
            halted=not mi.is_running,
            state="running" if mi.is_running else "halted",
        )
        if self.server is not None:
            banner = self.server.info()
            info.voltage = banner.get("voltage")
            info.serial = banner.get("serial")
            info.firmware = banner.get("firmware")
        try:
            # GDB may append a symbol annotation, e.g. "0x800b3a4 <main>".
            info.pc = int(self.evaluate("$pc").split(" ", 1)[0], 0)
        except (GdbError, ValueError, IndexError):
            info.pc = None
        return info


# ----------------------------------------------------------------------
def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _breakpoint_from_dict(raw: dict) -> Breakpoint:
    return Breakpoint(
        number=int(raw.get("number", 0)),
        type=str(raw.get("type", "breakpoint")),
        enabled=str(raw.get("enabled", "y")).lower() in ("y", "yes", "1", "true"),
        addr=_int(raw.get("addr")),
        func=raw.get("func"),
        file=raw.get("fullname") or raw.get("file"),
        line=_int(raw.get("line")),
        condition=raw.get("cond") or raw.get("condition"),
        hit_count=int(raw.get("times", 0) or 0),
    )


def create_backend(name: str, config: Config) -> Backend:
    """Backend factory; add new names here as they are implemented."""
    if name in ("jlink", "jlink-gdb", "jlink_gdb"):
        return JLinkGdbBackend(config)
    raise ConfigError(f"unknown backend {name!r} (available: jlink-gdb)")
