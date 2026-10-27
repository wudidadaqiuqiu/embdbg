# Examples

Small, firmware-agnostic patterns. They call the installed CLI
(`python -m embdbg …`); the same operations are available through the Python
API (`JLinkProbe`, `DwarfSymbols`, `DebugSession`).

A complete, firmware-specific example lives in the FOC project:
`software/Source_Project/tools/foc_cases/` (position/torque step + RTT), which
also shows the two-repository layout: embdbg stays generic, the firmware
repository holds the symbols, serial commands and safety rules.

| example | what it shows |
|---|---|
| `read_write.py` | resolve a symbol, read or write it, JSON output |
| `hss_capture.py` | stream DWARF fields to CSV at a given rate |
| `rtt_capture.py` | capture a SEGGER RTT channel |
| `flash_reset.py` | flash an image (J-Link DLL) and reset |
| `ring_capture.py` | drain a firmware ring buffer (1 kHz without per-sample USB) |
| `firmware/trace_ring.h` | firmware side: record array + head counter (~10 lines) |
| `firmware/trace_demo.c` | RT-Thread 1 kHz producer |

Run them with `--elf`/`--device` matching your firmware.
