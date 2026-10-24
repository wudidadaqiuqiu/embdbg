"""ELF symbol lookup without GDB (for the direct-J-Link sampling mode)."""

from __future__ import annotations

from pathlib import Path

from .errors import ConfigError, TargetError


class ElfSymbols:
    """Name -> address map for OBJECT/FUNC symbols in an ELF file."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        try:
            from elftools.elf.elffile import ELFFile
            from elftools.elf.sections import SymbolTableSection
        except ImportError as exc:  # pragma: no cover
            raise ConfigError("pyelftools is required; run: pip install pyelftools") from exc

        self._symbols: dict[str, int] = {}
        try:
            with open(self.path, "rb") as fh:
                elf = ELFFile(fh)
                for section in elf.iter_sections():
                    if not isinstance(section, SymbolTableSection):
                        continue
                    for sym in section.iter_symbols():
                        name = sym.name
                        if not name:
                            continue
                        value = int(sym["st_value"])
                        if value == 0:
                            continue
                        self._symbols.setdefault(name, value & ~1)
        except OSError as exc:
            raise ConfigError(f"cannot read ELF {self.path}: {exc}") from exc

    def resolve(self, name: str) -> int:
        try:
            return int(str(name), 0)
        except ValueError:
            pass
        address = self._symbols.get(name)
        if address is None:
            # C++ mangled names and local statics are not always present.
            suffix = [k for k in self._symbols if k.endswith(name)]
            if len(suffix) == 1:
                address = self._symbols[suffix[0]]
        if address is None:
            raise TargetError(f"symbol {name!r} not found in {self.path}")
        return address

    @property
    def symbols(self) -> dict[str, int]:
        return dict(self._symbols)
