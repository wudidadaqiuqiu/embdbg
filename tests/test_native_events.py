"""End-to-end MI test with a *native* program under the host gdb.

This exercises the real asynchronous path (``^running`` -> ``*stopped``) that the
J-Link backend relies on, without needing any probe or target hardware.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.mi import MIClient  # noqa: E402

GCC = shutil.which("gcc")
GDB = shutil.which("gdb")

SOURCE = """\
#include <stdio.h>

#ifdef _WIN32
#include <windows.h>
#define WAIT_MS(ms) Sleep(ms)
#else
#include <unistd.h>
#define WAIT_MS(ms) usleep((ms) * 1000)
#endif

static volatile int counter = 0;

int add(int a, int b) {
    return a + b;
}

int main(void) {
    WAIT_MS(3000);
    for (int i = 0; i < 3; i++) {
        counter = add(counter, i);
    }
    printf("%d\\n", counter);
    return 0;
}
"""


@unittest.skipUnless(GCC and GDB, "native gcc + gdb required")
class TestNativeStopEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="embdbg-native-"))
        source = cls.tmp / "prog.c"
        source.write_text(SOURCE, encoding="utf-8")
        cls.exe = cls.tmp / ("prog.exe" if os.name == "nt" else "prog")
        subprocess.run(
            [GCC, "-g", "-O0", "-o", str(cls.exe), str(source)],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.client = MIClient(GDB, [str(self.exe)], timeout=20)
        self.client.launch()

    def tearDown(self):
        self.client.close()

    def test_breakpoint_stop_and_step(self):
        self.client.execute('-break-insert "main"')
        # -exec-run starts a local inferior; the J-Link backend uses
        # -exec-continue because its remote target is already started.
        self.client.execute("-exec-run")
        event = self.client.wait_for_stop(20)
        self.assertEqual(event.reason, "breakpoint-hit")
        self.assertIsNotNone(event.frame)
        self.assertEqual(event.frame.func, "main")
        self.assertFalse(self.client.is_running)

        self.client.execute("-exec-next")
        event = self.client.wait_for_stop(20)
        self.assertEqual(event.reason, "end-stepping-range")

        value = self.client.execute('-data-evaluate-expression "counter"')
        self.assertEqual(str(value.get("value")), "0")

    def test_interrupt_running_target(self):
        import time

        self.client.execute("-exec-run")
        self.assertTrue(self.client.is_running)
        time.sleep(0.3)  # let the inferior enter its 3 s sleep
        self.client.interrupt()
        event = self.client.wait_for_stop(20)
        self.assertIn(event.reason, ("signal-received", "exited-signalled"))

    def test_memory_read_write(self):
        import struct

        self.client.execute('-break-insert "main"')
        self.client.execute("-exec-run")
        self.client.wait_for_stop(20)
        raw = str(self.client.execute('-data-evaluate-expression "&counter"').get("value"))
        address = int(raw.split()[0], 0)
        self.client.execute(f"-data-write-memory-bytes {address:#x} 2a000000")
        record = self.client.execute(f"-data-read-memory-bytes {address:#x} 4")
        contents = record.get("memory")[0]["contents"]
        self.assertEqual(struct.unpack("<i", bytes.fromhex(contents))[0], 42)


if __name__ == "__main__":
    unittest.main()
