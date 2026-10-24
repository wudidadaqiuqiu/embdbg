"""Minimal GDB Remote Serial Protocol (RSP) server.

This is what lets a single process own the J-Link **and** serve GDB: the process
translates RSP packets into J-Link DLL calls.  Ozone does the same internally
(with its own debugger); here GDB remains the debugger front-end, so we keep
source-level debugging while the sampler reads memory in the background.

Implemented (enough for arm-none-eabi-gdb / VSCode):
  qSupported, QStartNoAckMode, ?, g/p/P, m/M, Z0/Z1/z0/z1, c/s, Hc/Hg,
  qXfer:features:read, qRcmd, R, D, k, qC, qfThreadInfo/qsThreadInfo, qAttached.

Not implemented (deliberately):
  flash programming via vFlash* / GDB ``load`` -- use ``embdbg flash`` instead.
"""

from __future__ import annotations

import select
import socket
import threading
import time
import struct
from typing import Callable

from .errors import FocdbgError
from .jlink_probe import JLinkProbe

_REG_NAMES = (
    "r0 r1 r2 r3 r4 r5 r6 r7 r8 r9 r10 r11 r12 sp lr pc "
    "xpsr msp psp primask basepri faultmask control"
).split()

TARGET_XML = (
    '<?xml version="1.0"?>\n'
    '<!DOCTYPE target SYSTEM "gdb-target.dtd">\n'
    '<target version="1.0">\n'
    "  <architecture>arm</architecture>\n"
    '  <feature name="org.gnu.gdb.arm.m-profile">\n'
    "    <reg name=\"r0\" bitsize=\"32\" regnum=\"0\" type=\"uint32\"/>\n"
    "    <reg name=\"r1\" bitsize=\"32\" regnum=\"1\" type=\"uint32\"/>\n"
    "    <reg name=\"r2\" bitsize=\"32\" regnum=\"2\" type=\"uint32\"/>\n"
    "    <reg name=\"r3\" bitsize=\"32\" regnum=\"3\" type=\"uint32\"/>\n"
    "    <reg name=\"r4\" bitsize=\"32\" regnum=\"4\" type=\"uint32\"/>\n"
    "    <reg name=\"r5\" bitsize=\"32\" regnum=\"5\" type=\"uint32\"/>\n"
    "    <reg name=\"r6\" bitsize=\"32\" regnum=\"6\" type=\"uint32\"/>\n"
    "    <reg name=\"r7\" bitsize=\"32\" regnum=\"7\" type=\"uint32\"/>\n"
    "    <reg name=\"r8\" bitsize=\"32\" regnum=\"8\" type=\"uint32\"/>\n"
    "    <reg name=\"r9\" bitsize=\"32\" regnum=\"9\" type=\"uint32\"/>\n"
    "    <reg name=\"r10\" bitsize=\"32\" regnum=\"10\" type=\"uint32\"/>\n"
    "    <reg name=\"r11\" bitsize=\"32\" regnum=\"11\" type=\"uint32\"/>\n"
    "    <reg name=\"r12\" bitsize=\"32\" regnum=\"12\" type=\"uint32\"/>\n"
    "    <reg name=\"sp\" bitsize=\"32\" regnum=\"13\" type=\"data_ptr\"/>\n"
    "    <reg name=\"lr\" bitsize=\"32\" regnum=\"14\" type=\"uint32\"/>\n"
    "    <reg name=\"pc\" bitsize=\"32\" regnum=\"15\" type=\"code_ptr\"/>\n"
    "    <reg name=\"xpsr\" bitsize=\"32\" regnum=\"25\" type=\"uint32\"/>\n"
    "  </feature>\n"
    '  <feature name="org.gnu.gdb.arm.m-system">\n'
    "    <reg name=\"msp\" bitsize=\"32\" regnum=\"26\" type=\"data_ptr\"/>\n"
    "    <reg name=\"psp\" bitsize=\"32\" regnum=\"27\" type=\"data_ptr\"/>\n"
    "    <reg name=\"primask\" bitsize=\"32\" regnum=\"28\" type=\"uint32\"/>\n"
    "    <reg name=\"basepri\" bitsize=\"32\" regnum=\"29\" type=\"uint32\"/>\n"
    "    <reg name=\"faultmask\" bitsize=\"32\" regnum=\"30\" type=\"uint32\"/>\n"
    "    <reg name=\"control\" bitsize=\"32\" regnum=\"31\" type=\"uint32\"/>\n"
    "  </feature>\n"
    "</target>\n"
)


