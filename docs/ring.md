# Ring buffer + block read: sampling faster than USB allows

Host polling (`embdbg hss`) costs one USB round trip per sample: ~0.62 ms on
an ST-Link, i.e. a hard ceiling of ~1.5 kHz for a single variable. A ring
buffer moves the sampling clock **into the firmware** and turns the host into a
bulk consumer:

| | `hss` (host polling) | ring buffer |
|---|---|---|
| who times the samples | host | the control ISR / timer |
| traffic | one read per sample | one block read per poll |
| 1 kHz x 16 B | 16 KB/s in 1 KB/s-sized sips | 16 KB/s in one 1.6 KB read per 100 ms |
| measured | 993 Hz, ~2 % jitter | 1000 Hz, **0 dropped** |
| firmware change | none | ~10 lines |

## Firmware side

Copy [`examples/firmware/trace_ring.h`](../examples/firmware/trace_ring.h) into
the firmware (one `.c` includes it) and push from the control ISR:

```c
#include "trace_ring.h"

void control_isr(void)
{
    /* ... your control loop ... */
    trace_push(rt_tick_get(), iq, id);      /* ~0.1 us at 480 MHz */
}
```

[`examples/firmware/trace_demo.c`](../examples/firmware/trace_demo.c) shows the
RT-Thread variant (a 1 kHz periodic timer, `INIT_APP_EXPORT`).

That is the whole intrusion: one 16-byte copy, a barrier, a counter increment.
No RTOS call, no malloc, no printf, no blocking. Wrap it in `#if` if the
release build must not carry it.

**Cache note (STM32H7).** The ART-Pi BSP maps AXI SRAM (`0x24000000`) as
write-through, so an AHB-AP read from the debugger sees the data. A write-back
region (MPU `TEX/C/B = 0/1/1`, or no MPU region after `SCB_EnableDCache()`)
would show stale values -- mark that region non-cacheable before relying on it.

**Ordering.** The record is written, then `__DMB()`, then `head++`. The reader
only consumes records *below* head, so it never sees a half-written record.

## Host side

```bash
embdbg ring --buffer g_trace_buf --head g_trace_head \
            --fields "t:u32,a:i32,b:i32,seq:u32" \
            --rate 10 --duration 5 --out trace.csv
```

```
index,t,a,b,seq
4322,4324,4324,-4324,4322
4323,4325,4325,-4325,4323
...
ring: 4992 record(s) in 5.06s (987/s), 35 poll(s), 0 dropped, 16 B/record, capacity 2048
```

- `--buffer` / `--head` are ELF symbols; the capacity is inferred from the
  buffer symbol's `st_size` divided by the record size (`--capacity` overrides).
- `--fields` uses the same layout syntax as `--sample-fields`
  (`name:type[@offset]`, types `u8..u64/i8..i64/f32/f64`).
- `--rate` is the **poll** rate, not the sample rate; one poll drains
  everything written since the previous one.
- `--from-start` also emits the records already sitting in the buffer.
- `--format jsonl` emits one JSON object per record instead of CSV.
- The reader never writes and never halts the core.

### Wrap and overrun

The reader fetches `[last, head)` in one block read, or two when the span
crosses the end of the buffer. If `head - last > capacity`, the writer lapped
the reader: the old records are gone, so the reader skips to the last
`capacity` records and reports the loss:

```
warning: writer lapped the reader, 3633 record(s) lost (raise --rate or enlarge the buffer)
```

Measured on an ART-Pi (ST-Link, 1 kHz producer, 2048 x 16 B = 32 KB buffer):

| poll rate | records read | dropped |
|---|---|---|
| 10 Hz | 4992 in 5.06 s | **0** |
| 1 Hz | 5150 in 5.69 s | **0** |
| 0.2 Hz | 6144 in 17 s | 10197 (detected) |

## Choosing a buffer size

Between two polls the firmware writes `rate / poll_rate` records. Keep at least
2-3x that in the buffer:

| sample rate | poll rate | records between polls | 2 s of history at 16 B |
|---|---|---|---|
| 1 kHz | 10 Hz | 100 | 32 KB (2048 records) |
| 1 kHz | 1 Hz | 1000 | 32 KB is only 2 s -- fine, but a hiccup costs data |
| 20 kHz | 10 Hz | 2000 | 640 KB -- does not fit; poll faster or shrink the record |

## Limitations

- RAM cost: `capacity x record_size` in `.bss`. 2048 x 16 B = 32 KB.
- The u32 head wraps after ~49 days at 1 kHz (the reader handles wrap).
- The reader must keep up on average; bursts are absorbed by the buffer.
- Records are written by the CPU -- DMA-produced data needs one CPU-side copy
  (or a completion-interrupt push), which is usually 1-2 instructions per sample.
