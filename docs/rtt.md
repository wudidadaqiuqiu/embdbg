# RTT: SEGGER Real Time Transfer

`embdbg rtt` reads an up-channel of the target's SEGGER RTT control block. It
locates `_SEGGER_RTT` from the ELF, starts the RTT through the J-Link DLL and
reads the channel; the core keeps running.

## Firmware side

Copy the SEGGER RTT sources into the firmware repository (do not reference them
from outside the project):

```text
Drivers/SEGGER/
  SEGGER.h
  Config/SEGGER_RTT_Conf.h
  RTT/SEGGER_RTT.c
  RTT/SEGGER_RTT.h
  RTT/SEGGER_RTT_printf.c
  RTT/SEGGER_RTT_ASM_ARMv7M.s
```

CMake:

```cmake
target_sources(${PROJECT} PRIVATE
    Drivers/SEGGER/RTT/SEGGER_RTT.c
    Drivers/SEGGER/RTT/SEGGER_RTT_printf.c
    Drivers/SEGGER/RTT/SEGGER_RTT_ASM_ARMv7M.s)
target_include_directories(${PROJECT} PRIVATE
    Drivers/SEGGER Drivers/SEGGER/RTT Drivers/SEGGER/Config)
```

Initialise once and write to the channel:

```c
#include "SEGGER_RTT.h"

SEGGER_RTT_Init();

/* from the main loop, not from an ISR, a few lines every 100 ms */
SEGGER_RTT_printf(0, "tick %lu\r\n", (unsigned long)counter++);
```

Notes:

- `SEGGER_RTT_Init()` copies the `"SEGGER RTT"` ID into the control block; the
  J-Link searches for it. Without the init the block stays zero and the tool
  reports "control block not found".
- `SEGGER_RTT_printf` needs the `Float2Str` helper when `%f` is enabled; the
  SEGGER file references it at link time. Provide a small helper (the reference
  implementation is in SEGGER's `debug_log.c`).
- Channel 0 is the terminal channel; up-buffers are target→host. The default
  buffer size is 1 KB (`BUFFER_SIZE_UP`), enough for periodic text.

## Tool side

```bash
embdbg rtt --channel 0 --duration 2
embdbg rtt --channel 0 --duration 5 --out rtt.bin
```

- the `_SEGGER_RTT` address comes from the ELF (`variable_address`);
- the target is run before the RTT is started (the firmware must have
  initialised the block);
- `--no-run` leaves the target halted (useful to inspect a stale buffer);
- `--out` writes the raw bytes; otherwise the decoded UTF-8 text is printed.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `RTT Control Block has not yet been found` | firmware did not call `SEGGER_RTT_Init()`, or the ELF/`_SEGGER_RTT` does not match the flashed image |
| no data, no error | wrong channel, the firmware never writes, or the buffer was not flushed before halt |
| garbage text | the firmware writes binary to channel 0; use `--out` and decode yourself |
| target halted | `rtt` runs the target unless `--no-run` |
| probe busy | stop Ozone/`serve`/`gdbserver` first |
