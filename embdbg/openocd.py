"""Lifecycle management for ``openocd``.

OpenOCD plays the same role as ``JLinkGDBServerCL``: it owns the USB probe,
talks SWD/JTAG to the MCU and exposes a GDB server.  It is driven entirely by a
board config file (``-f artpi.cfg``), so the "device" string that J-Link needs
does not exist here -- the target is whatever the config declares.

Besides the GDB port this class opens OpenOCD's **TCL** port, which
:class:`embdbg.openocd_probe.OpenOcdProbe` uses for non-intrusive memory access
and for ``flash write_image`` (the J-Link DLL equivalent).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .errors import ServerError
from .util import find_free_ports

_TCL_END = b"\x1a"

_ERROR_RE = re.compile(
    # NB: do not match "license" here -- openocd's banner says "Licensed under
    # GNU GPL v2" on every start.
    r"(^\s*Error:|could not|cannot connect|failed to (?:connect|open|start)|"
    r"unable to connect|no device found)",
    re.I,
)
_VOLT_RE = re.compile(r"Target voltage:\s*([\d.]+)")
_STLINK_RE = re.compile(r"((?:STLINK|ST-Link)\s+\S+\s+\(API v\d\)[^\r\n]*)", re.I)
_DPIDR_RE = re.compile(r"SWD DPIDR\s+(0x[0-9a-fA-F]+)")
_CPU_RE = re.compile(r"(Cortex-M\d+\s+r\d+p\d+)")
_LISTEN_RE = re.compile(r"Listening on port (\d+) for gdb connections", re.I)


class OpenOcdServer:
    """Start, watch and stop one ``openocd`` process."""

    def __init__(
        self,
        executable: str | os.PathLike,
        config_path: str | os.PathLike,
        *,
        speed_khz: int | None = None,
        gdb_port: int | None = None,
        tcl_port: int | None = None,
        base_port: int = 3333,
        extra_args: list[str] | None = None,
        startup_grace: float = 0.3,
    ):
        self.executable = str(executable)
        self.config_path = str(config_path)
        self.speed_khz = int(speed_khz) if speed_khz else None
        self.base_port = int(base_port)
        self.extra_args = list(extra_args or [])
        self.startup_grace = startup_grace

        self._gdb_port = gdb_port
        self._tcl_port = tcl_port
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lines: deque[str] = deque(maxlen=4000)
        self._failure: str | None = None

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        """GDB server port (what GDB connects to)."""
        assert self._gdb_port is not None
        return self._gdb_port

    @property
    def tcl_port(self) -> int:
        assert self._tcl_port is not None
        return self._tcl_port

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def argv(self) -> list[str]:
        if self._gdb_port is None:
            self._gdb_port = find_free_ports(self.base_port, 1)
        if self._tcl_port is None:
            # keep TCL next to GDB so both come from one free window
            self._tcl_port = find_free_ports(self._gdb_port + 1, 1)
        ports = f"gdb port {self._gdb_port}; tcl port {self._tcl_port}; telnet port disabled"
        argv = [self.executable, "-f", self.config_path, "-c", ports]
        if self.speed_khz:
            # board configs usually set this too; an explicit -c wins because
            # it runs after -f
            argv += ["-c", f"adapter speed {self.speed_khz}"]
        argv += self.extra_args
        return argv

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._proc is not None:
            return
        if not Path(self.config_path).is_file():
            raise ServerError(f"OpenOCD config not found: {self.config_path}")
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
        self._reader = threading.Thread(target=self._read_loop, name="embdbg-openocd-server", daemon=True)
        self._reader.start()

        time.sleep(max(0.0, self.startup_grace))
        if self._failure:
            self.stop()
            raise ServerError(self._failure, detail=self.output_text())
        if not self.is_alive():
            rc = self._proc.returncode if self._proc else "?"
            self.stop()
            raise ServerError(
                f"openocd exited during startup (rc={rc}); check {self.config_path}",
                detail=self.output_text(),
            )

    def stop(self, timeout: float = 5.0) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            # `shutdown` over TCL is the graceful path: it releases the probe
            # and leaves the target running.  TerminateProcess (what
            # terminate() does on Windows) can leave the debug port wedged.
            self._shutdown_via_tcl()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
        if self._reader is not None:
            self._reader.join(timeout=timeout)
            self._reader = None

    def _shutdown_via_tcl(self) -> None:
        if self._tcl_port is None:
            return
        try:
            with socket.create_connection(("127.0.0.1", self._tcl_port), timeout=1.0) as sock:
                sock.sendall(b"shutdown" + _TCL_END)
                time.sleep(0.05)
        except OSError:
            pass

    # ------------------------------------------------------------------
    def output_lines(self) -> list[str]:
        return list(self._lines)

    def output_text(self) -> str:
        return "".join(self._lines)[-4000:]

    def info(self) -> dict[str, Any]:
        text = self.output_text()
        info: dict[str, Any] = {}
        if m := _VOLT_RE.search(text):
            try:
                info["voltage"] = float(m.group(1))
            except ValueError:
                pass
        if m := _STLINK_RE.search(text):
            info["firmware"] = m.group(1).strip()
        if m := _DPIDR_RE.search(text):
            info["dpidr"] = m.group(1)
        if m := _CPU_RE.search(text):
            info["cpu"] = m.group(1)
        info["port"] = self._gdb_port
        info["tcl_port"] = self._tcl_port
        info["pid"] = self.pid
        info["config"] = self.config_path
        return info

    def wait_for_gdb_port(self, timeout: float = 15.0) -> bool:
        """True once OpenOCD reports it is listening for GDB."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_alive():
                return False
            if any(_LISTEN_RE.search(line) for line in list(self._lines)):
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------
    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                self._lines.append(line)
                stripped = line.rstrip("\r\n")
                # OpenOCD warns about benign things (errata, missing SVF, ...),
                # so only a real error before the GDB port is up counts as a
                # startup failure; later ones are reported by the command that
                # caused them.
                if self._failure is None and _ERROR_RE.search(stripped):
                    if not any(_LISTEN_RE.search(x) for x in list(self._lines)):
                        self._failure = stripped
        except Exception:  # pragma: no cover - defensive
            pass
