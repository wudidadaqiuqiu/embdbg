"""Direct J-Link probe access (pylink / JLinkARM.dll).

This is the Ozone-style foundation: **one process owns the probe**.  The GDB RSP
server and the sampler both call into this object; every DLL access is
serialized by a re-entrant lock because J-Link's API is not thread safe.

Why pylink instead of the GDB server: the J-Link GDB server does not support
GDB non-stop mode (verified on hardware: ``remote does not support non-stop``),
so GDB cannot read memory while the target runs.  The DLL can -- that is exactly
what Ozone's live watch / graph (HSS) uses.
"""

from __future__ import annotations

import struct
import threading
import time
from typing import Iterable

from .errors import ConfigError, FocdbgError, TargetError

#: GDB register number -> (J-Link register index, name, bits)
GDB_REGISTERS: dict[int, tuple[int, str]] = {
    0: (0, "r0"),
    1: (1, "r1"),
    2: (2, "r2"),
    3: (3, "r3"),
    4: (4, "r4"),
    5: (5, "r5"),
    6: (6, "r6"),
    7: (7, "r7"),
    8: (8, "r8"),
    9: (9, "r9"),
    10: (10, "r10"),
    11: (11, "r11"),
    12: (12, "r12"),
    13: (13, "sp"),
    14: (14, "lr"),
    15: (15, "pc"),
    25: (16, "xpsr"),
    26: (17, "msp"),
    27: (18, "psp"),
    28: (24, "primask"),
    29: (25, "basepri"),
    30: (26, "faultmask"),
    31: (27, "control"),
}

#: Order of the ``g`` packet (ascending GDB register number, gaps omitted).
GDB_REG_ORDER: tuple[int, ...] = tuple(sorted(GDB_REGISTERS))

#: Cortex-M FPB (flash patch and breakpoint) comparators.
FPB_COMP_BASE = 0xE0002008
FPB_COMP_COUNT = 8
#: J-Link tags the top bits of FP_COMP; the address itself lives below bit 28.
FPB_ADDR_MASK = 0x1FFFFFFE


