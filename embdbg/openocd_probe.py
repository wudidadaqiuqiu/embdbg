"""OpenOCD probe access over the TCL RPC port.

This is the non-J-Link counterpart of :class:`embdbg.jlink_probe.JLinkProbe`.
It mirrors the small part of the JLinkProbe surface the CLI needs
(``open/close/read_memory/write_memory/halt/go/reset/flash_file/info``) and
talks to ``openocd`` over its TCL port instead of ``JLinkARM.dll``.

Why TCL and not the GDB server: OpenOCD's TCL commands run on the target's
AHB-AP, so reads and writes while the core is running are non-intrusive -- the
same "live watch" property the J-Link DLL provides (and the reason the CLI's
``read``/``write``/``hss`` do not halt the target).  The GDB server cannot do
that: GDB only accesses memory while the target is stopped.

Wire protocol: every command is sent as ASCII followed by ``0x1a``; the reply
is everything up to the next ``0x1a``.  Errors come back as plain text (no
marker), so replies are validated by their expected shape.
"""

from __future__ import annotations

import os
import re
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .errors import ConfigError, ServerError, TargetError
from .util import find_free_ports

_END = b"\x1a"
_HEX_TOKEN = re.compile(r"^0x[0-9a-fA-F]+$")
#: flash/verify results: the command's own result line, or a failure line
_WROTE_RE = re.compile(r"(wrote\s|error:|failed|no flash bank)", re.I)
_VERIFIED_RE = re.compile(r"(verified\s|error:|failed|mismatch)", re.I)
_TARGET_LINE = re.compile(
    r"^\s*(?P<index>\d+)(?P<current>\*)?\s+(?P<name>\S+)\s+.*\s(?P<state>running|halted)\s*$"
)

#: bytes per ``read_memory``/``write_memory`` round trip
_MAX_CHUNK = 4096


