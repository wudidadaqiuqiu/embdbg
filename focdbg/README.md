# focdbg

一个面向 **J-Link + STM32G431** 的可脚本化调试后端。CLI 只是薄薄一层，真正的接口是
`focdbg.DebugSession`，后续 GUI 直接复用，不需要重写调试逻辑。

```
 你的 CLI / GUI / pytest
        │  Python API
        ▼
  focdbg.DebugSession ──► Backend (可替换)
                              │
                    JLinkGdbBackend
                    ├─ JLinkGDBServerCL.exe   (USB → SWD)
                    └─ arm-none-eabi-gdb --interpreter=mi2
                              │ GDB/MI
                              ▼
                       STM32G431CB
```

## 为什么是 GDB/MI 而不是 GDB Python

本机 STM32CubeCLT 自带的 `arm-none-eabi-gdb` **没有编译 Python 支持**：

```
$ arm-none-eabi-gdb -batch -ex 'python print(1)'
Python scripting is not supported in this copy of GDB.
```

所以自动化走 **GDB/MI**（`--interpreter=mi2`），跨平台且不依赖 gdb 的 Python 构建。

## 快速开始

```bash
cd tools/focdbg

python -m focdbg doctor            # 检查 gdb / J-Link / ELF / 探针
python -m focdbg probes            # 列出 J-Link（只连 USB，不碰目标，板子跑着也安全）
python -m focdbg info              # 连接并打印目标信息
python -m focdbg flash build/Debug/foc.elf --no-run
python -m focdbg console           # pyOCD commander 风格交互
```

安装成命令（可选）：

```bash
pip install -e .
focdbg doctor
```

## 命令

| 命令 | 作用 |
|---|---|
| `probes`（别名 `list`） | 枚举 J-Link 探针（`ShowEmuList`，不连接目标） |
| `doctor` | 检查 gdb / JLinkGDBServer / JLink.exe / ELF / 探针 / 配置 |
| `info` | 连接后显示设备、状态、PC、电压、端口、PID |
| `flash <file>` | 烧写 ELF / HEX / BIN；`--base` 用于 bin；`--run/--no-run`；`--verify/--no-verify` |
| `reset` | 复位；默认 halt，`--run` 复位后运行 |
| `gdbserver` | 只起 J-Link GDB Server，供 VSCode / 原生 gdb 连接 |
| `console`（别名 `commander`） | 交互式调试控制台，或用 `-c` 执行命令后退出 |
| `json <command>` | 执行一条 console 命令并输出 JSON |
| `serve`（别名 `gdb`） | **单进程**：GDB RSP 服务端 + 后台采样（Ozone 式） |
| `sample` | 周期性采样内存/变量，按字段布局解码，输出 CSV/JSONL |
| `dump` | 一次性读取内存，或把环形缓冲按记录解码成 CSV |

全局参数：`--config --device --interface --speed --elf --gdb --jlink-server --jlink-exe
--base-port --probe-serial --json -v`。参数可放在子命令前后。

### 一次性自动化

```bash
# 烧写、停在 main、打印 PC、继续
python -m focdbg console \
  -c "flash build/Debug/foc.elf --no-run"   # 注意：flash 是子命令，不在 console 里
```

console 里没有 `flash`（它是 CLI 子命令）；自动化推荐：

```bash
python -m focdbg flash build/Debug/foc.elf --no-run --json
python -m focdbg console -c "break main" -c "go" -c "wait" -c "reg pc" --json
```

### console 命令（对齐 pyOCD commander）

```
halt (h)              go (g)                step (s) [n]        next (n) [n]
reset [halt|run]      reg [names...]        wreg <name> <val>
read8/16/32 <addr|sym> [count]              write8/16/32 <addr|sym> <val>...
break (b) <loc>       delete (d) [n|all]    info registers|breakpoints|threads|target
bt / where            frame <n>             print (p) <expr>    symbol <name>
disassemble (disasm)  threads               wait [timeout]      sleep <ms>
json [on|off]         gdb <raw command>     exit (q)
```

