"""Unit tests for the read/write resolver and CLI value handling.

The DWARF tests use the firmware ELF from the sibling FOC project when it is
present (the build has EMBDBG_TEST_ENABLE on).  They skip otherwise.
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

from elftools.elf.elffile import ELFFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embdbg.cli import _encode_value, _format_value, _literal_field, _parse_value  # noqa: E402
from embdbg.dwarf_symbols import DwarfSymbols  # noqa: E402

FOC_ELF = Path(
    "C:/docs/uni/research/PAT/foc/foc_open/software/Source_Project/build/Debug/foc.elf"
)


def _mem_reader(path: Path):
    sections = []
    with open(path, "rb") as fh:
        elf = ELFFile(fh)
        for sec in elf.iter_sections():
            addr = int(sec["sh_addr"])
            size = int(sec["sh_size"])
            if not addr or not size or not (sec["sh_flags"] & 0x2):
                continue
            if sec["sh_type"] == "SHT_NOBITS":
                sections.append((addr, b"\x00" * size))
            else:
                sections.append((addr, sec.data()))

    def read(address: int, length: int) -> bytes:
        for base, data in sections:
            if base <= address and address + length <= base + len(data):
                off = address - base
                return data[off:off + length]
        return b"\x00" * length

    return read


class TestLiteral(unittest.TestCase):
    def test_float_literal(self):
        f = _literal_field("0x20000208:float")
        self.assertEqual(f.address, 0x20000208)
        self.assertEqual((f.size, f.sampler_type, f.fmt), (4, "f32", "<f"))

    def test_default_u32(self):
        f = _literal_field("0x20001C74")
        self.assertEqual((f.size, f.sampler_type), (4, "u32"))

    def test_double(self):
        f = _literal_field("0x20000000:f64")
        self.assertEqual((f.size, f.sampler_type), (8, "f64"))

    def test_unknown_type(self):
        with self.assertRaises(Exception):
            _literal_field("0x20000000:u128")

    def test_not_literal(self):
        self.assertIsNone(_literal_field("g_embdbg_test.u32"))


class TestValues(unittest.TestCase):
    def test_parse_int(self):
        f = _literal_field("0x20000000:u32")
        self.assertEqual(_parse_value(f, "305419896"), 305419896)
        self.assertEqual(_parse_value(f, "0xDEADBEEF"), 0xDEADBEEF)

    def test_parse_float(self):
        f = _literal_field("0x20000000:f32")
        self.assertAlmostEqual(_parse_value(f, "3.14159"), 3.14159, places=5)

    def test_encode_int(self):
        f = _literal_field("0x20000000:u32")
        self.assertEqual(_encode_value(f, 0x11223344, None), struct.pack("<I", 0x11223344))

    def test_encode_float(self):
        f = _literal_field("0x20000000:f32")
        self.assertEqual(_encode_value(f, 8.5, None), struct.pack("<f", 8.5))

    def test_encode_bitfield(self):
        from dataclasses import replace
        f = replace(_literal_field("0x20000000:u32"), bit_offset=5, bit_size=3)

        class Probe:
            def read_memory(self, address, length):
                return (0xA5).to_bytes(4, "little")

        data = _encode_value(f, 0b101, Probe())
        self.assertEqual(int.from_bytes(data, "little"), 0xA5 | (0b101 << 5))

    def test_format(self):
        f = _literal_field("0x20000000:u32")
        self.assertIn("305419896", _format_value(f, 305419896))
        self.assertIn("0xDEADBEEF", _format_value(f, 0xDEADBEEF))
        ff = _literal_field("0x20000000:f32")
        self.assertIn("3.141590", _format_value(ff, 3.14159))


@unittest.skipUnless(FOC_ELF.exists(), "firmware ELF not built")
class TestResolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dwarf = DwarfSymbols(FOC_ELF)
        cls.mem = staticmethod(_mem_reader(FOC_ELF))
        try:
            cls.dwarf.resolve_mem("g_embdbg_test.u32")
        except Exception:  # pragma: no cover - fixture disabled
            raise unittest.SkipTest("EMBDBG_TEST_ENABLE fixture not in ELF")

    def read(self, expr):
        field = self.dwarf.resolve_mem(expr, self.mem)
        return field.decode(self.mem(field.read_address, field.read_size), 0)

    def test_scalars(self):
        self.assertEqual(self.read("g_embdbg_test.u8"), 0x5A)
        self.assertEqual(self.read("g_embdbg_test.i16"), -1234)
        self.assertEqual(self.read("g_embdbg_test.u32"), 0xDEADBEEF)
        self.assertAlmostEqual(self.read("g_embdbg_test.f32"), 3.14159, places=4)
        self.assertAlmostEqual(self.read("g_embdbg_test.f64"), 2.718281828, places=6)

    def test_arrays(self):
        self.assertEqual(self.read("g_embdbg_test.arr_u8[0]"), 0)
        self.assertEqual(self.read("g_embdbg_test.arr_u8[15]"), 15)
        self.assertEqual(self.read("g_embdbg_test.arr_i32[3]"), 1300)

    def test_nested(self):
        self.assertEqual(self.read("g_embdbg_test.inner.x"), 0x1234)
        self.assertEqual(self.read("g_embdbg_test.inner.y"), -5)
        self.assertEqual(self.read("g_embdbg_test.inner_arr[2].x"), 0x0303)
        self.assertEqual(self.read("g_embdbg_test.inner_arr[2].y"), 6)

    def test_bitfields(self):
        self.assertEqual(self.read("g_embdbg_test.bits.bit_a"), 0x15)
        self.assertEqual(self.read("g_embdbg_test.bits.bit_b"), 0x5)
        self.assertEqual(self.read("g_embdbg_test.bits.bit_c"), 0xABCDEF)

    def test_pointers(self):
        self.assertEqual(self.read("g_embdbg_test.pf"), self.dwarf.variable_address("g_embdbg_float"))
        self.assertEqual(self.read("g_embdbg_test.p_inner->x"), 0x1234)
        self.assertEqual(self.read("g_embdbg_test.p_arr[1]->y"), -6)

    def test_deref(self):
        self.assertAlmostEqual(self.read("*g_embdbg_test.pf"), 1.234, places=3)
        self.assertEqual(self.read("*g_embdbg_test.pi"), -777)

    def test_chain(self):
        self.assertEqual(self.read("g_embdbg_a.b->c->v"), 0xC0DE1234)
        self.assertEqual(self.read("g_embdbg_a.b->w"), 0xBEEF)
        self.assertEqual(self.read("g_embdbg_a.b->c->z"), -9)

    def test_literal_roundtrip(self):
        addr = self.dwarf.variable_address("g_embdbg_float")
        f = _literal_field(f"0x{addr:x}:float")
        self.assertAlmostEqual(f.decode(self.mem(f.read_address, f.read_size), 0), 1.234, places=3)


if __name__ == "__main__":
    unittest.main()
