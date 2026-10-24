# embdbg

A scriptable J-Link debug backend for embedded targets (Cortex-M). The CLI is a
thin layer over `embdbg.DebugSession`; the same API can be reused by a GUI or
test scripts.

```
your CLI / GUI / pytest
      │ Python API
      ▼
embdbg.DebugSession ──► Backend (pluggable)
                          │
                  JLinkGdbBackend
                  ├─ JLinkGDBServerCL.exe   (USB → SWD)
                  └─ arm-none-eabi-gdb --interpreter=mi2
```

## Commands

| command | purpose |
|---|---|
| `probes` / `list` | list connected J-Link probes |
| `info` | connect and show target information |
| `doctor` | check toolchain / config / probe |
| `flash <file>` | program an ELF/HEX/BIN (J-Link DLL by default, fast; `--gdb-flash` = old GDB-server path) |
| `reset` | reset the target |
| `gdbserver` | J-Link GDB server only (IDE / plain gdb) |
| `serve` (`gdb`) | GDB RSP + live sampling, Ozone-style breakpoint debugging |
| `serial` | serial port plugin (list / send / monitor) |
| `plugins` | list plugins |
| `hss` | high-speed DWARF-aware variable sampling (no halt) |
| `read` | read `var`, `var.field`, `arr[i]`, `a->b->c`, `*p`, `0xADDR:type` |
| `write` | write the same expressions; typed value (decimal/0x/0b/float) |
| `rtt` | read a SEGGER RTT up-channel |

`read` / `write` resolve globals, struct/array members, bit-fields and
`a->b->c` struct-pointer chains from DWARF (no LLM, no halt).

## Quick start

```bash
python -m embdbg doctor
python -m embdbg probes
python -m embdbg info
python -m embdbg flash build/Debug/firmware.elf --no-run
python -m embdbg hss --expr "sys.motor.speed" --rate 1000 --duration 2 --out cap.csv
python -m embdbg serve          # connect Ozone / gdb to 127.0.0.1:2331
```

Configuration: copy `embdbg.toml` into your firmware repository, set `device`
and `elf`, then run from there (or pass `--elf` / `--device` explicitly).

## Read / write / RTT examples

```bash
embdbg read  --elf build/Debug/firmware.elf --expr "g_state.arr[3]"
embdbg read  --expr "g_a.b->c->v"                 # chained struct pointers
embdbg read  --expr "*g_state.pf"                 # scalar pointer deref
embdbg read  --expr "0x20000208:float"            # literal address + type
embdbg write --expr "g_state.u32" --value 0x11223344
embdbg rtt   --channel 0 --duration 2
```

All of the above use the J-Link DLL: no halt for symbols (pointer values are
read through the DLL), no external tools, no LLM.

## Tests

```bash
python -m venv .venv
.venv/Scripts/pip install -q pytest pyelftools pylink-square pyserial
.venv/Scripts/python -m pytest -q      # 96 passed
```

## Dependencies

- GNU Arm toolchain (`arm-none-eabi-gdb`)
- SEGGER J-Link software (`JLinkGDBServerCL.exe`, `JLink.exe`)
- Python ≥ 3.10; optional `pyserial` (serial), `pylink-square` + `pyelftools`
  (HSS/native sampling, RTT)
