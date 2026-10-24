"""DWARF-based symbol and struct-field resolution.

The ELF symbol table only names top-level objects (``sys``, ``ec``, ...).
High-speed (HSS) observation, however, usually targets a *field* of a
structure: ``sys.foc_controller.speed``.  That needs type information:

    variable -> struct -> member offset -> member type -> ...

This module reads ``.debug_info`` (DWARF 4/5) with pyelftools and resolves
such an expression to an absolute address plus a scalar type/size, so the
sampler can read one contiguous block and decode each field without the user
hand-writing ``name:type@offset`` layouts.

Supported expression syntax::

    sys
    sys.foc_controller
    sys.foc_controller.angle
    sys.pid_speed.kp
    sys.cfg.enc_lut_a[2]
    sys.fault_status.serious_fault.bit.motor_cfg_err   # bit-field

The resolver is read-only and does not need a connected target.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .errors import ConfigError, TargetError

# Qualifiers that do not change the memory representation.
_QUALIFIERS = {
    "DW_TAG_typedef",
    "DW_TAG_const_type",
    "DW_TAG_volatile_type",
    "DW_TAG_restrict_type",
    "DW_TAG_atomic_type",
}

# DW_AT_encoding values (DWARF 5, section 7.8).
_ENC_ADDRESS = 0x01
_ENC_BOOLEAN = 0x02
_ENC_COMPLEX_FLOAT = 0x03
_ENC_FLOAT = 0x04
_ENC_SIGNED = 0x05
_ENC_SIGNED_CHAR = 0x06
_ENC_UNSIGNED = 0x07
_ENC_UNSIGNED_CHAR = 0x08
_ENC_UTF = 0x10

# DW_OP values used by simple location expressions.
_DW_OP_ADDR = 0x03
_DW_OP_CONSTU = 0x10
_DW_OP_PLUS_UCONST = 0x23


@dataclass(frozen=True)
class ResolvedField:
    """A scalar field resolved to an absolute address and a decoder type."""

    path: str
    address: int
    size: int  # storage size in bytes
    type_name: str
    sampler_type: str  # f32/f64/u8/i8/u16/i16/u32/i32/u64/i64
    fmt: str  # struct format string, e.g. "<f"
    signed: bool = False
    bit_offset: int | None = None  # bit position within ``address`` (0..7)
    bit_size: int | None = None
    enumerators: tuple[tuple[str, int], ...] = ()

    @property
    def read_address(self) -> int:
        return self.address

    @property
    def read_size(self) -> int:
        if self.bit_offset is None:
            return self.size
        return max(1, (self.bit_offset + (self.bit_size or 0) + 7) // 8)

    def decode(self, data: bytes, offset: int) -> float | int:
        """Decode this field from a block whose start is ``offset`` bytes back."""
        if self.bit_offset is not None:
            need = self.read_size
            raw = data[offset : offset + need]
            if len(raw) < need:
                raise TargetError(f"short read for {self.path}")
            value = int.from_bytes(raw, "little") >> self.bit_offset
            value &= (1 << (self.bit_size or 1)) - 1
            if self.signed and (value & (1 << ((self.bit_size or 1) - 1))):
                value -= 1 << (self.bit_size or 1)
            return value
        if offset + self.size > len(data):
            raise TargetError(f"short read for {self.path}")
        return struct.unpack_from(self.fmt, data, offset)[0]


@dataclass
class StructMember:
    """A named scalar leaf of a struct/union."""

    name: str
    offset: int  # byte offset from the struct base (bit-field: containing byte)
    type_name: str
    size: int
    kind: str
    bit_offset: int | None = None  # bit position inside ``offset`` (0..7)
    bit_size: int | None = None
    signed: bool = False
    sampler_type: str | None = None
    enumerators: tuple[tuple[str, int], ...] = ()


@dataclass
class _MemberSpec:
    """Internal: a member location relative to the struct being searched."""

    name: str
    byte_offset: int
    type_die: object
    type_name: str
    size: int
    bit_offset: int | None = None  # absolute bit offset from the struct base
    bit_size: int | None = None
    signed: bool = False
    enumerators: tuple[tuple[str, int], ...] = ()


@dataclass
class _Var:
    cu: object
    die: object
    declaration: bool
    address: int | None


def _attr_value(die, name: str):
    attr = die.attributes.get(name)
    return attr.value if attr is not None else None


def _die_name(die) -> str:
    value = _attr_value(die, "DW_AT_name")
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _member_byte_offset(die) -> int:
    value = _attr_value(die, "DW_AT_data_member_location")
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        offset = 0
        i = 0
        while i < len(value):
            op = value[i]
            if op in (_DW_OP_PLUS_UCONST, _DW_OP_CONSTU) and i + 1 < len(value):
                offset += int(value[i + 1])
                i += 2
            else:
                i += 1
        return offset
    return 0


def _dw_bit_offset(die, member_offset: int, storage_size: int) -> int | None:
    """Bit offset of a bit-field relative to its containing struct."""
    value = _attr_value(die, "DW_AT_data_bit_offset")
    if isinstance(value, int):
        return value
    legacy = _attr_value(die, "DW_AT_bit_offset")
    bit_size = _attr_value(die, "DW_AT_bit_size")
    if isinstance(legacy, int) and isinstance(bit_size, int):
        return member_offset * 8 + max(0, storage_size * 8 - legacy - bit_size)
    return None


class DwarfSymbols:
    """Read-only DWARF resolver for globals and scalar members."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        try:
            from elftools.elf.elffile import ELFFile
            from elftools.elf.sections import SymbolTableSection
        except ImportError as exc:  # pragma: no cover - dependency driven
            raise ConfigError("pyelftools is required: pip install pyelftools") from exc

        self._elf_symbols: dict[str, int] = {}
        self._vars: dict[str, list[_Var]] = {}
        self._dies: dict[int, tuple[object, object]] = {}
        self._dwarf = None
        self._pointer_size = 4
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
                        self._elf_symbols.setdefault(name, value & ~1)
                if not elf.has_dwarf_info():
                    raise ConfigError(f"{self.path} has no DWARF debug information")
                self._dwarf = elf.get_dwarf_info()
                for cu in self._dwarf.iter_CUs():
                    for die in cu.iter_DIEs():
                        self._dies[die.offset] = (cu, die)
                        if die.tag != "DW_TAG_variable":
                            continue
                        name = _die_name(die)
                        if not name:
                            continue
                        self._vars.setdefault(name, []).append(
                            _Var(
                                cu=cu,
                                die=die,
                                declaration="DW_AT_declaration" in die.attributes,
                                address=self._location_address(cu, die),
                            )
                        )
        except OSError as exc:
            raise ConfigError(f"cannot read ELF {self.path}: {exc}") from exc

    # ------------------------------------------------------------------
    # DWARF primitives
    # ------------------------------------------------------------------
    def _die_at(self, offset: int):
        item = self._dies.get(offset)
        return item[1] if item else None

    def _type_ref(self, cu, die):
        if die is None:
            return None
        attr = die.attributes.get("DW_AT_type")
        if attr is None:
            return None
        value = int(attr.value)
        if attr.form == "DW_FORM_ref_addr":
            return self._die_at(value)
        return self._die_at(int(cu.cu_offset) + value)

    def _type(self, cu, die):
        """Follow typedef/qualifier DIEs to the underlying type DIE."""
        seen = 0
        while die is not None and die.tag in _QUALIFIERS:
            die = self._type_ref(cu, die)
            seen += 1
            if seen > 32:  # malformed DWARF guard
                break
        return die

    def _type_name(self, cu, die) -> str:
        die = self._type(cu, die)
        if die is None:
            return "void"
        name = _die_name(die)
        if name:
            return name
        if die.tag == "DW_TAG_pointer_type":
            return self._type_name(cu, self._type_ref(cu, die)) + " *"
        if die.tag == "DW_TAG_array_type":
            return self._type_name(cu, self._type_ref(cu, die)) + "[]"
        if die.tag == "DW_TAG_structure_type":
            return "struct"
        if die.tag == "DW_TAG_union_type":
            return "union"
        if die.tag == "DW_TAG_enumeration_type":
            return "enum"
        return die.tag

    def _type_size(self, cu, die) -> int:
        die = self._type(cu, die)
        if die is None:
            return 0
        size = _attr_value(die, "DW_AT_byte_size")
        if isinstance(size, int):
            return size
        if die.tag == "DW_TAG_pointer_type":
            return self._pointer_size
        if die.tag == "DW_TAG_array_type":
            return self._type_size(cu, self._type_ref(cu, die)) * self._array_count(cu, die)
        return 0

    def _array_count(self, cu, die) -> int:
        count = 1
        for child in die.iter_children():
            if child.tag != "DW_TAG_subrange_type":
                continue
            n = _attr_value(child, "DW_AT_count")
            if not isinstance(n, int):
                upper = _attr_value(child, "DW_AT_upper_bound")
                n = int(upper) + 1 if isinstance(upper, int) else 0
            count *= int(n) if isinstance(n, int) and n > 0 else 1
        return count

    @staticmethod
    def _fmt_for(size: int, signed: bool) -> str:
        return {
            (1, True): "<b",
            (1, False): "<B",
            (2, True): "<h",
            (2, False): "<H",
            (4, True): "<i",
            (4, False): "<I",
            (8, True): "<q",
            (8, False): "<Q",
        }.get((size, signed), "<I")

    def _scalar(self, cu, die) -> tuple[str, str, bool, int, tuple[tuple[str, int], ...]]:
        """Return (sampler_type, fmt, signed, size, enumerators)."""
        die = self._type(cu, die)
        if die is None:
            raise TargetError("cannot resolve type")
        size = self._type_size(cu, die)
        if die.tag == "DW_TAG_enumeration_type":
            enumerators: list[tuple[str, int]] = []
            for child in die.iter_children():
                if child.tag == "DW_TAG_enumerator":
                    value = _attr_value(child, "DW_AT_const_value")
                    if isinstance(value, int):
                        enumerators.append((_die_name(child), value))
            signed = any(v < 0 for _, v in enumerators)
            sampler = ("i" if signed else "u") + str(max(1, size) * 8)
            return sampler, self._fmt_for(size, signed), signed, size, tuple(enumerators)
        if die.tag == "DW_TAG_base_type":
            encoding = _attr_value(die, "DW_AT_encoding")
            if encoding == _ENC_FLOAT and size == 4:
                return "f32", "<f", True, 4, ()
            if encoding == _ENC_FLOAT and size == 8:
                return "f64", "<d", True, 8, ()
            if encoding == _ENC_BOOLEAN:
                return "u8", "<B", False, 1, ()
            signed = encoding in (_ENC_SIGNED, _ENC_SIGNED_CHAR)
            sampler = ("i" if signed else "u") + str(max(1, size) * 8)
            return sampler, self._fmt_for(size, signed), signed, size, ()
        if die.tag == "DW_TAG_pointer_type":
            sampler = f"u{self._pointer_size * 8}"
            return sampler, self._fmt_for(self._pointer_size, False), False, self._pointer_size, ()
        raise TargetError(f"type {self._type_name(cu, die)!r} is not scalar")

    # ------------------------------------------------------------------
    # variables / locations
    # ------------------------------------------------------------------
    def _location_address(self, cu, die) -> int | None:
        value = _attr_value(die, "DW_AT_location")
        if not isinstance(value, (list, tuple)) or not value:
            return None
        if value[0] == _DW_OP_ADDR and len(value) > 1:
            address = int(value[1])
            offset = 0
            i = 2
            while i + 1 < len(value):
                if value[i] == _DW_OP_PLUS_UCONST:
                    offset += int(value[i + 1])
                    i += 2
                else:
                    i += 1
            return address + offset
        return None

    def _pick_variable(self, name: str) -> _Var:
        candidates = self._vars.get(name)
        if not candidates:
            raise TargetError(f"variable {name!r} not found in DWARF of {self.path}")
        for var in candidates:
            if not var.declaration and var.address is not None:
                return var
        for var in candidates:
            if var.address is not None:
                return var
        for var in candidates:
            if not var.declaration:
                return var
        return candidates[0]

    def variable_address(self, name: str) -> int:
        if name in self._elf_symbols:
            return int(self._elf_symbols[name])
        var = self._pick_variable(name)
        if var.address is not None:
            return int(var.address)
        raise TargetError(
            f"no address for {name!r}: not in the symbol table and DWARF has no static location"
        )

    # ------------------------------------------------------------------
    # members
    # ------------------------------------------------------------------
    def _member_specs(self, cu, struct_die, *, _base_byte: int = 0, _base_bit: int = 0,
                      _depth: int = 0) -> Iterator[_MemberSpec]:
        """Flatten direct members, descending through anonymous aggregates.

        Offsets in the yielded specs are relative to the struct passed in on
        the first call.
        """
        struct_die = self._type(cu, struct_die)
        if struct_die is None or _depth > 16:
            return
        if struct_die.tag not in ("DW_TAG_structure_type", "DW_TAG_union_type"):
            return
        for child in struct_die.iter_children():
            if child.tag != "DW_TAG_member":
                continue
            name = _die_name(child)
            offset = _member_byte_offset(child)
            mtype = self._type_ref(cu, child)
            msize = self._type_size(cu, mtype)
            bit_size = _attr_value(child, "DW_AT_bit_size")
            underlying = self._type(cu, mtype)
            if isinstance(bit_size, int):
                rel_bit = _dw_bit_offset(child, offset, msize)
                if rel_bit is None:
                    continue
                abs_bit = _base_bit + int(rel_bit)
                if not name:
                    continue
                yield _MemberSpec(
                    name=name,
                    byte_offset=_base_byte + abs_bit // 8,
                    type_die=mtype,
                    type_name=self._type_name(cu, mtype),
                    size=msize,
                    bit_offset=abs_bit % 8,
                    bit_size=int(bit_size),
                    # Recover the signedness of the bit-field type.
                    signed=self._scalar_signed(cu, mtype),
                )
                continue
            child_byte = _base_byte + offset
            if name:
                yield _MemberSpec(
                    name=name,
                    byte_offset=child_byte,
                    type_die=mtype,
                    type_name=self._type_name(cu, mtype),
                    size=msize,
                )
            elif underlying is not None and underlying.tag in (
                "DW_TAG_structure_type",
                "DW_TAG_union_type",
            ):
                yield from self._member_specs(
                    cu,
                    underlying,
                    _base_byte=child_byte,
                    _base_bit=(child_byte * 8),
                    _depth=_depth + 1,
                )

    def _scalar_signed(self, cu, die) -> bool:
        try:
            return bool(self._scalar(cu, die)[2])
        except TargetError:
            return False

    def _find_member(self, cu, struct_die, name: str) -> _MemberSpec:
        for spec in self._member_specs(cu, struct_die):
            if spec.name == name:
                return spec
        raise TargetError(f"member {name!r} not found in {self._type_name(cu, struct_die)}")

    def _materialize(self, cu, spec: _MemberSpec, var_address: int, struct_bit: int) -> ResolvedField:
        if spec.bit_offset is not None:
            # ``spec.byte_offset`` is the byte containing the bit-field; the
            # containing struct begins at ``struct_bit`` bits from var_address.
            address = var_address + struct_bit // 8 + 0  # marker, replaced below
            # spec.byte_offset was computed relative to the searched struct,
            # so its absolute byte address is var_address + struct_bit//8 +
            # spec.byte_offset (when the searched struct is byte-aligned).
            address = var_address + struct_bit // 8 + spec.byte_offset - 0
            bit = spec.bit_offset
            # If the struct itself starts at a non-zero bit, keep the bit
            # position consistent; structs are always byte-aligned in C.
            sampler, fmt, signed, _size, enums = self._scalar(cu, spec.type_die)
            return ResolvedField(
                path="",
                address=address,
                size=spec.size,
                type_name=spec.type_name,
                sampler_type=sampler,
                fmt=fmt,
                signed=signed,
                bit_offset=bit,
                bit_size=spec.bit_size,
                enumerators=enums,
            )
        sampler, fmt, signed, size, enums = self._scalar(cu, spec.type_die)
        return ResolvedField(
            path="",
            address=var_address + struct_bit // 8 + spec.byte_offset,
            size=size,
            type_name=spec.type_name,
            sampler_type=sampler,
            fmt=fmt,
            signed=signed,
            enumerators=enums,
        )

    # ------------------------------------------------------------------
    # expression parsing / resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_expr(expr: str):
        """Split ``a.b[2].c`` into a base name and accessor tokens."""
        text = expr.strip()
        if not text:
            raise ConfigError("empty expression")
        i = 0
        while i < len(text) and (text[i].isalnum() or text[i] in "_$"):
            i += 1
        if i == 0:
            raise ConfigError(f"invalid expression {expr!r}")
        base = text[:i]
        tokens: list[tuple[str, object]] = []
        while i < len(text):
            ch = text[i]
            if ch.isspace():
                i += 1
            elif ch == ".":
                i += 1
                j = i
                while j < len(text) and (text[j].isalnum() or text[j] in "_$"):
                    j += 1
                if j == i:
                    raise ConfigError(f"invalid member access in {expr!r}")
                tokens.append(("member", text[i:j]))
                i = j
            elif ch == "[":
                j = text.find("]", i)
                if j < 0:
                    raise ConfigError(f"unclosed '[' in {expr!r}")
                try:
                    value = int(text[i + 1 : j].strip(), 0)
                except ValueError as exc:
                    raise ConfigError(f"array index must be an integer in {expr!r}") from exc
                tokens.append(("index", value))
                i = j + 1
            else:
                raise ConfigError(f"unexpected {ch!r} in expression {expr!r}")
        return base, tokens

    def _walk(self, expr: str):
        """Return (var_address, cu, struct_bit, type_die, path)."""
        base, tokens = self._parse_expr(expr)
        var = self._pick_variable(base)
        cu = var.cu
        die = self._type_ref(cu, var.die)
        var_address = self.variable_address(base)
        struct_bit = 0
        path = base
        for kind, value in tokens:
            die = self._type(cu, die)
            if die is None:
                raise TargetError(f"cannot resolve {path}")
            if kind == "member":
                if die.tag not in ("DW_TAG_structure_type", "DW_TAG_union_type"):
                    raise TargetError(f"{path} is not a structure (cannot access .{value})")
                spec = self._find_member(cu, die, str(value))
                if spec.bit_offset is not None:
                    if (kind, value) != tokens[-1]:
                        raise TargetError(f"{path}.{value} is a bit-field and has no members")
                    abs_bit = struct_bit + spec.byte_offset * 8 + spec.bit_offset
                    path += "." + str(value)
                    field = self._materialize(cu, spec, var_address, struct_bit)
                    return (
                        var_address,
                        cu,
                        abs_bit,
                        spec.type_die,
                        path,
                        field,
                    )
                struct_bit += spec.byte_offset * 8
                die = spec.type_die
                path += "." + str(value)
            elif kind == "index":
                if die.tag != "DW_TAG_array_type":
                    raise TargetError(f"{path} is not an array")
                element = self._type_ref(cu, die)
                count = self._array_count(cu, die)
                index = int(value)  # type: ignore[arg-type]
                if index < 0 or index >= count:
                    raise TargetError(f"index {index} out of range for {path}[{count}]")
                struct_bit += index * self._type_size(cu, element) * 8
                die = element
                path += f"[{index}]"
            else:  # pragma: no cover - parser emits only the above
                raise ConfigError(f"unsupported token {kind!r}")
        return var_address, cu, struct_bit, die, path, None

    def resolve(self, expr: str) -> ResolvedField:
        var_address, cu, struct_bit, die, path, bitfield = self._walk(expr)
        if bitfield is not None:
            return ResolvedField(
                path=path,
                address=bitfield.address,
                size=bitfield.size,
                type_name=bitfield.type_name,
                sampler_type=bitfield.sampler_type,
                fmt=bitfield.fmt,
                signed=bitfield.signed,
                bit_offset=bitfield.bit_offset,
                bit_size=bitfield.bit_size,
                enumerators=bitfield.enumerators,
            )
        die = self._type(cu, die)
        sampler, fmt, signed, size, enums = self._scalar(cu, die)
        return ResolvedField(
            path=path,
            address=var_address + struct_bit // 8,
            size=size,
            type_name=self._type_name(cu, die),
            sampler_type=sampler,
            fmt=fmt,
            signed=signed,
            enumerators=enums,
        )

    def expand(self, expr: str, *, recursive: bool = False) -> list[ResolvedField]:
        """Expand a struct expression into scalar fields (bit-fields included)."""
        var_address, cu, struct_bit, die, path, _bitfield = self._walk(expr)
        die = self._type(cu, die)
        return list(self._expand(cu, die, path, var_address, struct_bit, recursive, 0))

    def _expand(self, cu, die, path: str, var_address: int, struct_bit: int,
                recursive: bool, depth: int) -> Iterator[ResolvedField]:
        die = self._type(cu, die)
        if die is None or depth > 16:
            return
        if die.tag not in ("DW_TAG_structure_type", "DW_TAG_union_type"):
            # Scalar target: expand() on a scalar is just one field.
            try:
                field = self.resolve(path)
            except TargetError:
                return
            yield field
            return
        for spec in self._member_specs(cu, die):
            child_path = f"{path}.{spec.name}" if path else spec.name
            child_bit = struct_bit + spec.byte_offset * 8
            if spec.bit_offset is not None:
                field = self._materialize(cu, spec, var_address, struct_bit)
                yield ResolvedField(
                    path=child_path,
                    address=field.address,
                    size=field.size,
                    type_name=field.type_name,
                    sampler_type=field.sampler_type,
                    fmt=field.fmt,
                    signed=field.signed,
                    bit_offset=field.bit_offset,
                    bit_size=field.bit_size,
                    enumerators=field.enumerators,
                )
            else:
                underlying = self._type(cu, spec.type_die)
                if underlying is not None and underlying.tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
                    if recursive:
                        yield from self._expand(
                            cu, underlying, child_path, var_address, child_bit, recursive, depth + 1
                        )
                elif underlying is not None and underlying.tag == "DW_TAG_array_type":
                    if recursive:
                        element = self._type_ref(cu, underlying)
                        count = self._array_count(cu, underlying)
                        elem_size = self._type_size(cu, element)
                        for i in range(count):
                            yield from self._expand(
                                cu, element, f"{child_path}[{i}]", var_address,
                                child_bit + i * elem_size * 8, recursive, depth + 1
                            )
                else:
                    try:
                        sampler, fmt, signed, size, enums = self._scalar(cu, spec.type_die)
                    except TargetError:
                        continue
                    yield ResolvedField(
                        path=child_path,
                        address=var_address + child_bit // 8,
                        size=size,
                        type_name=spec.type_name,
                        sampler_type=sampler,
                        fmt=fmt,
                        signed=signed,
                        enumerators=enums,
                    )

    # ------------------------------------------------------------------
    def describe(self, expr: str) -> str:
        """Human-readable layout for ``hss --list-fields``."""
        var_address, cu, struct_bit, die, path, bitfield = self._walk(expr)
        die = self._type(cu, die)
        if die is None:
            return f"{path}: <unresolved>"
        address = var_address + struct_bit // 8
        lines = [
            f"{path} = {self._type_name(cu, die)} ({self._type_size(cu, die)} bytes) @ 0x{address:08X}"
        ]
        if die.tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
            for spec in self._member_specs(cu, die):
                if spec.bit_offset is not None:
                    lines.append(
                        f"  {spec.name:<24} +{spec.byte_offset:<4} bit{spec.bit_offset}.."
                        f"{spec.bit_offset + (spec.bit_size or 0) - 1:<3} {spec.type_name}"
                    )
                else:
                    try:
                        sampler = self._scalar(cu, spec.type_die)[0]
                    except TargetError:
                        sampler = self._type(cu, spec.type_die).tag.replace("DW_TAG_", "")
                    lines.append(
                        f"  {spec.name:<24} +{spec.byte_offset:<4} {sampler:<8} {spec.type_name}"
                    )
        else:
            try:
                sampler, _fmt, _signed, size, _enums = self._scalar(cu, die)
                lines.append(f"  scalar  {sampler}  size={size}")
            except TargetError:
                pass
        return "\n".join(lines)


__all__ = ["DwarfSymbols", "ResolvedField", "StructMember"]
