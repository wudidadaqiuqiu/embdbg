"""Capture a SEGGER RTT up-channel.

    python examples/rtt_capture.py --elf build/Debug/fw.elf --device STM32G431CB \
        --channel 0 --duration 3 --out rtt.txt
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--channel", type=int, default=0)
    ap.add_argument("--duration", type=float, default=2.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    r = subprocess.run([sys.executable, "-m", "embdbg", "rtt",
                        "--channel", str(a.channel), "--duration", str(a.duration),
                        "--out", a.out, "--elf", a.elf, "--device", a.device])
    raise SystemExit(r.returncode)


if __name__ == "__main__":
    main()
