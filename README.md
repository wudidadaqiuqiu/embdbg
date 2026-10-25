# embdbg

English | [简体中文](README.zh-CN.md)

A scriptable **J-Link debug backend for embedded targets** (Cortex-M). The CLI is a thin
layer over `embdbg.DebugSession`; the same API can be reused by a GUI or test scripts.

```text
your CLI / GUI / pytest
      │ Python API
      ▼
embdbg.DebugSession ──► Backend (pluggable)
                          │
                  JLinkGdbBackend          JLinkProbe (DLL)
                  ├─ JLinkGDBServerCL.exe  ├─ memory read/write
                  ├─ arm-none-eabi-gdb     ├─ HSS
                  └─ GDB/MI                ├─ RTT
                                           └─ flash
```

## Features

- **read / write** symbols with fields, arrays, bit-fields and chained struct pointers
  (`g_state.counter`, `cfg.tbl[3]`, `a->b->c`, `*p`, `0x20000208:float`);
- **HSS** high-speed sampling of DWARF fields or whole structs (no halt);
- **RTT** up-channel read (SEGGER Real Time Transfer);
- **flash** with the J-Link DLL (fast, like Ozone) or the GDB-server path;
- **serve / gdbserver** GDB RSP server for Ozone or plain `arm-none-eabi-gdb`
  (breakpoints, stepping, live sampling);
- **serial** helper and a small plugin system;
- no LLM, no source parsing: all symbol/type information comes from the ELF/DWARF.

## Documentation

- [Flash: J-Link DLL vs GDB server, reset clock](docs/flash.md)
- [Symbol resolution: DWARF, pointers, literals](docs/symbols.md)
- [HSS: field layout and rate](docs/hss.md)
- [RTT: firmware integration and tool](docs/rtt.md)

## Install

```bash
pip install embdbg                   # base (GDB backend)
pip install "embdbg[serve,serial]"   # pylink-square + pyelftools + pyserial
```

Requirements:

- **OS: Windows only (currently).** Windows 10/11 x64 is the supported
  platform; Linux and macOS are not supported yet because the backend drives
  `JLinkGDBServerCL.exe` / `JLink.exe` and the Windows J-Link DLL.
- Python ≥ 3.10.
- GNU Arm toolchain (`arm-none-eabi-gdb`) — GDB backend, `serve`;
- SEGGER J-Link software (`JLinkGDBServerCL.exe`, `JLink.exe`) — probe access;
- A J-Link probe and the target board.

## Quick start

```bash
embdbg doctor                    # toolchain / config / probe check
embdbg probes                    # list J-Link probes (USB only, board may run)
embdbg info                      # connect and print target info

embdbg read  --expr "g_state.counter"
embdbg write --expr "g_state.counter" --value 42
embdbg read  --expr "g_a.b->c->v"
embdbg read  --expr "*g_state.pf"
embdbg read  --expr "0x20000208:float"

embdbg flash build/Debug/firmware.elf --no-run     # J-Link DLL, ~seconds
embdbg reset --run

embdbg hss --expr "g_state.counter" --rate 1000 --duration 2 --out cap.csv
embdbg rtt --channel 0 --duration 2

embdbg serve                     # Ozone / gdb: target extended-remote :2331
```

## Configuration

Copy [`embdbg.toml`](embdbg.toml) into your firmware repository and edit it; the
CLI looks for `embdbg.toml` in the current directory or `EMBDBG_CONFIG`.
Every option can be overridden on the command line.

```toml
[embdbg]
device = "STM32G431CB"           # J-Link device name
interface = "SWD"                # SWD | JTAG
speed_khz = 4000

run_after_flash = false          # default for `flash --run/--no-run`
elf = "build/Debug/firmware.elf" # DWARF symbols
verify = true                    # flash verify

# optional tool paths (auto-discovered when empty)
# gdb = "C:/path/to/arm-none-eabi-gdb.exe"
# jlink_server = "C:/Program Files/SEGGER/JLink/JLinkGDBServerCL.exe"
# jlink_exe = "C:/Program Files/SEGGER/JLink/JLink.exe"
# base_port = 2331
# probe_serial = "123456789"
singlerun = true
```

Common flags (available on most subcommands):

