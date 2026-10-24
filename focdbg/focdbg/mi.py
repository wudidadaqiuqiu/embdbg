"""Minimal, dependency-free GDB/MI client.

GDB is driven in MI2 mode over stdin/stdout.  Every command is tokenised and
matched to its result record; asynchronous records (``*stopped``, ``*running``,
``+download``) are dispatched to callbacks and queues.

Why MI and not the GDB Python API: the ``arm-none-eabi-gdb`` shipped with
STM32CubeCLT is built *without* Python support (verified on this machine), so MI
is the portable automation interface.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .errors import GdbError, GdbProtocolError, GdbTimeoutError
from .models import Frame, StopEvent
from .util import mi_quote

_RECORD_RE = re.compile(r"^(?P<token>\d*)(?P<kind>[\^*+=~@&])(?P<body>.*)$", re.S)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


@dataclass
class MIRecord:
    """One parsed GDB/MI line."""

    token: int | None
    kind: str  # 'result' | 'async' | 'stream'
    cls: str  # 'done'|'error'|'running'|'stopped'|'console'|'target'|'log'|...
    payload: Any

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.payload, dict):
            return self.payload.get(key, default)
        return default


class _ValueParser:
    """Recursive-descent parser for the MI value grammar."""

    def __init__(self, text: str):
        self.text = text
        self.i = 0

    def peek(self) -> str:
        return self.text[self.i] if self.i < len(self.text) else ""

    def skip_ws(self) -> None:
        while self.peek() in (" ", "\t", "\r", "\n"):
            self.i += 1

    def parse_cstring(self) -> str:
        self.skip_ws()
        if self.peek() != '"':
            raise GdbProtocolError(f"expected string at {self.i} in {self.text!r}")
        self.i += 1
        out: list[str] = []
        escapes = {
            "n": "\n",
            "t": "\t",
            "r": "\r",
            "f": "\f",
            "v": "\v",
            "a": "\a",
            "b": "\b",
            "e": "\x1b",
            '"': '"',
            "\\": "\\",
            "'": "'",
        }
        while True:
            ch = self.peek()
            if ch == "":
                raise GdbProtocolError("unterminated string")
            self.i += 1
            if ch == '"':
                break
            if ch == "\\":
                esc = self.peek()
                self.i += 1
                if esc in escapes:
                    out.append(escapes[esc])
                elif esc in "01234567":
                    digits = esc
                    for _ in range(2):
                        if self.peek() in "01234567":
                            digits += self.peek()
                            self.i += 1
                    out.append(chr(int(digits, 8)))
                else:
                    out.append(esc)
            else:
                out.append(ch)
        return "".join(out)

    def parse_name(self) -> str:
        self.skip_ws()
        m = _NAME_RE.match(self.text, self.i)
        if not m:
            raise GdbProtocolError(f"expected name at {self.i} in {self.text!r}")
        self.i = m.end()
        return m.group(0)

    def parse_value(self) -> Any:
        self.skip_ws()
        ch = self.peek()
        if ch == '"':
            return self.parse_cstring()
        if ch == "{":
            return self.parse_tuple()
        if ch == "[":
            return self.parse_list()
        # Tolerant fallback: MI never emits bare constants, but do not crash.
        start = self.i
        while self.peek() not in (",", "}", "]", ""):
            self.i += 1
        return self.text[start : self.i].strip()

    def parse_tuple(self) -> dict:
        self.i += 1  # '{'
        out: dict[str, Any] = {}
        while True:
            self.skip_ws()
            ch = self.peek()
            if ch == "":
                raise GdbProtocolError("unterminated tuple")
            if ch == "}":
                self.i += 1
                return out
            if ch == ",":
                self.i += 1
                continue
            name = self.parse_name()
            self.skip_ws()
            if self.peek() != "=":
                raise GdbProtocolError(f"expected '=' after {name!r}")
            self.i += 1
            out[name] = self.parse_value()

    def parse_list(self) -> list:
        self.i += 1  # '['
        out: list = []
        while True:
            self.skip_ws()
            ch = self.peek()
            if ch == "":
                raise GdbProtocolError("unterminated list")
            if ch == "]":
                self.i += 1
                return out
            if ch == ",":
                self.i += 1
                continue
            save = self.i
            try:
                name = self.parse_name()
                self.skip_ws()
                if self.peek() == "=":
                    self.i += 1
                    out.append({name: self.parse_value()})
                    continue
            except GdbProtocolError:
                pass
            self.i = save
            out.append(self.parse_value())

    def parse_results(self) -> dict:
        out: dict[str, Any] = {}
        while True:
            self.skip_ws()
            if self.peek() == "":
                return out
            if self.peek() == ",":
                self.i += 1
                continue
            name = self.parse_name()
            self.skip_ws()
            if self.peek() != "=":
                raise GdbProtocolError(f"expected '=' after {name!r}")
            self.i += 1
            out[name] = self.parse_value()


def parse_mi_record(line: str) -> MIRecord:
    """Parse one MI output line into a :class:`MIRecord`."""
    m = _RECORD_RE.match(line)
    if not m:
        raise GdbProtocolError(f"not a MI record: {line!r}")
    token = int(m.group("token")) if m.group("token") else None
    kind_ch = m.group("kind")
    body = m.group("body")
    if kind_ch in "~@&":
        text = _ValueParser(body).parse_cstring() if body.startswith('"') else body
        cls = {"~": "console", "@": "target", "&": "log"}[kind_ch]
        return MIRecord(token, "stream", cls, text)
    parser = _ValueParser(body)
    cls = parser.parse_name().strip()
    payload = parser.parse_results()
    return MIRecord(token, "result" if kind_ch == "^" else "async", cls, payload)


@dataclass
class _Pending:
    event: threading.Event
    record: MIRecord | None = None


class MIClient:
    """Thread-safe MI command channel to one GDB process."""

    def __init__(
        self,
        gdb_path: str,
        args: Iterable[str] = (),
        *,
        cwd: str | None = None,
        timeout: float = 20.0,
        on_stream: Callable[[MIRecord], None] | None = None,
        on_stop: Callable[[StopEvent], None] | None = None,
        on_download: Callable[[dict], None] | None = None,
    ):
        self.gdb_path = str(gdb_path)
        self.args = list(args)
        self.cwd = cwd
        self.timeout = timeout
        self._on_stream = on_stream
        self._on_stop = on_stop
        self._on_download = on_download

        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._console_lock = threading.RLock()
        self._pending: dict[int, _Pending] = {}
        self._token = 0
        self._closed = threading.Event()
        self._stops: queue.Queue[StopEvent] = queue.Queue()
        self._last_stop: StopEvent | None = None
        self._running = False
        self._console: deque[str] = deque(maxlen=4000)
        self._reader_error: BaseException | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def launch(self) -> None:
        argv = [self.gdb_path, "--interpreter=mi2", "-q", "-nx", *self.args]
        kwargs: dict[str, Any] = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=self.cwd,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(argv, **kwargs)
        self._reader = threading.Thread(target=self._read_loop, name="focdbg-gdb-mi", daemon=True)
        self._reader.start()
        for cmd in (
            "-gdb-set pagination off",
            "-gdb-set confirm off",
            "-gdb-set width 0",
            "-gdb-set height 0",
            "-gdb-set print elements 256",
        ):
            self.execute(cmd)
        # mi-async is required for -exec-interrupt while the target runs.
        try:
            self.execute("-gdb-set mi-async on")
        except GdbError:
            pass

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @property
    def last_stop(self) -> StopEvent | None:
        return self._last_stop

    def close(self, timeout: float = 3.0) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                self.execute("-gdb-exit", timeout=timeout)
            except (GdbError, OSError):
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._reader is not None:
            self._reader.join(timeout=timeout)
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._closed.set()

    # ------------------------------------------------------------------
    # command execution
    # ------------------------------------------------------------------
    def execute(self, command: str, timeout: float | None = None, check: bool = True) -> MIRecord:
        """Send one MI command and wait for its result record."""
        proc = self._proc
        if proc is None:
            raise GdbError("GDB has not been launched")
        if self._closed.is_set() or proc.poll() is not None:
            raise GdbError("GDB process has exited", command=command)
        timeout = self.timeout if timeout is None else timeout
        with self._lock:
            self._token += 1
            token = self._token
            pending = _Pending(threading.Event())
            self._pending[token] = pending
            try:
                self._write(f"{token}{command}")
                if not pending.event.wait(timeout):
                    raise GdbTimeoutError(
                        f"GDB did not answer {command!r} within {timeout:g}s",
                        command=command,
                    )
            finally:
                self._pending.pop(token, None)
        record = pending.record
        if record is None:  # pragma: no cover - defensive
            raise GdbError("GDB returned no record", command=command)
        if check and record.cls == "error":
            message = record.get("msg") or "unknown GDB error"
            raise GdbError(str(message), command=command)
        return record

    def execute_console(self, command: str, timeout: float | None = None, check: bool = True) -> tuple[str, MIRecord]:
        """Run a native GDB CLI command and return its console output."""
        with self._console_lock:
            self._console.clear()
            record = self.execute(
                f"-interpreter-exec console {mi_quote(command)}",
                timeout=timeout,
                check=check,
            )
            text = "".join(self._console)
        return text, record

    def interrupt(self) -> None:
        """Stop a running target (falls back to SIGINT if -exec-interrupt fails)."""
        try:
            self.execute("-exec-interrupt --all", timeout=10)
        except GdbError:
            self._write("\x03")

    def wait_for_stop(self, timeout: float | None = None) -> StopEvent:
        """Block until the next ``*stopped`` record."""
        if timeout is None:
            timeout = self.timeout
        try:
            event = self._stops.get(timeout=timeout)
        except queue.Empty:
            raise GdbTimeoutError(f"target did not stop within {timeout:g}s")
        self._last_stop = event
        return event

    def drain_stops(self) -> list[StopEvent]:
        events = []
        while True:
            try:
                events.append(self._stops.get_nowait())
            except queue.Empty:
                return events

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _write(self, text: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise GdbError("GDB stdin is not available")
        proc.stdin.write(text + "\n")
        proc.stdin.flush()

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for raw in self._proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                try:
                    record = parse_mi_record(line)
                except GdbProtocolError:
                    self._console.append(line + "\n")
                    continue
                self._dispatch(record)
        except Exception as exc:  # pragma: no cover - defensive
            self._reader_error = exc
        finally:
            self._closed.set()
            for pending in list(self._pending.values()):
                pending.record = MIRecord(None, "result", "error", {"msg": "GDB process exited"})
                pending.event.set()

    def _dispatch(self, record: MIRecord) -> None:
        if record.kind == "stream":
            if record.cls == "console":
                self._console.append(str(record.payload))
            if self._on_stream is not None:
                self._on_stream(record)
            return
        if record.kind == "async":
            if record.cls == "stopped":
                self._running = False
                event = _stop_event_from_record(record.payload if isinstance(record.payload, dict) else {})
                self._last_stop = event
                self._stops.put(event)
                if self._on_stop is not None:
                    self._on_stop(event)
            elif record.cls == "running":
                self._running = True
            elif record.cls == "download":
                if self._on_download is not None and isinstance(record.payload, dict):
                    self._on_download(record.payload)
            return
        # result record
        if record.cls == "running":
            self._running = True
        if record.token is not None:
            pending = self._pending.get(record.token)
            if pending is not None:
                pending.record = record
                pending.event.set()


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _stop_event_from_record(payload: dict) -> StopEvent:
    frame = None
    raw_frame = payload.get("frame")
    if isinstance(raw_frame, dict):
        frame = Frame(
            level=_to_int(raw_frame.get("level")) or 0,
            addr=_to_int(raw_frame.get("addr")),
            func=raw_frame.get("func"),
            file=raw_frame.get("fullname") or raw_frame.get("file"),
            line=_to_int(raw_frame.get("line")),
            arch=raw_frame.get("arch"),
        )
    return StopEvent(
        reason=str(payload.get("reason", "unknown")),
        signal=payload.get("signal-name"),
        frame=frame,
        breakpoint=_to_int(payload.get("bkptno")),
        thread_id=payload.get("thread-id"),
        raw=payload,
    )