class JLinkProbe:
    """Thread-safe wrapper around one J-Link connection."""

    def __init__(
        self,
        device: str,
        *,
        interface: str = "SWD",
        speed_khz: int = 4000,
        serial: str | None = None,
        halt_on_open: bool = True,
    ):
        self.device = device
        self.interface = interface.upper()
        self.speed_khz = int(speed_khz)
        self.serial = serial
        self.halt_on_open = halt_on_open
        self._jlink = None
        self._pylink = None
        self._lock = threading.RLock()
        self._breakpoints: dict[int, int] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def open(self) -> "JLinkProbe":
        if self._jlink is not None:
            return self
        try:
            import pylink
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ConfigError(
                "pylink-square is required for `embdbg serve`; run: pip install pylink-square"
            ) from exc
        self._pylink = pylink
        link = pylink.JLink()
        try:
            link.open(serial_no=int(self.serial) if self.serial else None)
            # A failed connect can leave NRST asserted; release it first so a
            # previous crashed session cannot keep the target in reset.
            try:
                link.set_reset_pin_high()
            except Exception:
                pass
            tif = (
                pylink.enums.JLinkInterfaces.SWD
                if self.interface == "SWD"
                else pylink.enums.JLinkInterfaces.JTAG
            )
            link.set_tif(tif)
            link.connect(self.device, speed=self.speed_khz)
            if self.halt_on_open:
                link.halt()
            # Hardware comparators survive reset and previous processes; start
            # from a clean slate so a stale breakpoint cannot fire unexpectedly.
            self._clear_fpb()
        except Exception as exc:
            try:
                link.close()
            except Exception:
                pass
            raise FocdbgError(f"J-Link connect failed: {exc}") from exc
        self._jlink = link
        return self

    def close(self) -> None:
        with self._lock:
            link, self._jlink = self._jlink, None
            self._breakpoints.clear()
        if link is not None:
            try:
                if link.halted():
                    self._clear_fpb()
            except Exception:
                pass
            try:
                link.close()
            except Exception:
                pass

    def __enter__(self) -> "JLinkProbe":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._jlink is not None

    def _require(self):
        if self._jlink is None:
            raise TargetError("J-Link is not open")
        return self._jlink

    # ------------------------------------------------------------------
    # execution state
    # ------------------------------------------------------------------
    @property
    def halted(self) -> bool:
        with self._lock:
            return bool(self._require().halted())

    @property
    def is_running(self) -> bool:
        return not self.halted

    def halt(self) -> None:
        with self._lock:
            self._require().halt()

    def go(self) -> None:
        """Resume without resetting (pylink's ``restart`` maps to JLINKARM_GoEx)."""
        with self._lock:
            link = self._require()
            if not link.halted():
                return
            link.restart()

    resume = go

    def step(self) -> None:
        with self._lock:
            self._require().step(thumb=True)

    def reset(self, halt: bool = True) -> None:
        with self._lock:
            self._require().reset(ms=0, halt=halt)

    def wait_halted(self, timeout: float | None = None, stop_event=None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.halted:
                return True
            if stop_event is not None and stop_event.is_set():
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.002)

    # ------------------------------------------------------------------
    # memory / registers
    # ------------------------------------------------------------------
    def read_memory(self, address: int, length: int) -> bytes:
        if length <= 0:
            return b""
        with self._lock:
            data = self._require().memory_read(int(address), int(length))
        return bytes(data)

    def write_memory(self, address: int, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._require().memory_write(int(address), list(bytes(data)))

    def read_gdb_register(self, number: int) -> int:
        if number not in GDB_REGISTERS:
            raise TargetError(f"unsupported GDB register {number}")
        index, _ = GDB_REGISTERS[number]
        with self._lock:
            value = int(self._require().register_read(index)) & 0xFFFFFFFF
        if number == 15:  # Cortex-M is Thumb-only; GDB wants PC bit 0 set.
            value |= 1
        return value

    def write_gdb_register(self, number: int, value: int) -> None:
        if number not in GDB_REGISTERS:
            raise TargetError(f"unsupported GDB register {number}")
        index, _ = GDB_REGISTERS[number]
        value &= 0xFFFFFFFF
        if number == 15:
            value &= ~1
        with self._lock:
            self._require().register_write(index, value)

    def read_gdb_registers(self) -> bytes:
        return b"".join(struct.pack("<I", self.read_gdb_register(n)) for n in GDB_REG_ORDER)

    def write_gdb_registers(self, data: bytes) -> None:
        expected = 4 * len(GDB_REG_ORDER)
        if len(data) != expected:
            raise TargetError(f"register block is {len(data)} bytes, expected {expected}")
        for offset, number in enumerate(GDB_REG_ORDER):
            (value,) = struct.unpack_from("<I", data, offset * 4)
            self.write_gdb_register(number, value)

    def read_named_registers(self, names: Iterable[str] | None = None) -> dict[str, int]:
        wanted = {n.lower() for n in names} if names else None
        out: dict[str, int] = {}
        for number, (_, name) in GDB_REGISTERS.items():
            if wanted is not None and name.lower() not in wanted:
                continue
            out[name] = self.read_gdb_register(number)
        return out

    # ------------------------------------------------------------------
    # breakpoints
    # ------------------------------------------------------------------
    def set_breakpoint(self, address: int) -> int:
        address &= ~1  # Thumb bit is not part of the comparator address
        with self._lock:
            if address in self._breakpoints:
                return self._breakpoints[address]
            handle = int(self._require().hardware_breakpoint_set(address, thumb=True))
            self._breakpoints[address] = handle
            return handle

    def clear_breakpoint(self, address: int) -> None:
        address &= ~1
        with self._lock:
            link = self._require()
            handle = self._breakpoints.pop(address, None)
            if handle is not None:
                try:
                    link.breakpoint_clear(handle)
                except Exception:
                    pass
            # JLINKARM_ClrBP() updates the DLL bookkeeping but does not clear
            # the comparator that was programmed when the breakpoint fired.
            for index in range(FPB_COMP_COUNT):
                try:
                    value = link.memory_read32(FPB_COMP_BASE + 4 * index, 1)[0]
                except Exception:
                    break
                if value & 1 and (value & FPB_ADDR_MASK) == address:
                    link.memory_write32(FPB_COMP_BASE + 4 * index, [0])

    def clear_all_breakpoints(self) -> None:
        with self._lock:
            link = self._require()
            self._breakpoints.clear()
            try:
                link.breakpoint_clear_all()
            except Exception:
                pass
            self._clear_fpb()

    def _clear_fpb(self) -> None:
        """Zero every Cortex-M FPB comparator (single 32-bit writes are required;
        a bulk auto-increment write across the PPB is silently ignored)."""
        link = self._jlink
        if link is None:
            return
        for index in range(FPB_COMP_COUNT):
            try:
                link.memory_write32(FPB_COMP_BASE + 4 * index, [0])
            except Exception:
                break

    @property
    def breakpoints(self) -> dict[int, int]:
        return dict(self._breakpoints)

    # ------------------------------------------------------------------
    # info
    # ------------------------------------------------------------------
    def info(self) -> dict:
        def value(name, default=None):
            attr = getattr(link, name, default)
            return attr() if callable(attr) else attr

        with self._lock:
            link = self._require()
            return {
                "product": value("product_name", "J-Link"),
                "serial": value("serial_number"),
                "firmware": value("firmware_version"),
                "device": self.device,
                "interface": self.interface,
                "speed_khz": self.speed_khz,
            }
