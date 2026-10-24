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
| `flash <file>` | program an ELF/HEX/BIN |
| `reset` | reset the target |
| `gdbserver` | J-Link GDB server only (IDE / plain gdb) |
| `serve` (`gdb`) | GDB RSP + live sampling, Ozone-style breakpoint debugging |
| `serial` | serial port plugin (list / send / monitor) |
| `plugins` | list plugins |
| `hss` | high-speed DWARF-aware variable sampling (no halt) |

Symbol/field/pointer aware `read` / `write` and RTT (`rtt`) are being added on
top of the same DWARF resolver.

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

## Dependencies

- GNU Arm toolchain (`arm-none-eabi-gdb`)
- SEGGER J-Link software (`JLinkGDBServerCL.exe`, `JLink.exe`)
- Python ≥ 3.10; optional `pyserial` (serial), `pylink-square` + `pyelftools`
  (HSS/native sampling, RTT)
