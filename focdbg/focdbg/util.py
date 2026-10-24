"""Small dependency-free helpers."""

from __future__ import annotations

import re
import socket

_INT_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*)$")


def parse_int(text: object) -> int:
    """Parse ``42``, ``0x2a``, ``0b101``, ``0o52`` and ``1_000`` style integers."""
    if isinstance(text, bool):
        raise ValueError(f"not an integer: {text!r}")
    if isinstance(text, int):
        return text
    s = str(text).strip()
    if not _INT_RE.match(s):
        raise ValueError(f"not an integer: {text!r}")
    body = s.replace("_", "")
    try:
        return int(body, 0)
    except ValueError:  # e.g. "010" (leading zero is invalid with base 0)
        return int(body, 10)


def format_addr(value: int, width: int = 8) -> str:
    return f"0x{value:0{width}x}"


def mi_quote(text: str) -> str:
    """Quote a string for the GDB/MI wire format."""
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r")
    return f'"{out}"'


def hexdump(data: bytes, base: int = 0, width: int = 16) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off : off + width]
        groups = []
        for i in range(0, len(chunk), 8):
            groups.append(" ".join(f"{b:02x}" for b in chunk[i : i + 8]))
        hexpart = "  ".join(groups)
        hexpart = hexpart.ljust(width * 3 - 1 + width // 8)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + off:08x}  {hexpart} |{ascii_part}|")
    return "\n".join(lines)


def _port_free(port: int) -> bool:
    # NOTE: no SO_REUSEADDR -- on Windows it would let us bind a port that is
    # already in use, defeating the whole check.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def find_free_ports(start: int, count: int = 1, attempts: int = 200) -> int:
    """Return the first port ``p`` where ``p .. p+count-1`` are all free."""
    for base in range(start, start + attempts):
        if all(_port_free(base + i) for i in range(count)):
            return base
    raise OSError(f"no free TCP port found near {start}")


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"