`<addr|sym>` 支持 `0x20000000`、`&variable` 和符号名；`reg` 支持大小写不敏感的子集过滤。

## 配置

`focdbg.toml` 的搜索顺序：`--config` → `FOCDBG_CONFIG` → 当前目录及上级 →
本工具目录 → `%APPDATA%\focdbg\`。当前配置：

```toml
[focdbg]
device = "STM32G431CB"
interface = "SWD"
speed_khz = 4000
elf = "C:/docs/uni/research/PAT/foc/foc_open/software/Source_Project/build/Debug/foc.elf"
run_after_flash = false
verify = true
singlerun = true
```

把本文件复制到固件仓库根目录并把 `elf` 改成 `build/Debug/foc.elf`，从仓库里运行即可自动发现。

环境变量：`FOCDBG_DEVICE` `FOCDBG_INTERFACE` `FOCDBG_SPEED` `FOCDBG_ELF` `FOCDBG_GDB`
`FOCDBG_JLINK_SERVER` `FOCDBG_JLINK_EXE` `FOCDBG_PORT` `FOCDBG_PROBE_SERIAL`。

## 单进程模式：GDB + 采样（Ozone 式）

`focdbg serve` 用**一个进程独占探针**，内部同时做两件事：

```
             ┌────────── focdbg serve ──────────┐
             │  JLinkProbe (pylink/DLL)          │
             │     ▲              ▲              │
             │  RSP server     Sampler (HSS)     │
             └─────┬──────────────┬──────────────┘
                   │ TCP :2331    │ CSV / JSONL
          arm-none-eabi-gdb / VSCode
```

- GDB 的断点/单步/回溯照常可用；
- 采样线程用同一探针在后台读内存（**不停核**），即 Ozone Graph 用的 HSS；
- 已实测：`monitor reset` → `monitor go` → 2 秒 → `monitor halt` 期间，
  采样完整记录 `uwTick` 0→2049，GDB 全程保持连接。

```bash
focdbg serve --sample uwTick --sample-fields "n:u32" --rate 100 --sample-out tick.csv
# 另一个终端
arm-none-eabi-gdb build/Debug/foc.elf -ex "target extended-remote 127.0.0.1:2331"
```

依赖：`pip install -e ".[serve]"`（pylink-square + pyelftools）。

限制：
- `serve` 与 `console`/`flash` **不能同时运行**（探针独占）；先 `focdbg flash`，再 `serve`。
- GDB 的 `load` 不支持（未实现 `vFlash*`），烧写请用 `focdbg flash`。
- `serve` 只暴露核心寄存器（r0-r15、xpsr、msp/psp、primask/basepri/faultmask/control），
  FPU 寄存器暂未加入 target description。

## 非侵入式采样（FOC 用）

断点/观察点会停核，电流环停几百微秒就会扰动电机。推荐的目标端/主机端分工：

```
MCU:  ISR 把样本写进 RAM 环形缓冲（不依赖调试器）
主机: 以较低频率通过 SWD 读缓冲，离线解码
```

主机端已经支持字段布局解码，类型：`u8/i8/u16/i16/u32/i32/u64/i64/f32/f64`，
`name:type` 默认顺序排列，`name:type@offset` 可指定偏移。

```bash
# 采样一个结构体（顺序布局：iq, id, ud, ud_ref）
focdbg sample --symbol g_foc --fields "iq:i32,id:i32,ud:f32,ud_ref:f32" \
    --rate 100 --duration 5 --out foc.csv

# 只采一个 u32 计数器，JSONL 输出
focdbg sample --symbol g_isr_count --fields "n:u32" --rate 50 --count 100 --format jsonl

# 把 4 KiB 环形缓冲按 12 字节记录解码成 CSV
focdbg dump --symbol g_capture_buf --length 4096 \
    --fields "iq:i32,id:i32,ud:f32" --format csv --out capture.csv
