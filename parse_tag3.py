#!/usr/bin/env python3
"""Parse the CRC16-protected tag3 telemetry frame.

Frame:
    A5 5A | len(=64) | seq | 16 x float (LE, 64 B) | crc16_lo crc16_hi

CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over the first 68 bytes.

Usage:
    python tools/parse_tag3.py --file capture.bin --out tag3.csv
    python tools/parse_tag3.py --port COM9 --baud 2000000 --duration 1 --out tag3.csv
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

FIELDS = [
    "raw", "angle", "ele", "speed", "speed_ref", "iq_ref", "iq", "id",
    "uq", "ud", "position", "ia", "vbus", "fault", "tick", "curflag",
]
SYNC = b"\xA5\x5A"
PAYLOAD = 64
FRAME = 4 + PAYLOAD + 2  # 70


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def parse(buf: bytes):
    rows = []
    bad = 0
    i = 0
    while True:
        j = buf.find(SYNC, i)
        if j < 0:
            break
        if len(buf) - j < FRAME:
            break
        frame = buf[j : j + FRAME]
        if frame[2] != PAYLOAD:
            bad += 1
            i = j + 1
            continue
        crc = frame[68] | (frame[69] << 8)
        if crc16_ccitt(frame[:68]) != crc:
            bad += 1
            i = j + 1
            continue
        rows.append(struct.unpack("<16f", frame[4:68]))
        i = j + FRAME
    return rows, bad


def read_port(port: str, baud: int, duration: float) -> bytes:
    import serial

    s = serial.Serial(port, baud, timeout=0.05)
    s.reset_input_buffer()
    s.write(b"\x03\x03\r\n")  # print_tag = 3
    buf = b""
    t0 = time.time()
    while time.time() - t0 < duration:
        buf += s.read(16384)
    s.close()
    return buf


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="binary capture")
    src.add_argument("--port", help="serial port to capture from")
    ap.add_argument("--baud", type=int, default=2000000)
    ap.add_argument("--duration", type=float, default=1.0)
    ap.add_argument("--out", help="CSV output")
    args = ap.parse_args(argv)

    if args.file:
        buf = open(args.file, "rb").read()
    else:
        buf = read_port(args.port, args.baud, args.duration)

    rows, bad = parse(buf)
    print("bytes=%d frames=%d bad=%d rate=%.1f Hz" % (len(buf), len(rows), bad,
          len(rows) / args.duration if args.port else 0.0), file=sys.stderr)
    if rows:
        print("first:", dict(zip(FIELDS, (round(v, 4) for v in rows[0]))), file=sys.stderr)
        print("last :", dict(zip(FIELDS, (round(v, 4) for v in rows[-1]))), file=sys.stderr)
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            f.write(",".join(FIELDS) + "\n")
            for r in rows:
                f.write(",".join("%.9g" % v for v in r) + "\n")
        print("wrote", args.out, file=sys.stderr)
    return 0 if rows and bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