| flag | meaning |
|---|---|
| `--config PATH` | config file |
| `--device NAME` | J-Link device name |
| `--interface SWD\|JTAG` | debug interface |
| `--speed KHZ` | SWD/JTAG clock |
| `--elf PATH` | ELF with DWARF information |
| `--probe-serial SN` | select a J-Link by serial |
| `--json` | machine-readable output |
| `-v/--verbose` | diagnostics |

## Commands

### probes / doctor / info

```bash
embdbg probes            # list probes (alias: list)
embdbg doctor            # toolchain, config, ELF, probe
embdbg info [--json]     # connect, print target/register/session info
```

### read / write

Resolve an expression against the ELF DWARF, then read/write through the J-Link
DLL. **Symbol reads do not halt the core**; pointer targets are fetched through
the DLL.

Expression forms:

| expression | meaning |
|---|---|
| `g_state.counter` | global variable / struct member |
| `g_state.tbl[3]` | array element (nested `[]`/`.` allowed) |
| `g_a.b->c->v` | chained struct pointers (`->`), any depth |
| `*g_state.pf` | scalar pointer dereference (leading `*`, repeatable) |
| `0x20000208:float` | literal address + type (default `u32`) |
| `g_state.bits.bit_a` | bit-field (read-modify-write on write) |

Literal types: `u8 i8 u16 i16 u32 i32 u64 i64 f32 f64 float double`.

`--value` is interpreted from the resolved type: integers accept decimal,
`0x` hex and `0b` binary; `f32/f64` accept floats; bit-fields are masked and
merged. A write is followed by a read-back; the exit code is non-zero on
mismatch.

```bash
embdbg read  --expr "g_state.arr_i32[3]"
# g_state.arr_i32[3] = 1300 (0x00000514) [i32]

embdbg write --expr "g_state.p_inner->x" --value 0xBEEF
# g_state.p_inner->x = 48879 (0xBEEF) [u16]  OK

embdbg write --expr "g_state.u32" --value 305419896      # decimal
embdbg write --expr "g_state.f32" --value 3.14159        # float
embdbg write --expr "g_state.bits.bit_a" --value 0b101   # bit-field

embdbg read --expr "0x20000208" --json
```

Aggregates (whole structs/arrays) are rejected by `read`/`write`; use `hss
--list-fields` / `--struct` for those.

### hss — high-speed sampling

Uses the J-Link HSS engine; fields are resolved from DWARF, so structs, arrays
and bit-fields are flattened automatically. The core keeps running.

```bash
embdbg hss --list-fields --struct "g_motor.state"          # print layout only
embdbg hss --struct "g_motor.state" --rate 1000 --duration 2 --out cap.csv
embdbg hss --expr "g_motor.state.speed" --expr "g_motor.state.iq" \
    --rate 500 --duration 3 --format jsonl --out cap.jsonl
```

- `--rate` default 500 Hz; probe-side HSS can reach ~1 kHz (fewer fields = faster);
- `--max-span` and `--coalesce-gap` coalesce neighbouring fields into one block;
- `--no-run` leaves the target halted, `--leave-halted` stops it afterwards;
- `--no-native` falls back to the host-side sampler.

See [docs/hss.md](docs/hss.md) for field layout, rate selection and block
coalescing.

### rtt — SEGGER RTT

Reads an up-channel of the target's SEGGER RTT control block. The `_SEGGER_RTT`
address is taken from the ELF; the RTT is started and the target is run.

```bash
embdbg rtt --channel 0 --duration 2
embdbg rtt --channel 0 --duration 5 --out rtt.bin
```

The firmware must call `SEGGER_RTT_Init()` once (see the SEGGER RTT sources and
the `embdbg_rtt_test` fixture in the FOC project).  See
[docs/rtt.md](docs/rtt.md) for the firmware integration and the failure modes.

### flash

Programs an ELF / HEX / BIN. **Default: J-Link DLL**, which is fast because it
resets the target first (the flash loader needs the reset/HSI clock). The old
GDB-server path is kept behind `--gdb-flash`.

```bash
embdbg flash build/Debug/firmware.elf --no-run
embdbg flash app.hex --run
embdbg flash app.bin --base 0x08000000 --no-verify
embdbg flash foc.elf --gdb-flash        # slow path, for comparison
```

- ELF: every `PT_LOAD` segment is written to its load address (flash or RAM);
- HEX/MOT/SREC: handled by the J-Link DLL;
- BIN: `--base` required;
- `--verify/--no-verify` overrides the config; `--run/--no-run` too.

See [docs/flash.md](docs/flash.md) for the two backends, the reset-clock
requirement and the ELF segment rules.