```

- 连接会 halt 内核，所以采样运行中的变量要加 `--resume`（先恢复运行再采）。
- 默认 `--halt`：每次采样 halt → 读 → resume，定时确定、兼容性最好。
- `--no-halt`：目标运行中直接读，是否可行取决于 J-Link/内核是否允许后台总线访问；
  失败会抛 GDB 错误，不会静默返回脏数据。
- 高速捕获（>1 kHz）不要靠主机轮询，应该在目标端存缓冲，再用 `dump` 一次性取回。
- `sample` 的进度和统计走 stderr，CSV/JSONL 走 stdout 或 `--out`，可直接接 pandas。

## HSS：DWARF 感知的不停核采样

`sample` 走 GDB，读内存前必须停核；对 FOC 来说停核会冻结 PWM。`hss` 改用
**J-Link DLL 后台总线访问**（Ozone HSS 的方式），核一直跑，按设定频率读 RAM。
字段地址/偏移直接从 ELF 的 `.debug_info` 解析，不再手写 `name:type@offset`：

```bash
# 先看 DWARF 解析出的结构布局（不需要连板子）
focdbg hss --list-fields --struct sys.foc_controller
focdbg hss --list-fields --expr sys.fault_status.serious_fault.bit.motor_cfg_err

# 运行中采样，核保持运行；CSV 列为 DWARF 表达式
focdbg hss --rate 500 --duration 5 \
    --expr sys.foc_controller.speed --expr sys.foc_controller.speed_ref \
    --expr sys.foc_controller.iq_ref --expr sys.foc_controller.iq \
    --out foc.csv

# 整个结构体一次读完（自动合并相邻字段，一次 background read）
focdbg hss --struct sys.foc_controller --rate 1000 --duration 3 --out foc.csv --format jsonl

# 嵌套结构/数组/bit-field 都支持
focdbg hss --expr sys.pid_speed.integral --expr sys.cfg.enc_lut_a[2] \
    --expr sys.fault_status.serious_fault.bit.driver_fault_err
```

要点：

- `--expr` 支持 `a.b[2].c`；`--struct` 展开所有标量成员；`--recursive` 递归嵌套
  struct/union/数组；bit-field 会按位解码。
- 多个字段会**合并成尽量少的连续块**（`--max-span` / `--coalesce-gap`），一帧只做
  少量 background read；实测同一 `sys` 内 9 个字段 500 Hz 无压力。
- 打开探针会先 halt 一次；默认随后 `go`，采样结束也保持运行。要留在 halt 用
  `--leave-halted`，要自己控制用 `--no-run`。
- 统计信息走 stderr，CSV/JSONL 走 stdout 或 `--out`。
- **多步写入的变量要小心**：HSS 是异步总线读，可能在 ISR 两次写之间采样。例如
  `mag_encoder_update()` 先写原始 `angle` 再原地减 LUT，HSS 会偶尔看到原始值
  （差一个 LUT，可能十几度）。这不是采样错误，而是目标端该变量不是原子快照；
  控制环（TIM7）优先级低于 DMA ISR，实际不会读到中间态。

符号解析基于 pyelftools 的 DWARF 4/5（DWARF5 的 `DW_AT_data_bit_offset`、
匿名 struct/union、typedef 链、数组下标都已处理）。`ElfSymbols` 仍然只做
顶级符号 → 地址的映射，`DwarfSymbols` 才做 `变量.成员` 的偏移解析。

## 插件

插件是带生命周期的命名对象。发现顺序：内置 → entry point（`focdbg.plugins` 组）→ `--plugin module:Class`。

内置 `serial` 插件，配置：

```toml
[plugins.serial]
port = "COM4"          # 设备名，或 pyserial URL：loop:// / socket://host:port
baudrate = 115200
bytesize = 8
parity = "N"           # N/E/O/M/S
stopbits = "1"         # 1 / 1.5 / 2
flow = "none"          # none / rtscts / xonxoff / dsrdtr
timeout = 0.1
encoding = "utf-8"
mode = "monitor"       # idle / monitor（serve 启动后台收数据）
log = "serial.log"     # monitor 输出文件（默认 stderr）
hex = false
timestamp = true
```

```bash
focdbg plugins                       # 可用 / 已配置插件
focdbg serial list                   # 枚举串口
focdbg serial send "hello" --port COM4 --baud 115200 --expect ok
focdbg serial send "DE AD BE EF" --port COM4 --hex --json
focdbg serial monitor --port COM4 --baud 115200 --log uart.log
focdbg serve                         # 配置了 [plugins.serial] 时同时起 GDB + 采样 + 串口
```

发送参数覆盖：`--baud --bytesize --parity --stopbits --flow --timeout --encoding --dtr --rts`；
`--expect` / `--expect-hex` 用于请求-应答，超时返回非 0。

自定义插件：

```python
from focdbg.plugins.base import Plugin

