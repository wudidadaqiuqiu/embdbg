# OpenOCD backend: ST-Link / CMSIS-DAP without a J-Link

`embdbg` started life around the SEGGER J-Link DLL. The `openocd-gdb` backend
removes that requirement: it drives **openocd** (any probe openocd supports --
ST-Link, CMSIS-DAP, FTDI, J-Link, ...) and reuses the same GDB/MI layer.

```
                 ┌──────────────┐   TCL (:tcl_port)   ┌───────────────────┐
  probe  <SWD>   │   openocd    │◄───────────────────►│ OpenOcdProbe      │
  ST-Link  ─────►│              │                     │ read/write/flash  │
                 │  gdb server  │                     └───────────────────┘
                 └──────┬───────┘   RSP (:gdb_port)
                        │
                        ▼
                 arm-none-eabi-gdb ◄── GDB/MI ── MIClient ── Backend
                                                   (breakpoints, frames,
                                                    registers, stepping)
```

| component | J-Link backend | OpenOCD backend |
|---|---|---|
| probe access | `JLinkARM.dll` (pylink) | `OpenOcdProbe` over openocd's TCL port |
| GDB server | `JLinkGDBServerCL.exe` | `openocd -f <board.cfg>` |
| device name | required (`device`) | not used; the board config names the target |
| flash engine | J-Link DLL | `flash write_image erase` |
| RTT | yes | no (needs the J-Link DLL) |
| `serve` / `gdbserver` | yes (RSP proxy) | not needed: openocd *is* the GDB server |

## Configure

```toml
[embdbg]
backend = "openocd-gdb"                  # default is "jlink-gdb"
openocd_config = "debug/openocd/artpi.cfg"   # `openocd -f` argument (required)
# openocd = "C:/path/to/openocd.exe"     # auto-discovered from PATH / winget
# openocd_args = []                      # extra argv after the config
speed_khz = 4000                         # becomes `-c "adapter speed <n>"`
```

Or on the command line: `--backend openocd-gdb --openocd-config artpi.cfg`.

`device` and `interface` are ignored by this backend -- the config file already
declares the transport, the adapter and the target.

## Use

```bash
embdbg doctor          # gdb + openocd + config + live probe check
embdbg probes          # prints the adapter openocd found (STLINK V2J48M35 ...)
embdbg info            # connect, show state / PC / voltage
embdbg flash build/rtthread.bin --base 0x90000000 --run
embdbg read --expr rt_tick              # non-intrusive, target keeps running
embdbg hss --expr rt_tick --rate 100    # host-side sampler (see below)
```

Typical timings on an STM32H750 / ART-Pi, 127 KB image in external QSPI:
**~2.3-3 s** including openocd start-up and verify.

## What works, what does not

| command | openocd backend |
|---|---|
| `doctor`, `probes`, `info`, `reset` | yes |
| `read`, `write` | yes, over TCL -- the core is **not** halted |
| `flash` | yes, `flash write_image erase` + `verify_image` |
| `flash --gdb-flash` | yes (drives the same flash engine from a connected session) |
| `debug` | yes -- breakpoints, stepping, backtrace, locals, registers |
| `serve` | yes, in the OpenOCD shape: start its GDB server + sample over TCL |
| `gdbserver` | yes -- starts openocd and prints the `target extended-remote` line |
| `hss` | yes, forced to `--no-native` (host-side sampling) |
| `rtt` | refused: SEGGER RTT is a J-Link feature |

## Debugging (breakpoints, stepping, variables)

`embdbg debug` is a session, not a set of one-shot commands, because **OpenOCD
drops the breakpoints when GDB disconnects** (verified: `bp` is empty after
detach). One process holds the GDB/MI connection for the whole session:

```text
$ embdbg debug
openocd-gdb: halted  pc 0x90001db4
embdbg debug session -- 'help' for commands, 'quit' to exit
(embdbg) break rt_pin_write
1	rt_pin_write (pin.c:145)
(embdbg) run
breakpoint 1: rt_pin_write at pin.c:145
  at rt_pin_write at pin.c:145
(embdbg) bt
#0   rt_pin_write at pin.c:145
#1   main at main.c:26
(embdbg) frame 1
(embdbg) locals
count = 505
(embdbg) print rt_tick
rt_tick = 504001
(embdbg) regs pc sp
pc     0x90000b04
sp     0x24001db0
```

Scriptable form (CI / bug reports): `-c` runs each command and exits.

```bash
embdbg debug -c "break rt_pin_write" -c run -c bt -c "frame 1" -c locals -c quit
```

Breakpoints are **hardware** breakpoints (`hw_breakpoints = true`): code lives
in QSPI flash, so software breakpoints cannot work, and the Cortex-M7 only has
8 of them.

## serve on OpenOCD: no proxy needed

`serve` on J-Link exists because the DLL is not a GDB server; openocd already
is one. So the OpenOCD shape simply:

1. starts `openocd` with a GDB port **and** a TCL port,
2. points your debugger at the GDB port,
3. runs the sampler against TCL in the same process.

Debugger and sampling then coexist -- verified with a plain
`arm-none-eabi-gdb` hitting a breakpoint while `serve --sample rt_tick` kept
appending CSV rows.

```bash
embdbg serve --sample rt_tick --sample-size 4 --rate 20 --sample-out ticks.csv
# stderr: openocd pid 1234: gdb 127.0.0.1:2331, tcl 2332
#         arm-none-eabi-gdb "<elf>" -ex "target extended-remote 127.0.0.1:2331"
```

(`--sample-size` is still required without `--sample-fields`, same as on J-Link.)

## Why TCL and not GDB for memory access

GDB can only touch memory while the target is stopped. OpenOCD's TCL commands
go through the AHB-AP, so `read`/`write`/`hss` work with the core running --
that is what makes `hss` usable without halting the firmware.

The wire protocol is simple: command bytes + `0x1a`, reply bytes + `0x1a`.
Three quirks are worth knowing:

* **One command can produce several messages.** `flash write_image` sends
  `auto erase enabled` and then `wrote N bytes ...` as two terminated messages;
  the probe keeps reading until it sees the result line.
* **Paths must use `/`.** The command string is parsed by TCL, so a Windows
  path like `build\rtthread.bin` loses its `\r`, `\b`, `\a` characters.
* **`shutdown` beats `TerminateProcess`.** On Windows, `stop()` sends
  `shutdown` over TCL first; killing openocd can leave the ST-Link wedged until
  the USB cable is re-plugged.

## Notes for external-QSPI targets (ART-Pi)

* openocd probes the QSPI bank lazily: the `flash1 'win w25q64fv/jv'` line only
  appears on the first flash operation, not at start-up. `monitor flash banks`
  (or `embdbg probes`) is the quick check.
* The QSPI controller is configured by the bootloader/firmware, so the probe
  resets and lets the target run briefly before `flash write_image`
  (`startup_delay`, 0.6 s by default). Without that openocd reports
  *"No QSPI, no OCTOSPI at 0x52005000"*.
* Code runs from QSPI, i.e. from flash: hardware breakpoints only. The backend
  keeps `hw_breakpoints = true` for both backends.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `openocd exited during startup` | check the config path and the openocd log (`--server-log`, `-v`) |
| `openocd could not open the probe ... Error: libusb_open() failed` | another openocd / Ozone / J-Flash owns the probe: close it |
| `could not attach GDB to the OpenOCD GDB server` | the probe connected but no target responded: power-cycle the board |
| GDB reports `SWD DPIDR` warnings | normal; openocd prints the ID it read |
| flash says `couldn't open <path>` | path had backslashes -- fixed in the probe; report if you see it again |
