# Symbol resolution (`read` / `write` / `hss`)

Everything is resolved from the **ELF/DWARF**; no source parsing, no LLM.

## Pipeline

1. **DWARF (pyelftools)** gives, for every global:
   - its address (`DW_AT_location` / the ELF symbol table);
   - its type graph: structs/unions, member byte and bit offsets, arrays and
     bounds, base types, enums, typedefs, qualifiers.
2. **The expression is walked** against that graph. Member offsets and array
   element sizes come from DWARF, not from the C source.
3. **Pointers are fetched at runtime.** The walker reads the 4-byte pointer
   value through the J-Link DLL, then adds the DWARF member offset of the next
   field. This repeats for every `->` / leading `*`.
4. **The final scalar is read/written** through the J-Link DLL at the resolved
   address. Cortex-M AHB-AP accesses do not stop the core.

The GDB backend is not involved; `read`/`write` never halt the CPU. Only
`serve`/`gdbserver` start GDB (and those do halt for breakpoints).

## Expression grammar

```
expr    := '*'* path [ ':' type ]
path    := NAME accessor*
accessor:= '.' NAME | '[' INT ']' | '->' NAME
type    := u8 i8 u16 i16 u32 i32 u64 i64 f32 f64 float double
```

Examples:

| expression | resolved as |
|---|---|
| `g_state.u32` | global struct member |
| `g_state.inner_arr[2].y` | nested array + struct |
| `g_a.b->c->v` | `b` pointer member → read `b` → member `c` pointer → read `c` → member `v` |
| `*g_state.pf` | read the pointer → dereference to the scalar |
| `0x20000208:f32` | literal address, explicit type |
| `g_state.bits.bit_a` | bit-field: byte address + bit offset/size |

The leading `*` is repeatable (`**pp`). `->` can follow a struct member that is
itself a pointer (`a.b->c`).

## Value handling (write)

The resolved type decides how `--value` is interpreted:

- integers: `int(value, 0)` — decimal, `0x`, `0b`; signed/unsigned width from
  DWARF (`struct.pack`);
- floats: `float(value)` and the IEEE format for `f32`/`f64`;
- bit-fields: read the containing storage, mask and insert
  (`(raw & ~mask) | ((value << bit_offset) & mask)`), write it back;
- after writing, the same field is read back and compared; a mismatch makes the
  command exit non-zero.

## Why not an expression parser for arbitrary C?

Casts, pointer arithmetic and macros would need a full C parser. Two escape
hatches cover the practical cases:

- `0xADDR:type` for a computed address;
- the GDB backend (`serve`) for expressions that need the target's own
  evaluator.

## Limits

- Aggregates are not printed as a whole; use a scalar member or `hss --struct`.
- A pointer target is read once per command; if the firmware changes the
  pointer concurrently, the result is a snapshot.
- No bit-field spanning more than one storage unit beyond what DWARF describes.
- `char[]`/`uint8_t[]` are readable element by element; use `hss` for arrays.
