# embdbg

[English](README.md) | 简体中文

面向 **Cortex-M** 的可脚本化 **J-Link 调试后端**。CLI 只是
`embdbg.DebugSession` 的薄封装，GUI / 测试脚本可以直接复用同一套 API。

```text
your CLI / GUI / pytest
      │ Python API
      ▼
embdbg.DebugSession ──► Backend (pluggable)
                          │
                  JLinkGdbBackend          JLinkProbe (DLL)
                  ├─ JLinkGDBServerCL.exe  ├─ memory read/write
                  ├─ arm-none-eabi-gdb     ├─ HSS
                  └─ GDB/MI                ├─ RTT
                                           └─ flash
```

## 功能

- **read / write**：支持字段、数组、位域、结构体指针链
  （`g_state.counter`、`cfg.tbl[3]`、`a->b->c`、`*p`、`0x20000208:float`）；
- **HSS**：按 DWARF 解析字段/结构体，高速采样，不停核；
- **RTT**：读 SEGGER RTT 上行通道；
- **flash**：默认走 J-Link DLL（和 Ozone 同级速度），也保留 GDB-server 路径；
- **serve / gdbserver**：给 Ozone 或 `arm-none-eabi-gdb` 提供 GDB RSP
  （断点、单步、实时采样）；
- **serial**：串口助手 + 插件；
- 不解析源码、不用 LLM：符号/类型全部来自 ELF/DWARF。

## 文档

- [Flash：J-Link DLL vs GDB server、复位时钟](docs/flash.md)
- [符号解析：DWARF、指针、字面地址](docs/symbols.md)
- [HSS：字段布局与采样率选择](docs/hss.md)
- [RTT：固件集成与工具用法](docs/rtt.md)

## 安装

```bash
pip install embdbg                   # 基础（GDB 后端）
pip install "embdbg[serve,serial]"   # pylink-square + pyelftools + pyserial
```

依赖：

- GNU Arm 工具链（`arm-none-eabi-gdb`）——GDB 后端、`serve`；
- SEGGER J-Link 软件（`JLinkGDBServerCL.exe`、`JLink.exe`）——探针访问；
- Python ≥ 3.10。

## 快速开始

```bash
embdbg doctor                    # 工具链 / 配置 / 探针检查
embdbg probes                    # 列 J-Link（只动 USB，板子跑着也安全）
embdbg info                      # 连接并打印目标信息

embdbg read  --expr "g_state.counter"
embdbg write --expr "g_state.counter" --value 42
embdbg read  --expr "g_a.b->c->v"
embdbg read  --expr "*g_state.pf"
embdbg read  --expr "0x20000208:float"

embdbg flash build/Debug/firmware.elf --no-run     # J-Link DLL，几秒
embdbg reset --run

embdbg hss --expr "g_state.counter" --rate 1000 --duration 2 --out cap.csv
embdbg rtt --channel 0 --duration 2

embdbg serve                     # Ozone / gdb: target extended-remote :2331
```

## 配置

把 [`embdbg.toml`](embdbg.toml) 复制到固件仓库并修改；CLI 会在当前目录或
`EMBDBG_CONFIG` 指定的路径查找。所有项都能在命令行覆盖。

```toml
[embdbg]
device = "STM32G431CB"           # J-Link 器件名
interface = "SWD"                # SWD | JTAG
speed_khz = 4000

run_after_flash = false          # flash 的 --run/--no-run 默认值
elf = "build/Debug/firmware.elf" # DWARF 符号
verify = true                    # 烧写后校验

# 可选工具路径（留空自动发现）
# gdb = "C:/path/to/arm-none-eabi-gdb.exe"
# jlink_server = "C:/Program Files/SEGGER/JLink/JLinkGDBServerCL.exe"
# jlink_exe = "C:/Program Files/SEGGER/JLink/JLink.exe"
# base_port = 2331
# probe_serial = "123456789"
singlerun = true
```

通用参数：`--config`、`--device`、`--interface`、`--speed`、`--elf`、
`--probe-serial`、`--json`、`-v`。

## 命令

### read / write

表达式用 ELF DWARF 解析，之后通过 J-Link DLL 读写。**符号读不停核**；
指针目标在运行时通过 DLL 读。

