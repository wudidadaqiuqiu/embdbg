/* trace_demo.c -- 1 kHz producer for `embdbg ring` (RT-Thread example)
 *
 * Compiles next to trace_ring.h.  Drop both files into an RT-Thread BSP's
 * applications/ directory, add trace_demo.c to the build, and the ring starts
 * filling at 1 kHz; `g_trace_buf` / `g_trace_head` are then visible to the
 * debugger through the ELF.
 *
 *   embdbg ring --buffer g_trace_buf --head g_trace_head \
 *               --fields "t:u32,a:i32,b:i32,seq:u32" --rate 10 --duration 5
 */
#include <rtthread.h>
#include "trace_ring.h"

static rt_timer_t trace_timer;

static void trace_timer_cb(void *parameter)
{
    rt_uint32_t tick = rt_tick_get();

    /* stand-ins for the real control-loop values */
    trace_push(tick, (rt_int32_t)tick, -(rt_int32_t)tick);
}

int trace_demo_init(void)
{
    trace_timer = rt_timer_create("trace",
                                  trace_timer_cb,
                                  RT_NULL,
                                  1,                       /* every 1 ms -> 1 kHz */
                                  RT_TIMER_FLAG_PERIODIC | RT_TIMER_FLAG_SOFT_TIMER);
    if (trace_timer == RT_NULL)
    {
        return -RT_ERROR;
    }
    rt_timer_start(trace_timer);
    return RT_EOK;
}
INIT_APP_EXPORT(trace_demo_init);