class OpenOcdProbe:
    """One OpenOCD instance, spoken to over TCL.

    ``open()`` attaches to ``tcl_port`` when that port already answers;
    otherwise it starts a private ``openocd`` (TCL only, no GDB/telnet ports)
    and owns it until :meth:`close`.
    """

    def __init__(
        self,
        executable: str | os.PathLike,
        config_path: str | os.PathLike,
        *,
        tcl_port: int = 0,
        extra_args: list[str] | None = None,
        startup_timeout: float = 15.0,
        startup_delay: float = 0.6,
        log_file: str | None = None,
    ):
        self.executable = str(executable)
        self.config_path = str(config_path)
        self.requested_port = int(tcl_port)
        self.extra_args = list(extra_args or [])
        self.startup_timeout = float(startup_timeout)
        #: how long the firmware gets to run (and configure QSPI) before a flash
        self.startup_delay = float(startup_delay)
        self.log_file = log_file

        self.port: int | None = None
        self.process: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._spawned = False
        self._log_path: str | None = None
        self._temps: list[str] = []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def open(self) -> "OpenOcdProbe":
        if self._sock is not None:
            return self
        if not Path(self.config_path).is_file():
            raise ConfigError(f"OpenOCD config not found: {self.config_path}")

        if self.requested_port and _port_open(self.requested_port):
            self.port = self.requested_port
            self._spawned = False
            self._connect()
            return self

        self._spawn()
        return self

    def _spawn(self) -> None:
        port = self.requested_port or find_free_ports(6666)
        self.port = port
        # `disabled` is the documented way to switch a server off; 0 would
        # merely pick a free port (OpenOCD behaviour, verified on 0.12).
        commands = f"tcl port {port}; telnet port disabled; gdb port disabled"
        argv = [self.executable, "-f", self.config_path, "-c", commands, *self.extra_args]

        if self.log_file:
            self._log_path = self.log_file
            Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)
            log = open(self._log_path, "w", encoding="utf-8", errors="replace")
        else:
            fd, self._log_path = tempfile.mkstemp(prefix="embdbg-openocd-", suffix=".log")
            log = os.fdopen(fd, "w", encoding="utf-8", errors="replace")

        try:
            self.process = subprocess.Popen(
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as exc:
            log.close()
            raise ServerError(f"could not start openocd ({self.executable}): {exc}") from exc
        finally:
            try:
                log.close()
            except Exception:  # pragma: no cover - the child owns the handle now
                pass

        self._spawned = True
        self._wait_for_port()

    def _wait_for_port(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise ServerError(
                    f"openocd exited with code {self.process.returncode} while starting",
                    detail=self.output_text(),
                )
            try:
                self._connect()
                return
            except (OSError, ServerError):
                time.sleep(0.15)
        raise ServerError(
            f"openocd did not open its TCL port {self.port} within {self.startup_timeout:.0f}s",
            detail=self.output_text(),
        )

    def _connect(self) -> None:
        assert self.port is not None
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5.0)
        sock.settimeout(30.0)
        self._sock = sock

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            if self._spawned:
                try:
                    sock.sendall(b"shutdown" + _END)
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass
        if self._spawned and self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    self.process.kill()
        self.process = None
        self._spawned = False

    def __enter__(self) -> "OpenOcdProbe":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def output_text(self) -> str:
        if not self._log_path:
            return ""
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()[-4000:]
        except OSError:  # pragma: no cover
            return ""

    # ------------------------------------------------------------------
    # TCL plumbing
    # ------------------------------------------------------------------
    def _require(self) -> socket.socket:
        if self._sock is None:
            raise TargetError("OpenOCD probe is not open")
        return self._sock

    def command(self, text: str, *, timeout: float | None = None) -> str:
        """Send one TCL command and return the reply as text."""
        return self._stream(text, timeout=timeout, stop=None)

    def _stream(
        self,
        text: str,
        *,
        timeout: float | None = None,
        stop: re.Pattern[str] | None = None,
    ) -> str:
        """Send a command and read replies.

        OpenOCD sends one ``0x1a``-terminated message per *log line* and one for
        the command result, so a single command such as ``flash write_image``
        produces several messages (``auto erase enabled`` then ``wrote ...``).
        With ``stop`` given, keep reading until a message matches -- the match
        (or an error line) is the command's result.
        """
        sock = self._require()
        if timeout is not None:
            sock.settimeout(timeout)
        messages: list[str] = []
        buffer = bytearray()
        try:
            sock.sendall(text.encode("utf-8") + _END)
            matched = False
            while True:
                data = sock.recv(65536)
                if not data:
                    raise ServerError("OpenOCD closed the TCL connection", detail=self.output_text())
                buffer += data
                while _END in buffer:
                    index = buffer.index(_END)
                    message = bytes(buffer[:index]).decode("utf-8", errors="replace")
                    del buffer[: index + 1]
                    messages.append(message)
                    if stop is None:
                        return message
                    if not message and not matched:
                        # an empty message is the command result for commands
                        # without output -- nothing more is coming
                        return "\n".join(messages)
                    if stop.search(message):
                        matched = True
                if matched and not buffer:
                    # give trailing log lines a chance to arrive, then stop
                    sock.settimeout(0.05)
        except socket.timeout as exc:
            if stop is not None and messages:
                return "\n".join(messages)
            raise ServerError(f"OpenOCD did not answer {text!r} in time") from exc
        except OSError as exc:
            raise ServerError(f"OpenOCD connection failed during {text!r}: {exc}") from exc
        finally:
            try:
                sock.settimeout(30.0)
            except OSError:  # pragma: no cover
                pass
        return "\n".join(messages)

    # ------------------------------------------------------------------
    # execution control
    # ------------------------------------------------------------------
    def halt(self) -> None:
        self.command("halt")

    def go(self) -> None:
        self.command("resume")

    def reset(self, halt: bool = True) -> None:
        self.command("reset halt" if halt else "reset run")

    @property
    def is_running(self) -> bool:
        return self._state() == "running"

    @property
    def halted(self) -> bool:
        return not self.is_running

    def _state(self) -> str:
        text = self.command("targets")
        for line in text.splitlines():
            match = _TARGET_LINE.match(line)
            if match and match.group("current"):
                return match.group("state")
        return "unknown"

    def wait_halted(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else 5.0)
        while time.monotonic() < deadline:
            if self.halted:
                return True
            time.sleep(0.02)
        return self.halted

    # ------------------------------------------------------------------
    # memory
    # ------------------------------------------------------------------
    def read_memory(self, address: int, length: int) -> bytes:
        address = int(address)
        if length < 0:
            raise ValueError("length must be >= 0")
        if length == 0:
            return b""
        out = bytearray()
        current, remaining = address, length
        while remaining > 0:
            if current % 4 == 0 and remaining >= 4:
                count = min(remaining // 4, _MAX_CHUNK // 4)
                text = self.command(f"read_memory {current:#x} 32 {count}")
                tokens = text.split()
                if len(tokens) != count or not all(_HEX_TOKEN.match(t) for t in tokens):
                    raise TargetError(
                        f"OpenOCD could not read {count} word(s) at {current:#x}: {text.strip()}"
                    )
                for token in tokens:
                    out += struct.pack("<I", int(token, 16))
                step = count * 4
            else:
                count = min(remaining, _MAX_CHUNK)
                text = self.command(f"read_memory {current:#x} 8 {count}")
                tokens = text.split()
                if len(tokens) != count or not all(_HEX_TOKEN.match(t) for t in tokens):
                    raise TargetError(
                        f"OpenOCD could not read {count} byte(s) at {current:#x}: {text.strip()}"
                    )
                out += bytes(int(token, 16) for token in tokens)
                step = count
            current += step
            remaining -= step
        return bytes(out)

    def write_memory(self, address: int, data: bytes) -> None:
        address = int(address)
        view = memoryview(bytes(data))
        offset = 0
        while offset < len(view):
            current = address + offset
            if current % 4 == 0 and len(view) - offset >= 4:
                count = min((len(view) - offset) // 4, _MAX_CHUNK // 4)
                words = struct.unpack_from(f"<{count}I", view, offset)
                payload = " ".join(f"0x{word:08x}" for word in words)
                self.command(f"write_memory {current:#x} 32 {{{payload}}}")
                offset += count * 4
            else:
                count = min(len(view) - offset, _MAX_CHUNK)
                payload = " ".join(f"0x{byte:02x}" for byte in view[offset : offset + count])
                self.command(f"write_memory {current:#x} 8 {{{payload}}}")
                offset += count

    # ------------------------------------------------------------------
    # programming
    # ------------------------------------------------------------------
    def flash_file(self, path: str | os.PathLike, address: int | None = None, *, verify: bool = True) -> int:
        """Program ``path`` and verify it; returns the number of bytes written.

        The target is reset and allowed to run briefly first: on this board the
        QSPI controller is configured by the bootloader/firmware, and OpenOCD's
        stmqspi driver refuses to touch the bank while the controller is still
        unconfigured ("No QSPI, no OCTOSPI at 0x52005000").
        """
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            raise ConfigError(f"firmware file not found: {file_path}")

        self._wake_target()
        self.halt()

        total = 0
        try:
            for start, image in self._images(file_path, address):
                # Path() renders backslashes on Windows, and TCL treats
                # "\a"/"\b"/"\r" as escapes -- always send POSIX separators.
                target = _tcl_path(image)
                if start is not None:
                    target = f"{target} {start:#x}"
                text = self._stream(f"flash write_image erase {target}", timeout=300, stop=_WROTE_RE)
                total += _image_size(image)
                if "wrote" not in text.lower():
                    raise TargetError(
                        f"flash failed for {file_path.name}: {text.strip() or 'no output'}"
                    )
                if verify:
                    verified_text = self._stream(
                        f"verify_image {target}", timeout=300, stop=_VERIFIED_RE
                    )
                    if "verified" not in verified_text.lower():
                        raise TargetError(
                            f"verify failed for {file_path.name}: "
                            f"{verified_text.strip() or 'no output'}"
                        )
        finally:
            self._cleanup_temps()
        return total

    def flash(self, data: bytes, address: int) -> None:
        fd, tmp = tempfile.mkstemp(prefix="embdbg-flash-", suffix=".bin")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(data))
            self.flash_file(tmp, address)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _wake_target(self) -> None:
        """Let the firmware run once so clock/QSPI init happens, then stop it."""
        try:
            if self.is_running:
                self.halt()
                return
        except ServerError:
            pass
        self.command("reset run")
        time.sleep(self.startup_delay)
        self.halt()

    def _cleanup_temps(self) -> None:
        for path in self._temps:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._temps.clear()

    def _images(self, path: Path, address: int | None) -> list[tuple[int | None, str]]:
        suffix = path.suffix.lower()
        if suffix == ".elf":
            images = _write_elf_images(path)
            self._temps.extend(image for _, image in images)
            return images
        if suffix in (".hex", ".ihex", ".srec", ".s19"):
            return [(None, str(path))]
        if suffix == ".bin":
            if address is None:
                raise ConfigError("--base is required when flashing a .bin file")
            return [(int(address), str(path))]
        raise ConfigError(f"unsupported firmware file type: {path.suffix}")

    # ------------------------------------------------------------------
    # info / unsupported
    # ------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        try:
            version = self.command("version").strip()
        except ServerError:
            version = ""
        return {"version": version, "state": self._state(), "port": self.port}

    #: RTT is implemented on top of the J-Link DLL's RTT API.
    def rtt_start(self, address: int = 0):  # pragma: no cover - guard
        raise ConfigError(
            "RTT is not available with the OpenOCD backend; use the J-Link backend "
            "or the board's UART console"
        )

    def rtt_read(self, channel: int = 0, size: int = 4096) -> bytes:  # pragma: no cover - guard
        raise ConfigError("RTT is not available with the OpenOCD backend")

    def rtt_stop(self) -> None:  # pragma: no cover - guard
        pass


# ----------------------------------------------------------------------
def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _tcl_path(value: str | os.PathLike) -> str:
    """Format a file path for a TCL command.

    OpenOCD parses commands as TCL, so a Windows path would have its ``\\a``,
    ``\\b`` and ``\\r`` sequences eaten as escapes.  Send POSIX separators, and
    brace the result when it contains whitespace.
    """
    text = Path(value).as_posix()
    if any(ch.isspace() for ch in text):
        return "{" + text + "}"
    return text


def _image_size(image: str) -> int:
    try:
        return os.path.getsize(image)
    except OSError:  # pragma: no cover
        return 0


def _write_elf_images(path: Path) -> list[tuple[int, str]]:
    """Split an ELF into contiguous loadable byte ranges on disk.

    OpenOCD's own ELF loader drives the flash driver with ``p_vaddr``, and this
    board's firmware has ``.data`` at ``0x24000000`` with its load address in
    QSPI -- that combination makes OpenOCD assert in ``flash/nor/core.c``.
    Converting to flat images (using ``p_paddr``) avoids the loader entirely.
    """
    try:
        from elftools.elf.elffile import ELFFile
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency hint
        raise ConfigError(
            "flashing an ELF with the OpenOCD backend needs pyelftools "
            "(pip install pyelftools); alternatively pass a .bin with --base"
        ) from exc

    ranges: list[tuple[int, bytearray]] = []
    with open(path, "rb") as fh:
        elf = ELFFile(fh)
        for segment in elf.iter_segments():
            if segment["p_type"] != "PT_LOAD":
                continue
            data = segment.data()
            if not data:
                continue
            address = int(segment["p_paddr"])
            if ranges:
                start, buffer = ranges[-1]
                end = start + len(buffer)
                if address == end:
                    buffer += data
                    continue
                if address < end:
                    # overlapping load segments: keep what does not overlap
                    overlap = end - address
                    if overlap < len(data):
                        buffer += data[overlap:]
                    continue
            ranges.append((address, bytearray(data)))

    if not ranges:
        raise ConfigError(f"{path} has no loadable segments")

    images: list[tuple[int, str]] = []
    try:
        for start, data in ranges:
            fd, tmp = tempfile.mkstemp(prefix="embdbg-image-", suffix=".bin")
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(data))
            images.append((start, tmp))
    except OSError:  # pragma: no cover
        pass
    return images
