"""Serial port plugin: configurable send / receive / monitor.

Accepts a device name (``COM3``, ``/dev/ttyUSB0``) or a pyserial URL
(``loop://``, ``socket://host:port``), which makes it testable without hardware.

Configuration (``[plugins.serial]`` in focdbg.toml, or the ``serial`` CLI):

    port      = "COM3"        # required
    baudrate  = 115200
    bytesize  = 8             # 5..8
    parity    = "N"           # N/E/O/M/S
    stopbits  = "1"           # 1 / 1.5 / 2
    flow      = "none"        # none / rtscts / xonxoff / dsrdtr
    timeout   = 0.1
    encoding  = "utf-8"
    dtr       = "on"|"off"    # optional
    rts       = "on"|"off"    # optional
    mode      = "idle"        # idle / monitor
    log       = "serial.log"  # monitor output (default stderr)
    hex       = false         # monitor as hex
    timestamp = true
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from typing import Any, TextIO

from .base import Plugin, PluginContext, PluginError

_PARITY_NAMES = ("N", "E", "O", "M", "S")
_STOPBITS_NAMES = ("1", "1.5", "2")
_FLOW_NAMES = ("none", "rtscts", "xonxoff", "dsrdtr")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise PluginError(f"invalid boolean: {value!r}")


def _as_bool_optional(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    return _as_bool(value)


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def encode_payload(
    text: str = "",
    *,
    hex_mode: bool = False,
    encoding: str = "utf-8",
    newline: str = "lf",
) -> bytes:
    """Turn CLI text into bytes (hex mode, or text + an optional line ending)."""
    if hex_mode:
        cleaned = text.replace(" ", "").replace(",", "").replace("\n", "")
        try:
            data = bytes.fromhex(cleaned)
        except ValueError as exc:
            raise PluginError(f"invalid hex payload: {text!r}") from exc
    else:
        data = str(text).encode(encoding, errors="replace")
    if newline == "lf":
        data += b"\n"
    elif newline == "crlf":
        data += b"\r\n"
    elif newline not in ("none", ""):
        raise PluginError(f"invalid newline mode: {newline!r}")
    return data


def list_ports() -> list[dict[str, str]]:
    try:
        from serial.tools import list_ports as serial_list_ports
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise PluginError("pyserial is required: pip install pyserial") from exc
    return [
        {
            "device": port.device,
            "description": port.description or "",
            "hwid": port.hwid or "",
        }
        for port in serial_list_ports.comports()
    ]


class SerialPlugin(Plugin):
    name = "serial"
    description = "Serial port send/receive (configurable framing and flow control)"

    def __init__(self, options: dict[str, Any] | None = None, context: PluginContext | None = None):
        super().__init__(options, context)
        opts = self.options
        self.port = str(opts.get("port") or opts.get("url") or "").strip()
        if not self.port:
            raise PluginError(
                "serial plugin requires 'port' (COM3, /dev/ttyUSB0, loop://, socket://host:port)"
            )
        self.baudrate = int(opts.get("baudrate", 115200))
        self.bytesize = int(opts.get("bytesize", 8))
        self.parity = str(opts.get("parity", "N")).upper()[:1]
        self.stopbits = str(opts.get("stopbits", "1"))
        self.flow = str(opts.get("flow", "none")).lower()
        self.timeout = float(opts.get("timeout", 0.1))
        self.encoding = str(opts.get("encoding", "utf-8"))
        self.mode = str(opts.get("mode", "idle")).lower()
        self.log_path = opts.get("log") or opts.get("out")
        self.hex_mode = _as_bool(opts.get("hex"), False)
        self.timestamp = _as_bool(opts.get("timestamp"), True)
        self.dtr = _as_bool_optional(opts.get("dtr"))
        self.rts = _as_bool_optional(opts.get("rts"))

        self._serial = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sink: TextIO | None = None
        self._owns_sink = False

        self._validate()

    # ------------------------------------------------------------------
    def _validate(self) -> None:
        if self.bytesize not in (5, 6, 7, 8):
            raise PluginError(f"bytesize must be 5..8, got {self.bytesize}")
        if self.parity not in _PARITY_NAMES:
            raise PluginError(f"parity must be one of {_PARITY_NAMES}, got {self.parity!r}")
        if self.stopbits not in _STOPBITS_NAMES:
            raise PluginError(f"stopbits must be one of {_STOPBITS_NAMES}, got {self.stopbits!r}")
        if self.flow not in _FLOW_NAMES:
            raise PluginError(f"flow must be one of {_FLOW_NAMES}, got {self.flow!r}")
        if self.baudrate <= 0:
            raise PluginError(f"baudrate must be positive, got {self.baudrate}")

    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._serial is not None and bool(self._serial.is_open)

    def open(self) -> "SerialPlugin":
        if self._serial is not None:
            return self
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise PluginError("pyserial is required: pip install pyserial") from exc

        parity = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
            "M": serial.PARITY_MARK,
            "S": serial.PARITY_SPACE,
        }[self.parity]
        bytesize = {
            5: serial.FIVEBITS,
            6: serial.SIXBITS,
            7: serial.SEVENBITS,
            8: serial.EIGHTBITS,
        }[self.bytesize]
        stopbits = {
            "1": serial.STOPBITS_ONE,
            "1.5": serial.STOPBITS_ONE_POINT_FIVE,
            "2": serial.STOPBITS_TWO,
        }[self.stopbits]

        try:
            self._serial = serial.serial_for_url(
                self.port,
                baudrate=self.baudrate,
                bytesize=bytesize,
                parity=parity,
                stopbits=stopbits,
                timeout=self.timeout,
                write_timeout=self.timeout,
                rtscts=self.flow == "rtscts",
                xonxoff=self.flow == "xonxoff",
                dsrdtr=self.flow == "dsrdtr",
            )
        except Exception as exc:
            raise PluginError(f"cannot open serial port {self.port!r}: {exc}") from exc
        try:
            if self.dtr is not None:
                self._serial.dtr = self.dtr
            if self.rts is not None:
                self._serial.rts = self.rts
        except Exception as exc:
            raise PluginError(f"cannot set DTR/RTS on {self.port!r}: {exc}") from exc
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        if self._owns_sink and self._sink is not None:
            try:
                self._sink.close()
            except Exception:
                pass
        self._owns_sink = False
        self._sink = None

    def __enter__(self) -> "SerialPlugin":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self.open()
        if self.mode == "monitor":
            self._open_sink()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._background_monitor, name="focdbg-serial", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "baudrate": self.baudrate,
            "bytesize": self.bytesize,
            "parity": self.parity,
            "stopbits": self.stopbits,
            "flow": self.flow,
            "mode": self.mode,
            "open": self.is_open,
        }

    # ------------------------------------------------------------------
    # raw I/O
    # ------------------------------------------------------------------
    def _require(self):
        if self._serial is None:
            raise PluginError("serial port is not open")
        return self._serial

    def write(self, data: bytes) -> int:
        written = self._require().write(bytes(data))
        return int(written or 0)

    def flush(self) -> None:
        self._require().flush()

    def read(self, size: int = 1) -> bytes:
        return bytes(self._require().read(size) or b"")

    def read_available(self) -> bytes:
        link = self._require()
        waiting = getattr(link, "in_waiting", 0)
        if not waiting:
            return b""
        return bytes(link.read(waiting) or b"")

    def expect(self, pattern: bytes, timeout: float = 2.0) -> tuple[bool, bytes]:
        """Read until ``pattern`` appears or the timeout expires."""
        if not pattern:
            raise PluginError("expect pattern must not be empty")
        deadline = time.perf_counter() + max(0.0, timeout)
        received = bytearray()
        while time.perf_counter() < deadline:
            chunk = self.read_available()
            if not chunk:
                chunk = self.read(1)
            if chunk:
                received.extend(chunk)
                if pattern in received:
                    return True, bytes(received)
        return False, bytes(received)

    # ------------------------------------------------------------------
    # monitor
    # ------------------------------------------------------------------
    def _open_sink(self) -> None:
        if self.log_path:
            try:
                self._sink = open(self.log_path, "a", encoding="utf-8", errors="replace", newline="")
            except OSError as exc:
                raise PluginError(f"cannot open serial log {self.log_path!r}: {exc}") from exc
            self._owns_sink = True
        else:
            self._sink = sys.stderr
            self._owns_sink = False

    def _background_monitor(self) -> None:
        try:
            self.monitor(out=self._sink, stop_event=self._stop)
        except Exception as exc:  # pragma: no cover - background thread
            self.context.emit(f"serial monitor stopped: {exc}")

    def monitor(
        self,
        *,
        duration: float | None = None,
        out: TextIO | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Read until duration elapses / stop_event is set, emitting lines or hex."""
        out = out or sys.stdout
        deadline = None if duration is None else time.perf_counter() + duration
        buffer = bytearray()
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if deadline is not None and time.perf_counter() >= deadline:
                break
            chunk = self.read_available()
            if not chunk:
                chunk = self.read(1)
            if not chunk:
                continue
            if self.hex_mode:
                self._emit_hex(chunk, out)
                continue
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, rest = bytes(buffer).partition(b"\n")
                buffer = bytearray(rest)
                self._emit_text(line.rstrip(b"\r"), out)
        if buffer and not self.hex_mode:
            self._emit_text(bytes(buffer), out)

    def _emit_text(self, data: bytes, out: TextIO) -> None:
        text = data.decode(self.encoding, errors="replace")
        if self.timestamp:
            out.write(f"[{_timestamp()}] {text}\n")
        else:
            out.write(text + "\n")
        out.flush()

    def _emit_hex(self, data: bytes, out: TextIO) -> None:
        line = " ".join(f"{byte:02x}" for byte in data)
        if self.timestamp:
            out.write(f"[{_timestamp()}] {line}\n")
        else:
            out.write(line + "\n")
        out.flush()