class RspServer:
    """Single-client GDB RSP server bound to a :class:`JLinkProbe`."""

    def __init__(
        self,
        probe: JLinkProbe,
        host: str = "127.0.0.1",
        port: int = 2331,
        on_event: Callable[[str], None] | None = None,
    ):
        self.probe = probe
        self.host = host
        self._requested_port = int(port)
        self._on_event = on_event
        self._sock: socket.socket | None = None
        self._client: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ack = True
        self._port = int(port)

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._port

    def start(self) -> "RspServer":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self._requested_port))
        sock.listen(1)
        sock.settimeout(0.2)
        self._sock = sock
        self._port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="embdbg-rsp", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._client, self._sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "RspServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ------------------------------------------------------------------
    def _emit(self, message: str) -> None:
        if self._on_event is not None:
            self._on_event(message)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, peer = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._client = conn
            self._emit(f"GDB connected from {peer[0]}:{peer[1]}")
            try:
                self._serve(conn)
            except (ConnectionError, OSError):
                pass
            except Exception as exc:  # keep serving the next client
                self._emit(f"GDB session error: {exc}")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                self._client = None
                self._emit("GDB disconnected")

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(None)
        self._ack = True  # per-connection state: GDB renegotiates every time
        buf = bytearray()
        while not self._stop.is_set():
            packet = self._extract(buf, conn)
            if packet is None:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf.extend(chunk)
                continue
            reply, close = self._handle(packet, conn)
            if reply is not None:
                self._send(conn, reply)
            if close:
                return

    # ------------------------------------------------------------------
    def _extract(self, buf: bytearray, conn: socket.socket) -> str | None:
        while True:
            start = buf.find(b"$")
            if start < 0:
                buf.clear()
                return None
            end = buf.find(b"#", start)
            if end < 0 or len(buf) < end + 3:
                del buf[:start]
                return None
            payload = bytes(buf[start + 1 : end])
            try:
                expected = int(bytes(buf[end + 1 : end + 3]), 16)
            except ValueError:
                del buf[: end + 3]
                continue
            del buf[: end + 3]
            if (sum(payload) & 0xFF) != expected:
                self._send_raw(conn, b"-")
                continue
            if self._ack:
                self._send_raw(conn, b"+")
            return payload.decode("latin-1")

    def _send_raw(self, conn: socket.socket, data: bytes) -> None:
        try:
            conn.sendall(data)
        except OSError:
            pass

    def _send(self, conn: socket.socket, payload: str) -> None:
        data = payload.encode("latin-1")
        frame = b"$" + data + b"#" + f"{sum(data) & 0xFF:02x}".encode()
        self._send_raw(conn, frame)

    # ------------------------------------------------------------------
    def _stop_reply(self) -> str:
        return "T05thread:1;"

    def _handle(self, packet: str, conn: socket.socket) -> tuple[str | None, bool]:
        if packet == "QStartNoAckMode":
            self._ack = False
            return "OK", False
        if packet.startswith("qSupported"):
            return (
                "PacketSize=4000;qXfer:features:read+;QStartNoAckMode+;"
                "swbreak+;hwbreak+",
                False,
            )
        if packet == "?":
            return self._stop_reply(), False
        if packet.startswith("qXfer:features:read:"):
            return self._xfer(packet), False
        if packet == "g":
            return self.probe.read_gdb_registers().hex(), False
        if packet.startswith("p"):
            return self._read_register(packet[1:]), False
        if packet.startswith("P"):
            self._write_register(packet[1:])
            return "OK", False
        if packet.startswith("m"):
            return self._read_memory(packet[1:]), False
        if packet.startswith("M"):
            self._write_memory(packet[1:])
            return "OK", False
        if packet[:1] in ("Z", "z"):
            return self._breakpoint(packet), False
        if packet == "c" or (packet.startswith("c") and packet[1:].isdigit()):
            return self._continue(conn), False
        if packet == "s" or (packet.startswith("s") and packet[1:].isdigit()):
            self.probe.step()
            return self._stop_reply(), False
        if packet in ("Hc-1", "Hc0", "Hc1", "Hg0", "Hg1", "Hc1"):
            return "OK", False
        if packet == "qC":
            return "QC1", False
        if packet == "qfThreadInfo":
            return "m1", False
        if packet == "qsThreadInfo":
            return "l", False
        if packet.startswith("qThreadExtraInfo"):
            return "Thread 1".encode().hex(), False
        if packet == "qAttached":
            return "1", False
        if packet.startswith("qOffsets"):
            return "Text=0;Data=0;Bss=0", False
        if packet.startswith("qSymbol"):
            return "OK", False
        if packet.startswith("qTStatus") or packet.startswith("qXfer:memory-map"):
            return "", False
        if packet == "!":
            return "OK", False
        if packet == "vCont?":
            return "", False
        if packet.startswith("vMustReplyEmpty"):
            return "", False
        if packet.startswith("qRcmd,"):
            return self._monitor(packet[6:]), False
        if packet == "R":
            self.probe.reset(halt=True)
            return "OK", False
        if packet in ("D", "k"):
            try:
                if self.probe.halted:
                    self.probe.clear_all_breakpoints()
            except FocdbgError:
                pass
            return "OK", True
        if packet.startswith("vKill"):
            return "OK", True
        return "", False

    # ------------------------------------------------------------------
    def _xfer(self, packet: str) -> str:
        try:
            name, rest = packet[len("qXfer:features:read:") :].split(":", 1)
            offset_s, length_s = rest.split(",")
            offset = int(offset_s, 16)
            length = int(length_s, 16)
        except ValueError:
            return "E00"
        if "target.xml" not in name:
            return "E00"
        data = TARGET_XML.encode("ascii")
        chunk = data[offset : offset + length]
        prefix = "l" if offset + len(chunk) >= len(data) else "m"
        return prefix + chunk.decode("ascii")

    def _read_register(self, spec: str) -> str:
        try:
            number = int(spec, 16)
            value = self.probe.read_gdb_register(number)
        except (ValueError, FocdbgError):
            return "E01"
        return struct.pack("<I", value).hex()

    def _write_register(self, spec: str) -> None:
        try:
            number_s, value_s = spec.split("=", 1)
            number = int(number_s, 16)
            raw = bytes.fromhex(value_s)
            (value,) = struct.unpack("<I", raw[:4].ljust(4, b"\x00"))
        except ValueError:
            return
        self.probe.write_gdb_register(number, value)

    def _read_memory(self, spec: str) -> str:
        try:
            addr_s, length_s = spec.split(",")
            address = int(addr_s, 16)
            length = int(length_s, 16)
        except ValueError:
            return "E01"
        try:
            return self.probe.read_memory(address, length).hex()
        except FocdbgError:
            return "E01"

    def _write_memory(self, spec: str) -> None:
        try:
            head, hexdata = spec.split(":", 1)
            addr_s, length_s = head.split(",")
            address = int(addr_s, 16)
            length = int(length_s, 16)
            data = bytes.fromhex(hexdata)
        except ValueError:
            return
        self.probe.write_memory(address, data[:length])

    def _breakpoint(self, packet: str) -> str:
        kind = packet[1:2]
        if kind not in ("0", "1"):
            return ""
        try:
            rest = packet[2:].lstrip(",")
            addr_s, _, _ = rest.partition(",")
            address = int(addr_s, 16)
        except ValueError:
            return "E01"
        if packet[0] == "Z":
            self.probe.set_breakpoint(address)
        else:
            self.probe.clear_breakpoint(address)
        return "OK"

    def _continue(self, conn: socket.socket) -> str:
        self.probe.go()
        while not self._stop.is_set():
            if self.probe.halted:
                return self._stop_reply()
            ready, _, _ = select.select([conn], [], [], 0.01)
            if not ready:
                continue
            data = conn.recv(1)
            if not data:
                raise ConnectionError("GDB disconnected")
            if data == b"\x03":
                self.probe.halt()
                return self._stop_reply()
            # ignore stray acks / bytes; all-stop GDB only sends ^C here
        return self._stop_reply()

    def _monitor(self, hex_command: str) -> str:
        try:
            command = bytes.fromhex(hex_command).decode("latin-1").strip().lower()
        except ValueError:
            return "E01"
        try:
            if command in ("reset", "reset halt"):
                self.probe.reset(halt=True)
            elif command in ("halt", "h"):
                self.probe.halt()
            elif command in ("go", "g", "continue"):
                self.probe.go()
            elif command.startswith("speed"):
                pass
            else:
                return "OK"
        except FocdbgError:
            return "E01"
        return "OK"