### reset

```bash
embdbg reset          # reset, leave halted
embdbg reset --run    # reset and run
```

### serve / gdbserver — breakpoint debugging

`serve` owns the probe and exposes a GDB RSP server (Ozone-style) plus optional
live sampling. `gdbserver` runs only the SEGGER GDB server.

```bash
embdbg serve --port 2331                    # connect Ozone / gdb
embdbg gdbserver --port 2331 --timeout 60   # server only
```

Start Ozone or `arm-none-eabi-gdb` with `target extended-remote 127.0.0.1:2331`.
The GDB server and the DLL-based commands are mutually exclusive: stop the
server before running `read`/`hss`/`rtt`/`flash`.

### serial / plugins

```bash
embdbg serial list
embdbg serial send "help\r\n" --port COM9 --baud 2000000 --expect "OK"
embdbg serial monitor --port COM9 --baud 2000000 --duration 2
embdbg plugins --json
```

`--port` also accepts pyserial URLs (`loop://`, `socket://host:port`). The
serial helper is a plugin; more plugins can be registered.

## How symbol resolution works

1. **DWARF first.** `pyelftools` reads the ELF's DWARF: variable addresses,
   struct layouts, member byte/bit offsets, array bounds, types.
2. **Pointers at runtime.** A chained pointer (`a->b->c`) or a dereference
   (`*p`) reads the pointer value from the target through the J-Link DLL, then
   adds the DWARF offsets — statically typed, dynamically addressed.
3. **Memory access through the J-Link DLL.** Cortex-M AHB-AP reads do not stop
   the core, so `read`/`write`/`hss`/`rtt` are non-intrusive.
4. **GDB is optional.** It is used by `serve`/`gdbserver`; `read`/`write` do not
   need it and never stop the CPU. No LLM, no C parser: only DWARF.

Details: [docs/symbols.md](docs/symbols.md).

## Using embdbg from a firmware repository

embdbg is a standalone Python package; the firmware-specific use cases belong to
the firmware repository. Install embdbg editable in that repository's venv and
keep the cases next to the firmware:

```text
firmware/
  tools/foc_cases/           # step / torque / RTT cases (the real example)
  tools/foc_cases/requirements-dev.txt
  analysis/                  # captures (gitignored)
```

```bash
pip install -e /path/to/tools[serve,serial]
python tools/foc_cases/position_step.py
```

The cases call the CLI (`python -m embdbg read/write/hss/rtt`) or the Python API
(`JLinkProbe`, `DwarfSymbols`, `DebugSession`); `examples/` in this repository
shows the generic patterns. Keep symbols, serial commands, current limits and
safety checks in the firmware repository; embdbg stays generic.

## Tests

```bash
python -m venv .venv
.venv/Scripts/pip install -q pytest pyelftools pylink-square pyserial
.venv/Scripts/python -m pytest -q      # 96 passed
```

`tests/test_embdbg_readwrite.py` exercises the resolver against the gated
firmware ELF (`g_embdbg_test` fixture: scalars, arrays, nested structs,
bit-fields, struct-pointer chains, `*p`, literal addresses) and the typed
value encoder/decoder. It skips when the ELF or the fixture is absent.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `flashed ... in ~50s` | using `--gdb-flash`; drop it (J-Link DLL is ~5s) |
| `RAMCode did not respond` | flash loader ran without a reset; the DLL path resets first |
| `ELF file not found` | `--elf` missing or config pointing at a stale path |
| probe open fails / `connect` error | Ozone or `serve`/`gdbserver` still owns the probe |
| `RTT Control Block not found` | firmware never called `SEGGER_RTT_Init()`; check `_SEGGER_RTT` in the ELF |
| `read` aggregate error | use a scalar member, or `hss --struct` |
| write mismatch | signed/unsigned width or bit-field mask; check the type in the error |

## Status / limitations

- **Windows only (currently)** — Linux/macOS are not supported yet; see
  Requirements.
- `read`/`write` resolve statics, struct/array members, bit-fields,
  struct-pointer chains, leading `*` and literal addresses; arbitrary C
  expressions (casts/arithmetic) are not parsed — use `0xADDR:type` or GDB.
- HSS pointer targets are resolved at start; if the pointer changes during the
  capture the address is not re-evaluated.
- The J-Link DLL is the fast path; the GDB-server flash path is kept for
  comparison/debugging only.
