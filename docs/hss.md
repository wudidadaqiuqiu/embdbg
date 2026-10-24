# HSS: high-speed sampling

`embdbg hss` streams variables from the target using the J-Link **HSS** engine.
The core keeps running; fields are resolved from DWARF.

## Fields and layout

Two ways to select data:

```bash
--expr "g_motor.state.speed"          # one scalar leaf (repeatable)
--struct "g_motor.state"              # every scalar member of the struct
--struct "g_motor.state" --recursive  # descend nested structs and arrays
--list-fields --struct "g_motor.state" # print the layout and exit
```

`--list-fields` prints the resolved address, byte size and type of each leaf.
Bit-fields are reported with their bit position and are sampled as an
independent field.

Arrays are flattened with an explicit index (`tbl[0]`, `tbl[1]`, …). Large
arrays/structs produce many fields; prefer `--expr` for the interesting ones.

## Rate selection

| setup | practical rate |
|---|---|
| probe-side HSS, 1–4 compact fields | ~1 kHz |
| probe-side HSS, 7+ fields / wide blocks | ~500–600 Hz |
| host-side sampler (`--no-native`) | limited by the J-Link read round-trip |

The probe reads each block once per sample; the sample period is the HSS timer,
not the host round-trip. `--max-span` and `--coalesce-gap` merge neighbouring
fields into a single block: fewer blocks and smaller spans mean a higher rate.
Fields far apart (for example different structs) become separate blocks and cost
more.

`--rate` is clamped to `MaxFreq` reported by the DLL. If you ask for more than
the probe can do, the tool prints the clamp and continues.

## Output

- `--format csv` (default): header `t_s,<field paths…>`;
- `--format jsonl`: one object per sample;
- `--out FILE` writes the stream; without it, stdout;
- `--duration S` / `--count N` bound the capture.

The timestamp is the HSS sample tick converted to seconds; the first sample is
`t=0`.

## Target state

- default: start the target (`go`) and keep it running;
- `--no-run`: leave it halted and sample whatever is in memory;
- `--leave-halted`: run during the capture, halt afterwards.

For control-loop analysis, keep the target running and use a high rate only for
the few signals you need. A 100–200 Hz capture is often enough to see trends;
1 kHz is for step responses and current-loop transients.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| rate much lower than requested | too many blocks/fields; coalesce or sample fewer `--expr`s |
| `MaxBlocks` error | split the field set; the probe limits the block count |
| all zeros | wrong symbol/fixture, or the struct was optimized out |
| `--list-fields` empty | the struct has no scalar leaves (only nested/arrays); use `--recursive` |
| probe busy | stop `serve`/`gdbserver`/Ozone first |
