"""Locate the external tools (GDB, J-Link) and list probes.

On this machine the relevant installations are:
  * ``C:\\ST\\STM32CubeCLT_*\\GNU-tools-for-STM32\\bin\\arm-none-eabi-gdb.exe``
  * ``C:\\Program Files\\SEGGER\\JLink_V*\\JLinkGDBServerCL.exe``
  * ``C:\\Program Files\\SEGGER\\JLink_V*\\JLink.exe``
but nothing here is hard-coded: PATH and explicit config always win.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import ToolNotFoundError
from .models import ProbeInfo

_EMU_RE = re.compile(
    r"J-Link\[(?P<index>\d+)\]:\s*Connection:\s*(?P<conn>[^,]+),\s*"
    r"Serial number:\s*(?P<serial>\d+),\s*ProductName:\s*(?P<product>.+)"
)


def _version_key(path: Path) -> tuple:
    numbers = re.findall(r"\d+", str(path))
    return tuple(int(n) for n in numbers)


def _expand(path: str | os.PathLike) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser()


def _first_file(candidates) -> Path | None:
    for cand in candidates:
        if not cand:
            continue
        p = _expand(cand)
        if p.is_file():
            return p
    return None


def _glob_latest(patterns: list[str]) -> Path | None:
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(Path(p) for p in __import__("glob").glob(os.path.expandvars(pattern)))
    files = [p for p in matches if p.is_file()]
    if not files:
        return None
    files.sort(key=_version_key, reverse=True)
    return files[0]


def find_gdb(explicit: str | None = None) -> Path:
    """Locate an ARM-capable GDB."""
    candidates = [explicit]
    which = shutil.which("arm-none-eabi-gdb") or shutil.which("arm-none-eabi-gdb.exe")
    candidates.append(which)
    candidates.append(
        _glob_latest(
            [
                r"C:\ST\STM32CubeCLT_*\GNU-tools-for-STM32\bin\arm-none-eabi-gdb.exe",
                r"C:\Program Files\STMicroelectronics\STM32CubeCLT_*\GNU-tools-for-STM32\bin\arm-none-eabi-gdb.exe",
                r"C:\Program Files (x86)\GNU Arm Embedded Toolchain\*\bin\arm-none-eabi-gdb.exe",
                r"C:\Program Files\GNU Arm Embedded Toolchain\*\bin\arm-none-eabi-gdb.exe",
                "/usr/bin/arm-none-eabi-gdb",
                "/opt/*/bin/arm-none-eabi-gdb",
            ]
        )
    )
    candidates.append(shutil.which("gdb-multiarch"))
    found = _first_file(candidates)
    if found is None:
        raise ToolNotFoundError(
            "arm-none-eabi-gdb",
            "set --gdb / FOCDBG_GDB, or install STM32CubeCLT",
        )
    return found


def find_jlink_server(explicit: str | None = None, jlink_dir: str | None = None) -> Path:
    """Locate ``JLinkGDBServerCL`` (the console GDB server)."""
    candidates = [explicit]
    if jlink_dir:
        d = _expand(jlink_dir)
        candidates.extend([d / "JLinkGDBServerCL.exe", d / "JLinkGDBServerCL"])
    candidates.extend(
        [
            shutil.which("JLinkGDBServerCL"),
            shutil.which("JLinkGDBServerCL.exe"),
            shutil.which("JLinkGDBServerCLExe"),
        ]
    )
    candidates.append(_glob_latest(_segger_patterns("JLinkGDBServerCL.exe")))
    found = _first_file(candidates)
    if found is None:
        raise ToolNotFoundError(
            "JLinkGDBServerCL",
            "install SEGGER J-Link and set --jlink-server / FOCDBG_JLINK_SERVER",
        )
    return found


def find_jlink_exe(explicit: str | None = None, jlink_dir: str | None = None) -> Path:
    """Locate ``JLink.exe`` (used for probe enumeration)."""
    candidates = [explicit]
    if jlink_dir:
        d = _expand(jlink_dir)
        candidates.extend([d / "JLink.exe", d / "JLinkExe"])
    candidates.extend([shutil.which("JLinkExe"), shutil.which("JLink.exe")])
    candidates.append(_glob_latest(_segger_patterns("JLink.exe")))
    found = _first_file(candidates)
    if found is None:
        raise ToolNotFoundError(
            "JLink.exe",
            "install SEGGER J-Link and set --jlink-exe / FOCDBG_JLINK_EXE",
        )
    return found


def _segger_patterns(executable: str) -> list[str]:
    return [
        rf"C:\Program Files\SEGGER\JLink_V*\{executable}",
        rf"C:\Program Files (x86)\SEGGER\JLink_V*\{executable}",
        rf"/opt/SEGGER/JLink*/{executable}",
    ]


def find_elf(explicit: str | None = None, search_root: str | os.PathLike | None = None) -> Path | None:
    """Find an ELF, preferring an explicit path then ``build/Debug``."""
    if explicit:
        p = _expand(explicit)
        return p if p.is_file() else None
    root = _expand(search_root) if search_root else Path.cwd()
    patterns = [
        "build/Debug/*.elf",
        "build/Release/*.elf",
        "build/*/*.elf",
        "build/*.elf",
        "*.elf",
    ]
    found: list[Path] = []
    for pattern in patterns:
        found = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if found:
            return found[0]
    return None


def list_probes(jlink_exe: str | os.PathLike) -> list[ProbeInfo]:
    """Enumerate connected J-Link probes via ``JLink.exe ShowEmuList``.

    This talks to the USB probe only -- it never connects to the target, so it
    is safe to run while the board is executing.
    """
    exe = _expand(jlink_exe)
    if not exe.is_file():
        raise ToolNotFoundError(str(jlink_exe))
    fd, script = tempfile.mkstemp(prefix="embdbg-emu-", suffix=".jlink")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write("ShowEmuList\nq\n")
        argv = [str(exe)]
        if os.name == "nt":
            argv += ["-NoGui", "1"]
        argv += ["-CommandFile", script]
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        probes = []
        for line in (proc.stdout or "").splitlines():
            m = _EMU_RE.search(line)
            if m:
                probes.append(
                    ProbeInfo(
                        index=int(m.group("index")),
                        serial=m.group("serial"),
                        product=m.group("product").strip(),
                        connection=m.group("conn").strip(),
                    )
                )
        return probes
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


def gdb_version(gdb: str | os.PathLike) -> str | None:
    try:
        proc = subprocess.run(
            [str(gdb), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        first = (proc.stdout or "").splitlines()
        return first[0].strip() if first else None
    except (OSError, subprocess.SubprocessError):
        return None