class MyPlugin(Plugin):
    name = "myplugin"
    description = "example"
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def commands(self):        # 可选：给 console / GUI 的命令
        return {}
```

注册为 entry point：

```toml
[project.entry-points."focdbg.plugins"]
myplugin = "mypkg.myplugin:MyPlugin"
```

## Python API（GUI 的接入点）

```python
from focdbg import Config, DebugSession

session = DebugSession(Config.load())
session.on_stop(lambda event: print("stopped:", event.summary))   # 读线程回调，别阻塞

with session:
    session.reset(halt=True)
    session.set_breakpoint("main")
    session.resume()
    event = session.wait_for_stop(timeout=5)
    print(event.reason, event.frame.short)
    print(session.read_registers(["pc", "sp"]))
    print(session.read_memory(0x20000000, 16).hex())
    session.flash("build/Debug/foc.elf", run=False)
```

`CommandInterpreter(session)` 可直接嵌进 GUI，做成命令面板或脚本窗口：

```python
from focdbg.console import CommandInterpreter

interp = CommandInterpreter(session)
result = interp.execute_line("read32 &g_foc_state 8")
if result.ok:
    print(result.text)      # 人类可读
    print(result.data)      # 结构化，给 UI / JSON
```

## 扩展新后端

`focdbg/backend.py` 里的 `Backend` 是抽象基类。新增 pyOCD 或 OpenOCD 后端只需实现
`open/close/reset/halt/resume/step/read_memory/write_memory/read_registers/...`
并在 `create_backend()` 注册，CLI 与 GUI 无需改动。

## 实现可靠性说明

- **J-Link 探针枚举**只走 `JLink.exe ShowEmuList`，不连接目标，板子运行时可安全执行。
- **不会在未确认的情况下连接目标**：`info`/`flash`/`console` 才会建立 SWD 连接；连接会 halt 内核。
- **校验**：J-Link GDB Server V8.10 不接受 `-verify`（会告警），因此烧写后用 GDB 的
  `compare-sections -r` 做校验，不匹配会返回非 0 退出码。
- **断点**默认使用硬件断点（`-break-insert -h`），因为 Flash 上无法下软件断点。
- **超时**：连接 30 s；若服务器 15 s 无输出会提前报“探针未插入”，避免干等。
- **退出码**：0 成功，1 运行/目标错误，2 配置错误，130 Ctrl-C。

## 已知限制 / 下一步

- **RAM 采样已支持**（`sample` / `dump`）；RTT / SWO 仍未实现。>1 kHz 的捕获请走目标端环形缓冲。
- **不要在电流环 ISR 里打断点**，用采样代替。
- `flash` 后若烧的是另一个 ELF，符号会切换（符合 GDB 语义），但 `info` 里的 elf 字段仍显示启动时的路径。
- 未实现 `erase`、`watchpoint`、`trace`。
- 真机验证清单（接上板子后）：
  1. `doctor` 应显示 1 个探针；
  2. `info` 显示电压约 3.3 V、state=halted；
  3. `console -c "reg pc"` 的 PC 落在 `0x0800xxxx`；
  4. `flash --no-run` 后 `compare-sections` OK；
  5. `break main` + `go` + `wait` 能停在 main。