| 表达式 | 含义 |
|---|---|
| `g_state.counter` | 全局变量 / 结构体成员 |
| `g_state.tbl[3]` | 数组元素（可嵌套 `[]`/`.`） |
| `g_a.b->c->v` | 结构体指针链（`->`，任意深度） |
| `*g_state.pf` | 标量指针解引用（前缀 `*`，可重复） |
| `0x20000208:float` | 字面地址 + 类型（默认 `u32`） |
| `g_state.bits.bit_a` | 位域（写时读-改-写） |

字面类型：`u8 i8 u16 i16 u32 i32 u64 i64 f32 f64 float double`。

`--value` 按解析出的类型解释：整数支持十进制、`0x`、`0b`；`f32/f64` 支持
浮点；位域按 mask/shift 合并。写后自动读回，不一致时退出码非 0。

```bash
embdbg read  --expr "g_state.arr_i32[3]"
# g_state.arr_i32[3] = 1300 (0x00000514) [i32]

embdbg write --expr "g_state.p_inner->x" --value 0xBEEF
# g_state.p_inner->x = 48879 (0xBEEF) [u16]  OK
```

### hss / rtt / flash

```bash
embdbg hss --list-fields --struct "g_motor.state"        # 只看布局
embdbg hss --struct "g_motor.state" --rate 1000 --duration 2 --out cap.csv
embdbg rtt --channel 0 --duration 2 --out rtt.bin
embdbg flash firmware.elf --no-run
embdbg flash app.bin --base 0x08000000
embdbg flash firmware.elf --gdb-flash   # 慢路径，仅对比用
```

细节见 [HSS](docs/hss.md)、[RTT](docs/rtt.md)、[Flash](docs/flash.md)。

### serve / gdbserver（断点调试）

```bash
embdbg serve --port 2331
embdbg gdbserver --port 2331 --timeout 60
```

Ozone 或 `arm-none-eabi-gdb` 用 `target extended-remote 127.0.0.1:2331` 连接。
GDB server 与 DLL 命令互斥：跑 `read`/`hss`/`rtt`/`flash` 前先停掉它。

### serial / plugins

```bash
embdbg serial list
embdbg serial send "help\r\n" --port COM9 --baud 2000000 --expect "OK"
embdbg serial monitor --port COM9 --duration 2
embdbg plugins --json
```

`--port` 也接受 pyserial URL（`loop://`、`socket://host:port`）。

## 符号解析原理

1. **先查 DWARF**：pyelftools 读取变量地址、结构体布局、成员字节/位偏移、
   数组边界、基础类型；
2. **指针在运行时取值**：`a->b->c`、`*p` 先用 J-Link DLL 读出指针值，再加
   DWARF 偏移——静态类型、动态地址；
3. **内存访问走 J-Link DLL**：Cortex-M AHB-AP 读不会停核，
   `read/write/hss/rtt` 都是非侵入式；
4. **GDB 可选**：`serve/gdbserver` 用；`read/write` 不需要 GDB，也不停核。
   不解析 C 源码、不用 LLM，只用 DWARF。

## 测试

```bash
python -m venv .venv
.venv/Scripts/pip install -q pytest pyelftools pylink-square pyserial
.venv/Scripts/python -m pytest -q      # 96 passed
```

`tests/test_embdbg_readwrite.py` 用固件 ELF 的测试夹具覆盖标量/数组/嵌套
结构/位域/结构体指针链/`*p`/字面地址，以及类型化编解码；ELF 不存在时自动
skip。

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| 烧写约 50s | 用了 `--gdb-flash`；去掉即可（DLL 约 5s） |
| `RAMCode did not respond` | flash loader 没在复位时钟下跑；DLL 路径会自动先复位 |
| `ELF file not found` | `--elf` 或配置里的路径失效 |
| 探针打不开 / connect 失败 | Ozone 或 `serve`/`gdbserver` 还占着探针 |
| `RTT Control Block not found` | 固件没调 `SEGGER_RTT_Init()`，或 ELF 与烧写镜像不匹配 |
| read 报聚合错误 | 用标量成员，或 `hss --struct` |
| 写入不匹配 | 符号/宽度/位域 mask；看错误里的类型 |

## 限制

- `read/write` 覆盖静态变量、结构体/数组成员、位域、结构体指针链、前缀 `*`
  和字面地址；任意 C 表达式（cast/算术）不解析——用 `0xADDR:type` 或 GDB；
- HSS 的指针目标在开始时解析一次，采样过程中指针变化不会重新求值；
- J-Link DLL 是快路径；GDB flash 路径只为对比/调试保留。
