"""Flash an image with the J-Link DLL and optionally reset/run.

    python examples/flash_reset.py --elf build/Debug/fw.elf --device STM32G431CB \
        --image build/Debug/fw.elf --run
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def run(*args: str) -> int:
    r = subprocess.run([sys.executable, "-m", "embdbg", *args])
    return r.returncode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", required=True, help="ELF with symbols")
    ap.add_argument("--device", required=True)
    ap.add_argument("--image", required=True, help="ELF/HEX/BIN to program")
    ap.add_argument("--run", action="store_true", help="run after flashing")
    a = ap.parse_args()
    common = ["--elf", a.elf, "--device", a.device]
    rc = run("flash", a.image, *common, *( ["--run"] if a.run else ["--no-run"] ))
    if rc == 0 and not a.run:
        rc = run("reset", "--run", *common)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
