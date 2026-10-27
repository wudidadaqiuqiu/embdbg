/* trace_ring.h -- firmware side of `embdbg ring`
 *
 * Copy this file into the firmware, include it from exactly one .c file, and
 * call trace_push() from the control ISR or a timer callback.  The debugger
 * only ever *reads*: no halt, no printf, no RTOS call inside the push.
 *
 * Cost : one 16-byte copy + a counter increment (~0.1 us at 480 MHz)
 * RAM  : TRACE_CAPACITY * sizeof(trace_rec_t)   (2048 x 16 B = 32 KB)
 *
 * Cache note (STM32H7): the ART-Pi BSP maps AXI SRAM (0x24000000) as
 * write-through, so a debugger read through the AHB-AP sees the data.  If the
 * buffer lands in write-back memory (an MPU region with TEX/C/B = 0/1/1, or
 * no MPU region at all after SCB_EnableDCache()), the debugger can read stale
 * values -- make that region non-cacheable before relying on this.
 *
 * Ordering: the record is written first, then __DMB(), then head++.  The
 * reader only consumes records below head, so it never sees a half-written
 * record; if it falls more than CAPACITY records behind, the oldest ones are
 * overwritten and `embdbg ring` reports them as dropped.
 */
#ifndef TRACE_RING_H
#define TRACE_RING_H

#include <stdint.h>

#ifndef TRACE_CAPACITY
#define TRACE_CAPACITY 2048u            /* must be a power of two */
#endif

typedef struct
{
    uint32_t t;      /* rt_tick_get() */
    int32_t  a;
    int32_t  b;
    uint32_t seq;    /* head value before the push */
} trace_rec_t;

static trace_rec_t       g_trace_buf[TRACE_CAPACITY];
static volatile uint32_t g_trace_head;  /* monotonic record counter */

static inline void trace_barrier(void)
{
#if defined(__GNUC__)
    __asm volatile ("dmb 0xF" ::: "memory");
#elif defined(__CC_ARM)
    __dmb(0xF);
#else
    /* single-core Cortex-M without barriers: best effort */
#endif
}

static inline void trace_push(uint32_t t, int32_t a, int32_t b)
{
    uint32_t head = g_trace_head;
    trace_rec_t *rec = &g_trace_buf[head & (TRACE_CAPACITY - 1u)];

    rec->t   = t;
    rec->a   = a;
    rec->b   = b;
    rec->seq = head;

    trace_barrier();
    g_trace_head = head + 1u;
}

#endif /* TRACE_RING_H */
