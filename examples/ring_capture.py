"""Drain a firmware ring buffer through the embdbg CLI.

Prerequisite (firmware, ~10 lines): see examples/firmware/trace_ring.h -- a
record array plus a monotonic u32 head counter.

    python examples/ring_capture.py --elf build/Debug/fw.elf \
        --buffer g_trace_buf --head g_trace_head \
        --fields "t:u32,iq:i32,id:i32" --duration 5 --out trace.csv

Unlike hss (one USB round trip per sample, ~1.5 kHz ceiling), this reads whole
blocks: the firmware sets the sampling rate, the host just drains it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def embdbg(*args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "embdbg", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", required=True)
    ap.add_argument("--buffer", required=True, help="trace buffer symbol")
    ap.add_argument("--head", required=True, help="u32 head counter symbol")
    ap.add_argument("--fields", required=True, help="t:u32,iq:i32,id:i32")
    ap.add_argument("--rate", type=float, default=10.0, help="polls per second")
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--out", default="trace.csv")
    args = ap.parse_args()

    embdbg(
        "ring",
        "--elf", args.elf,
        "--buffer", args.buffer,
        "--head", args.head,
        "--fields", args.fields,
        "--rate", str(args.rate),
        "--duration", str(args.duration),
        "--out", args.out,
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
