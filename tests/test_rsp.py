"""RSP server tests with a fake probe (no J-Link, no GDB)."""

from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.rsp import RspServer  # noqa: E402


class FakeProbe:
    def __init__(self):
        self._halted = True
        self.memory = bytearray(b"\x01\x02\x03\x04" + b"\x00" * 12)
        self.breakpoints: dict[int, int] = {}
        self.regs = {n: 0x1000 + n for n in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 25, 26, 27, 28, 29, 30, 31)}
        self.go_calls = 0
        self.step_calls = 0
        self.reset_calls = 0
        self.written: list[tuple[int, bytes]] = []

    @property
    def halted(self):
        return self._halted

    def go(self):
        self.go_calls += 1
        self._halted = True  # stop immediately so tests are fast

    def halt(self):
        self._halted = True

    def step(self):
        self.step_calls += 1
        self._halted = True

    def reset(self, halt=True):
        self.reset_calls += 1
        self._halted = halt

    def read_gdb_register(self, number):
        return self.regs[number]

    def write_gdb_register(self, number, value):
        self.regs[number] = value

    def read_gdb_registers(self):
        import struct

        from embdbg.jlink_probe import GDB_REG_ORDER

        return b"".join(struct.pack("<I", self.regs[n]) for n in GDB_REG_ORDER)

    def read_memory(self, address, length):
        return bytes(self.memory[:length])

    def write_memory(self, address, data):
        self.written.append((address, bytes(data)))

    def set_breakpoint(self, address):
        self.breakpoints[address & ~1] = 1

    def clear_breakpoint(self, address):
        self.breakpoints.pop(address & ~1, None)


def send_packet(sock: socket.socket, payload: str) -> None:
    data = payload.encode("latin-1")
    sock.sendall(b"$" + data + b"#" + f"{sum(data) & 0xFF:02x}".encode())


def recv_packet(sock: socket.socket) -> str:
    buf = b""
    while True:
        buf += sock.recv(1)
        if b"$" in buf and b"#" in buf[buf.index(b"$") :]:
            start = buf.index(b"$")
            end = buf.index(b"#", start)
            if len(buf) >= end + 3:
                return buf[start + 1 : end].decode("latin-1")


class TestRspServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = FakeProbe()
        cls.server = RspServer(cls.probe, host="127.0.0.1", port=0)
        cls.server.start()
        cls.sock = socket.create_connection(("127.0.0.1", cls.server.port), timeout=5)
        send_packet(cls.sock, "qSupported:multiprocess+")
        cls.supported = recv_packet(cls.sock)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sock.close()
        finally:
            cls.server.stop()

    def setUp(self):
        self.probe.breakpoints.clear()
        self.probe.written.clear()
        self.probe.regs = {n: 0x1000 + n for n in self.probe.regs}

    def test_qsupported(self):
        self.assertIn("PacketSize=", self.supported)
        self.assertIn("qXfer:features:read+", self.supported)

    def test_target_xml(self):
        send_packet(self.sock, "qXfer:features:read:target.xml:0,fff")
        reply = recv_packet(self.sock)
        self.assertTrue(reply.startswith("l"))
        self.assertIn("<architecture>arm</architecture>", reply)
        self.assertIn('name="xpsr"', reply)

    def test_g_packet_size(self):
        send_packet(self.sock, "g")
        reply = recv_packet(self.sock)
        self.assertEqual(len(reply), 4 * 2 * len(self.probe.regs))

    def test_memory_read(self):
        send_packet(self.sock, "m20000000,4")
        self.assertEqual(recv_packet(self.sock), "01020304")

    def test_memory_write(self):
        send_packet(self.sock, "M20000000,4:deadbeef")
        self.assertEqual(recv_packet(self.sock), "OK")
        self.assertEqual(self.probe.written[0], (0x20000000, b"\xde\xad\xbe\xef"))

    def test_breakpoint_set_clear(self):
        send_packet(self.sock, "Z1,800b3a9,2")
        self.assertEqual(recv_packet(self.sock), "OK")
        self.assertIn(0x0800B3A8, self.probe.breakpoints)
        send_packet(self.sock, "z1,800b3a9,2")
        self.assertEqual(recv_packet(self.sock), "OK")
        self.assertNotIn(0x0800B3A8, self.probe.breakpoints)

    def test_continue_returns_stop(self):
        send_packet(self.sock, "c")
        reply = recv_packet(self.sock)
        self.assertTrue(reply.startswith("T05"))
        self.assertEqual(self.probe.go_calls, 1)

    def test_step(self):
        send_packet(self.sock, "s")
        self.assertTrue(recv_packet(self.sock).startswith("T05"))
        self.assertEqual(self.probe.step_calls, 1)

    def test_monitor_reset(self):
        send_packet(self.sock, "qRcmd," + "reset".encode().hex())
        self.assertEqual(recv_packet(self.sock), "OK")
        self.assertEqual(self.probe.reset_calls, 1)

    def test_unknown_packet_is_empty(self):
        send_packet(self.sock, "qWhatever")
        self.assertEqual(recv_packet(self.sock), "")


class TestReconnect(unittest.TestCase):
    def test_ack_mode_reset_between_connections(self):
        probe = FakeProbe()
        server = RspServer(probe, host="127.0.0.1", port=0)
        server.start()
        try:
            first = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            send_packet(first, "QStartNoAckMode")
            self.assertEqual(recv_packet(first), "OK")
            first.close()
            time.sleep(0.1)

            second = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            send_packet(second, "qSupported")
            self.assertEqual(second.recv(1), b"+")  # new connection must ack again
            self.assertIn("PacketSize=", recv_packet(second))
            second.close()
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
