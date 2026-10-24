"""Read or write one firmware symbol through the embdbg CLI.

    python examples/read_write.py --elf build/Debug/fw.elf --device STM32G431CB g_state.u32
    python examples/read_write.py ... g_state.u32 --value 0x11223344
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def embdbg(*args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "embdbg", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("expr")
    ap.add_argument("--value", default=None)
    a = ap.parse_args()
    common = ["--elf", a.elf, "--device", a.device, "--json"]
    if a.value is None:
        print(embdbg("read", "--expr", a.expr, *common))
    else:
        print(embdbg("write", "--expr", a.expr, "--value", a.value, *common))


if __name__ == "__main__":
    main()
