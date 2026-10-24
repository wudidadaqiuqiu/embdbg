# Flash: why the J-Link DLL is fast

`embdbg flash` has two backends:

| backend | flag | engine | typical time for ~120 KB |
|---|---|---|---|
| J-Link DLL | default | `pylink` → `JLINK_Flash` | **~5–6 s** (with verify) |
| GDB server | `--gdb-flash` | `JLinkGDBServerCL` + `arm-none-eabi-gdb -target-download` | ~50 s |

The DLL path mirrors what Ozone / J-Link Commander do. The GDB path is kept only
for comparison and debugging; do not use it in a normal flashing loop.

## The reset-clock problem

The J-Link flash loader runs a small RAM algorithm. It expects the target to be
in its **reset/HSI clock** state. If the firmware is already running (for
example the FOC at 170 MHz), the loader times out:

```
Timeout while programming sector, RAMCode did not respond in time!
```

The fix is a reset before the first flash operation:

```python
probe.reset(halt=True)
probe.flash(data, address)   # flash loader now runs from the reset state
```

`embdbg flash` does this automatically. If you write your own pylink script,
always reset before `JLink.flash*()`.

## What gets written (ELF)

For an ELF, every `PT_LOAD` segment is programmed at its **load address**
(`p_paddr`), exactly like `objcopy`/GDB `load`:

- `.text` / `.rodata` / the `.data` **load image** → flash (`0x0800_0000…`);
- RAM-addressed segments, if any → `JLink` memory write;
- `.bss` is not written; the startup code zeroes it;
- the startup copies the `.data` load image from flash to RAM.

For `.hex` / `.mot` / `.srec` the J-Link DLL parses the file; `.bin` requires
`--base`.

## Usage

```bash
embdbg flash firmware.elf --no-run          # program, stay halted
embdbg flash firmware.elf --run             # program and run
embdbg flash app.bin --base 0x08000000      # raw binary
embdbg flash app.hex --no-verify            # skip read-back
```

- `--verify` (default from the config) reads every segment back and compares it;
- a mismatch prints the offending segment and exits non-zero;
- `--run` / `--no-run` override `run_after_flash` from the config.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| ~50 s flash | you are on `--gdb-flash`; remove it |
| `RAMCode did not respond` | no reset before `JLink.flash`; reset first |
| `verify: MISMATCH` | wrong `--base`, wrong image, or the flash was protected |
| `ELF file not found` | `--elf` / config `elf` points at a stale path |
| probe busy | Ozone or `serve`/`gdbserver` still holds the probe |

The J-Link DLL and the GDB server are mutually exclusive; stop the GDB server
(`serve`/`gdbserver`) before flashing, sampling or reading symbols.
