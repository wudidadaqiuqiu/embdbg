"""Stream DWARF fields with the J-Link HSS.

    python examples/hss_capture.py --elf build/Debug/fw.elf --device STM32G431CB \
        --rate 1000 --duration 2 --out cap.csv \
        --expr g_motor.state.speed --expr g_motor.state.iq
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--expr", action="append", required=True)
    ap.add_argument("--rate", type=float, default=1000.0)
    ap.add_argument("--duration", type=float, default=2.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    args = ["hss", "--rate", str(a.rate), "--duration", str(a.duration),
            "--out", a.out, "--elf", a.elf, "--device", a.device]
    for e in a.expr:
        args += ["--expr", e]
    r = subprocess.run([sys.executable, "-m", "embdbg", *args])
    raise SystemExit(r.returncode)


if __name__ == "__main__":
    main()
