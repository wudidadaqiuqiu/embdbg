"""OpenOCD backend tests -- no probe, no hardware, no openocd process.

Two things are covered:

* :class:`embdbg.openocd_probe.OpenOcdProbe` against a fake TCL server, i.e.
  the wire protocol (``0x1a`` framing, ``read_memory`` shapes) that the board
  would otherwise be needed to exercise;
* the backend selection / config plumbing in :mod:`embdbg.backend` and
  :mod:`embdbg.config`.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.backend import JLinkGdbBackend, OpenOcdGdbBackend, create_backend  # noqa: E402
from embdbg.config import Config  # noqa: E402
from embdbg.errors import ConfigError, TargetError  # noqa: E402
from embdbg.openocd import OpenOcdServer  # noqa: E402
from embdbg.openocd_probe import OpenOcdProbe  # noqa: E402

TARGETS_RUNNING = (
    "  TargetName        Type       Endian TapName            State       \n"
    "--  ---------------- ---------- ------ ------------------ -----------\n"
    " 0  stm32h7x.ap2     mem_ap     little stm32h7x.cpu       running     \n"
    " 1* stm32h7x.cpu0    cortex_m   little stm32h7x.cpu       running     \n"
)
TARGETS_HALTED = TARGETS_RUNNING.replace("running     ", "halted      ")


class FakeTcl(threading.Thread):
    """Minimal OpenOCD TCL endpoint: one client, ``0x1a`` framing."""

    daemon = True

    def __init__(self):
        super().__init__(name="fake-tcl")
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.commands: list[str] = []
        self.halted = False
        self._stop = threading.Event()
        self._done = threading.Event()

    # -- fake behaviour -------------------------------------------------
    def reply(self, cmd: str) -> str:
        if cmd == "targets":
            return TARGETS_HALTED if self.halted else TARGETS_RUNNING
        if cmd == "halt":
            self.halted = True
            return ""
        if cmd in ("resume", "reset run"):
            self.halted = False
            return ""
        if cmd == "reset halt":
            self.halted = True
            return ""
        if cmd == "version":
            return "xPack Open On-Chip Debugger 0.12.0"
        if cmd.startswith("read_memory "):
            _, addr, width, count = cmd.split()
            addr, width, count = int(addr, 0), int(width), int(count)
            if width == 32:
                return " ".join(f"0x{0xA0000000 + addr + 4 * i:08x}" for i in range(count))
            return " ".join(f"0x{0x10 + i:02x}" for i in range(count))
        if cmd.startswith("write_memory "):
            return ""
        if cmd.startswith("flash write_image"):
            # real openocd sends the log line and the result as two messages
            return "auto erase enabled\x1awrote 16 bytes from file firmware.bin in 0.1s"
        if cmd.startswith("verify_image"):
            return "verified 16 bytes in 0.1s"
        return ""

    # -- plumbing -------------------------------------------------------
    def run(self) -> None:
        # The probe first probes the port with a short-lived connection and
        # then opens the real one, so serve connections in a loop.
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            self._serve(conn)
        self._done.set()

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                buf += data
                while b"\x1a" in buf:
                    raw, buf = buf.split(b"\x1a", 1)
                    cmd = raw.decode("utf-8", "replace")
                    self.commands.append(cmd)
                    conn.sendall(self.reply(cmd).encode("utf-8") + b"\x1a")
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        self._done.wait(timeout=5)


class TestOpenOcdProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "board.cfg"
        self.cfg.write_text("# fake board config\n", encoding="utf-8")
        self.fake = FakeTcl()
        self.fake.start()
        self.probe = OpenOcdProbe("openocd", self.cfg, tcl_port=self.fake.port)
        self.probe.open()

    def tearDown(self):
        try:
            self.probe.close()
        finally:
            self.fake.close()
            self.tmp.cleanup()

    def test_attaches_to_existing_tcl_port(self):
        self.assertTrue(self.probe.is_open)
        self.assertIsNone(self.probe.process)

    def test_state_parsing(self):
        self.assertTrue(self.probe.is_running)
        self.fake.halted = True
        self.assertFalse(self.probe.is_running)
        self.assertTrue(self.probe.halted)

    def test_read_memory_words(self):
        data = self.probe.read_memory(0x20000000, 8)
        self.assertEqual(len(data), 8)
        self.assertEqual(data[:4], (0xA0000000 + 0x20000000).to_bytes(4, "little"))
        self.assertIn("read_memory 0x20000000 32 2", self.fake.commands)

    def test_read_memory_unaligned_uses_bytes(self):
        data = self.probe.read_memory(0x20000001, 3)
        self.assertEqual(data, bytes([0x10, 0x11, 0x12]))
        self.assertIn("read_memory 0x20000001 8 3", self.fake.commands)

    def test_write_memory_words(self):
        self.probe.write_memory(0x20000000, b"\x01\x02\x03\x04")
        self.assertIn("write_memory 0x20000000 32 {0x04030201}", self.fake.commands)

    def test_write_memory_bytes_when_unaligned(self):
        self.probe.write_memory(0x20000001, b"\xaa\xbb")
        self.assertIn("write_memory 0x20000001 8 {0xaa 0xbb}", self.fake.commands)

    def test_flash_file_bin(self):
        image = Path(self.tmp.name) / "firmware.bin"
        image.write_bytes(b"\x00" * 16)
        written = self.probe.flash_file(image, 0x90000000)
        self.assertEqual(written, 16)
        flash_cmds = [c for c in self.fake.commands if c.startswith("flash write_image")]
        self.assertEqual(len(flash_cmds), 1)
        self.assertIn("0x90000000", flash_cmds[0])
        self.assertTrue(any(c.startswith("verify_image") for c in self.fake.commands))

    def test_flash_file_bin_requires_base(self):
        image = Path(self.tmp.name) / "firmware.bin"
        image.write_bytes(b"\x00" * 4)
        with self.assertRaises(ConfigError):
            self.probe.flash_file(image)

    def test_flash_file_can_skip_verify(self):
        image = Path(self.tmp.name) / "fw2.bin"
        image.write_bytes(b"\x00" * 4)
        self.probe.flash_file(image, 0x90000000, verify=False)
        self.assertFalse(any(c.startswith("verify_image") for c in self.fake.commands))

    def test_flash_command_uses_posix_separators(self):
        # Windows paths contain "\a"/"\b"/"\r"; TCL would eat them as escapes
        image = Path(self.tmp.name) / "fw3.bin"
        image.write_bytes(b"\x00" * 4)
        self.probe.flash_file(image, 0x90000000)
        cmd = [c for c in self.fake.commands if c.startswith("flash write_image")][0]
        self.assertNotIn("\\", cmd)
        self.assertIn("fw3.bin", cmd)

    def test_flash_failure_is_reported(self):
        image = Path(self.tmp.name) / "bad.bin"
        image.write_bytes(b"\x00" * 4)
        self.probe.startup_delay = 0.0
        self.fake.reply = lambda cmd: ""  # openocd says nothing useful
        with self.assertRaises(TargetError):
            self.probe.flash_file(image, 0x90000000)


class TestOpenOcdServer(unittest.TestCase):
    def test_argv_contains_ports_and_config(self):
        server = OpenOcdServer(
            "openocd", "board.cfg", speed_khz=1000, gdb_port=6100, tcl_port=6101
        )
        argv = server.argv()
        self.assertEqual(argv[0], "openocd")
        self.assertEqual(argv[1:3], ["-f", "board.cfg"])
        ports = argv[argv.index("-c") + 1]
        self.assertIn("gdb port 6100", ports)
        self.assertIn("tcl port 6101", ports)
        self.assertIn("telnet port disabled", ports)
        self.assertIn("adapter speed 1000", " ".join(argv))

    def test_info_parses_banner(self):
        server = OpenOcdServer("openocd", "board.cfg", gdb_port=6100, tcl_port=6101)
        server._lines.extend(
            [
                "Info : STLINK V2J48M35 (API v2) VID:PID 0483:374B\n",
                "Info : Target voltage: 3.273887\n",
                "Info : SWD DPIDR 0x6ba02477\n",
                "Info : [stm32h7x.cpu0] Cortex-M7 r1p1 processor detected\n",
            ]
        )
        info = server.info()
        self.assertEqual(info["voltage"], 3.273887)
        self.assertEqual(info["dpidr"], "0x6ba02477")
        self.assertEqual(info["cpu"], "Cortex-M7 r1p1")
        self.assertIn("STLINK", info["firmware"])


class TestOpenOcdBackendSelection(unittest.TestCase):
    def test_factory_accepts_openocd_aliases(self):
        config = Config(openocd_config="board.cfg")
        for name in ("openocd", "openocd-gdb", "OCD", "openocd_gdb"):
            self.assertIsInstance(create_backend(name, config), OpenOcdGdbBackend)

    def test_factory_keeps_jlink(self):
        config = Config(device="STM32G431CB")
        for name in ("jlink", "jlink-gdb", "jlink_gdb"):
            self.assertIsInstance(create_backend(name, config), JLinkGdbBackend)

    def test_factory_rejects_unknown(self):
        with self.assertRaises(ConfigError):
            create_backend("pyocd", Config())

    def test_config_normalises_backend(self):
        config = Config(backend="OpenOCD", openocd_config="board.cfg")
        config.validate()
        self.assertEqual(config.backend, "openocd-gdb")
        self.assertTrue(config.is_openocd)

    def test_config_requires_openocd_config(self):
        config = Config(backend="openocd-gdb")
        with self.assertRaises(ConfigError):
            config.validate()

    def test_openocd_backend_does_not_need_device(self):
        backend = OpenOcdGdbBackend(Config(openocd_config="board.cfg"))
        backend._validate()  # must not raise


class TestOpenOcdBackendInheritsMiLayer(unittest.TestCase):
    """The MI surface must work unchanged for the OpenOCD backend."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_backend import FakeMI

        self.config = Config(backend="openocd-gdb", openocd_config="board.cfg", verify=False)
        self.backend = OpenOcdGdbBackend(self.config)
        self.mi = FakeMI()
        self.backend.mi = self.mi

    def test_read_memory(self):
        self.assertEqual(self.backend.read_memory(0x20000000, 4), b"\x01\x02\x03\x04")

    def test_backtrace(self):
        frames = self.backend.backtrace()
        self.assertEqual(frames[0].func, "main")

    def test_breakpoint_uses_hardware_by_default(self):
        with self.assertRaises(TargetError):  # FakeMI never returns a bkpt record
            self.backend.set_breakpoint("main")
        self.assertTrue(any("-break-insert -f -h" in c for c in self.mi.commands))

    def test_target_info_has_no_serial(self):
        info = self.backend.target_info()
        self.assertIsNone(info.serial)


if __name__ == "__main__":
    unittest.main()
