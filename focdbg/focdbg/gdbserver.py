"""Lifecycle management for ``JLinkGDBServerCL``.

The server is the bottom half of the backend: it owns the USB probe and speaks
SWD to the MCU.  GDB connects to it over the GDB Remote Serial Protocol.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .errors import ServerError
from .util import find_free_ports

_READY_RE = re.compile(r"Waiting for GDB connection", re.I)
_ERROR_RE = re.compile(
    r"(ERROR|cannot connect|could not connect|failed to connect|"
    r"no j-link|not found|invalid|licen[cs]e|GDBServer will be closed)",
    re.I,
)
_FW_RE = re.compile(r"Firmware:\s*(.+)")
_SN_RE = re.compile(r"S/N:\s*(\d+)")
_VOLT_RE = re.compile(r"Target voltage:\s*([\d.]+)\s*V")
_DEVICE_RE = re.compile(r"Target device:\s*(.+)")
_SPEED_RE = re.compile(r"Target interface speed:\s*(\d+)\s*kHz")


class JLinkGDBServer:
    """Start, watch and stop one ``JLinkGDBServerCL`` process."""

    def __init__(
        self,
        executable: str | os.PathLike,
        *,
        device: str,
        interface: str = "SWD",
        speed_khz: int = 4000,
        port: int | None = None,
        base_port: int = 2331,
        singlerun: bool = True,
        verify: bool = False,
        probe_serial: str | None = None,
        log_file: str | None = None,
        extra_args: list[str] | None = None,
        startup_grace: float = 0.3,
    ):
        self.executable = str(executable)
        self.device = device
        self.interface = interface
        self.speed_khz = int(speed_khz)
        self.base_port = int(base_port)
        self.singlerun = singlerun
        self.verify = verify
        self.probe_serial = probe_serial
        self.log_file = log_file
        self.extra_args = list(extra_args or [])
        self.startup_grace = startup_grace

        self._port = port
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lines: deque[str] = deque(maxlen=4000)
        self._ready = threading.Event()
        self._failure: str | None = None

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        assert self._port is not None
        return self._port

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def argv(self) -> list[str]:
        # The server also opens port+1 (SWO) and port+2 (terminal I/O).
        port = self._port if self._port is not None else find_free_ports(self.base_port, 3)
        argv = [
            self.executable,
            "-device",
            self.device,
            "-if",
            self.interface,
            "-speed",
            str(self.speed_khz),
            "-port",
            str(port),
        ]
        if self.singlerun:
            argv.append("-singlerun")
        if self.probe_serial:
            argv += ["-select", f"usb={self.probe_serial}"]
        if self.log_file:
            Path(self.log_file).parent.mkdir(parents=True, exist_ok=True)
            argv += ["-log", self.log_file]
        argv += self.extra_args
        return argv

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._proc is not None:
            return
        self._port = self._port or find_free_ports(self.base_port, 3)
        argv = self.argv()
        kwargs: dict[str, Any] = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise ServerError(f"cannot run {self.executable}: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name="focdbg-jlink-server", daemon=True)
        self._reader.start()

        # The server's stdout is block-buffered when it is a pipe, so readiness
        # cannot be observed there (the "-log" file, in contrast, is flushed
        # line by line).  Wait just long enough to catch an immediate exit and
        # let the GDB connect retry loop in the backend decide when it is up.
        time.sleep(max(0.0, self.startup_grace))
        if self._failure:
            self.stop()
            raise ServerError(self._failure, detail=self.output_text())
        if not self.is_alive():
            rc = self._proc.returncode if self._proc else "?"
            self.stop()
            raise ServerError(f"J-Link GDB server exited during startup (rc={rc})", detail=self.output_text())

    def stop(self, timeout: float = 5.0) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass
        if self._reader is not None:
            self._reader.join(timeout=timeout)
            self._reader = None
        self._ready.clear()

    # ------------------------------------------------------------------
    def output_lines(self) -> list[str]:
        return list(self._lines)

    def output_text(self) -> str:
        return "".join(self._lines)

    def banner_text(self) -> str:
        """Server banner: prefer the real-time log file, fall back to stdout."""
        if self.log_file:
            try:
                with open(self.log_file, "r", encoding="utf-8", errors="replace") as fh:
                    return fh.read(200_000)
            except OSError:
                pass
        return self.output_text()

    def info(self) -> dict[str, Any]:
        """Parse useful facts out of the server banner."""
        text = self.banner_text()
        info: dict[str, Any] = {}
        if m := _FW_RE.search(text):
            info["firmware"] = m.group(1).strip()
        if m := _SN_RE.search(text):
            info["serial"] = m.group(1)
        if m := _VOLT_RE.search(text):
            try:
                info["voltage"] = float(m.group(1))
            except ValueError:
                pass
        if m := _DEVICE_RE.search(text):
            info["device"] = m.group(1).strip()
        if m := _SPEED_RE.search(text):
            info["speed_khz"] = int(m.group(1))
        info["port"] = self._port
        info["pid"] = self.pid
        return info

    # ------------------------------------------------------------------
    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                self._lines.append(line)
                stripped = line.rstrip("\r\n")
                if _READY_RE.search(stripped):
                    self._ready.set()
                elif _ERROR_RE.search(stripped) and self._failure is None:
                    self._failure = stripped
        except Exception:  # pragma: no cover - defensive
            pass
        finally:
            self._ready.set()
