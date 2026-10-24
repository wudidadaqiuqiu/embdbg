"""pyOCD-commander-style command interpreter.

The interpreter only depends on :class:`~focdbg.session.DebugSession`, so a GUI
can reuse it verbatim for a command palette / scripting console.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import Callable

from .errors import FocdbgError
from .models import CommandResult, RegisterValue, StopEvent
from .session import DebugSession
from .util import format_addr, hexdump, parse_int


@dataclass
class _Command:
    handler: Callable[[list[str]], CommandResult]
    help: str
    usage: str = ""


class CommandInterpreter:
    def __init__(self, session: DebugSession, *, json_mode: bool = False):
        self.session = session
        self.json_mode = json_mode
        self.exit_requested = False
        self._commands: dict[str, _Command] = {}
        self._aliases: dict[str, str] = {}
        self._register_commands()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def execute_line(self, line: str) -> CommandResult:
        line = line.strip()
        if not line or line.startswith("#"):
            return CommandResult(ok=True)
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as exc:
            return CommandResult(ok=False, error=f"cannot parse command: {exc}")
        if not parts:
            return CommandResult(ok=True)
        name = parts[0].lower()
        name = self._aliases.get(name, name)
        command = self._commands.get(name)
        if command is None:
            return CommandResult(ok=False, error=f"unknown command {parts[0]!r} (try 'help')")
        if name == "gdb":
            raw = line[len(parts[0]) :].strip()
            return self._run(command, [raw] if raw else [])
        return self._run(command, parts[1:])

    def execute(self, name: str, args: list[str] | None = None) -> CommandResult:
        key = self._aliases.get(name.lower(), name.lower())
        command = self._commands.get(key)
        if command is None:
            return CommandResult(ok=False, error=f"unknown command {name!r}")
        return self._run(command, args or [])

    def command_names(self) -> list[str]:
        return sorted(self._commands)

    def help_text(self, name: str | None = None) -> str:
        if name:
            key = self._aliases.get(name.lower(), name.lower())
            cmd = self._commands.get(key)
            if cmd is None:
                return f"unknown command {name!r}"
            usage = f" {cmd.usage}" if cmd.usage else ""
            return f"{key}{usage}\n    {cmd.help}"
        lines = ["Commands:"]
        for key in sorted(self._commands):
            cmd = self._commands[key]
            usage = f" {cmd.usage}" if cmd.usage else ""
            aliases = [a for a, target in self._aliases.items() if target == key]
            alias_text = f" ({', '.join(sorted(aliases))})" if aliases else ""
            lines.append(f"  {key}{usage}{alias_text}\n      {cmd.help}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    def _run(self, command: _Command, args: list[str]) -> CommandResult:
        try:
            return command.handler(args)
        except FocdbgError as exc:
            return CommandResult(ok=False, error=str(exc))
        except (ValueError, KeyError, IndexError) as exc:
            return CommandResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def _register(self, name: str, handler: Callable, help: str, usage: str = "", aliases: tuple[str, ...] = ()) -> None:
        self._commands[name] = _Command(handler, help, usage)
        for alias in aliases:
            self._aliases[alias] = name

    def _register_commands(self) -> None:
        self._register("help", self.cmd_help, "show help", "[command]", aliases=("?",))
        self._register("halt", self.cmd_halt, "halt the target", aliases=("h",))
        self._register("go", self.cmd_go, "resume the target", aliases=("g", "continue", "c"))
        self._register("step", self.cmd_step, "step one source line", "[count]", aliases=("s",))
        self._register("next", self.cmd_next, "step over one source line", "[count]", aliases=("n",))
        self._register("reset", self.cmd_reset, "reset the target", "[halt|run]")
        self._register("reg", self.cmd_reg, "read registers", "[name ...]")
        self._register("wreg", self.cmd_wreg, "write a register", "<name> <value>")
        self._register("read8", self.cmd_read8, "read bytes", "<address|symbol> [count]")
        self._register("read16", self.cmd_read16, "read half-words", "<address|symbol> [count]")
        self._register("read32", self.cmd_read32, "read words", "<address|symbol> [count]")
        self._register("write8", self.cmd_write8, "write bytes", "<address|symbol> <value> ...")
        self._register("write16", self.cmd_write16, "write half-words", "<address|symbol> <value> ...")
        self._register("write32", self.cmd_write32, "write words", "<address|symbol> <value> ...")
        self._register("break", self.cmd_break, "set a breakpoint", "<location>", aliases=("b",))
        self._register("delete", self.cmd_delete, "delete breakpoint(s)", "[number|all]", aliases=("d",))
        self._register("info", self.cmd_info, "show registers/breakpoints/threads/target", "<topic>")
        self._register("bt", self.cmd_bt, "show a backtrace", aliases=("where",))
        self._register("frame", self.cmd_frame, "select a stack frame", "<level>")
        self._register("print", self.cmd_print, "evaluate an expression", "<expression>", aliases=("p",))
        self._register("symbol", self.cmd_symbol, "resolve a symbol to an address", "<name>")
        self._register("disassemble", self.cmd_disassemble, "disassemble code", "[start] [end]", aliases=("disasm",))
        self._register("threads", self.cmd_threads, "list threads")
        self._register("wait", self.cmd_wait, "wait for the target to stop", "[timeout]")
        self._register("sleep", self.cmd_sleep, "sleep (useful in scripts)", "<milliseconds>")
        self._register("json", self.cmd_json, "toggle JSON result printing", "[on|off]")
        self._register("gdb", self.cmd_gdb, "run a raw GDB command", "<command>")
        self._register("exit", self.cmd_exit, "quit", aliases=("quit", "q"))

    # ------------------------------------------------------------------
    # execution control
    # ------------------------------------------------------------------
    def cmd_help(self, args: list[str]) -> CommandResult:
        text = self.help_text(args[0] if args else None)
        return CommandResult(ok=True, text=text, data={"commands": self.command_names()})

    def cmd_halt(self, args: list[str]) -> CommandResult:
        event = self.session.halt()
        text = "target halted" if event is None else event.summary
        return CommandResult(ok=True, text=text, data={"stop": event})

    def cmd_go(self, args: list[str]) -> CommandResult:
        self.session.resume()
        return CommandResult(ok=True, text="running", data={"running": True})

    def cmd_step(self, args: list[str]) -> CommandResult:
        return self._step(args, into=True)

    def cmd_next(self, args: list[str]) -> CommandResult:
        return self._step(args, into=False)

    def _step(self, args: list[str], *, into: bool) -> CommandResult:
        count = parse_int(args[0]) if args else 1
        event = self.session.step(into=into, count=count)
        return CommandResult(
            ok=True,
            text=self._format_stop(event),
            data={"stop": event, "pc": self._pc()},
        )

    def cmd_reset(self, args: list[str]) -> CommandResult:
        mode = args[0].lower() if args else "halt"
        if mode not in ("halt", "run"):
            raise ValueError("reset expects 'halt' or 'run'")
        self.session.reset(halt=(mode == "halt"))
        return CommandResult(ok=True, text=f"reset ({mode})", data={"halt": mode == "halt"})

    # ------------------------------------------------------------------
    # registers / memory
    # ------------------------------------------------------------------
    def cmd_reg(self, args: list[str]) -> CommandResult:
        registers = self.session.read_registers(args or None)
        if not registers:
            return CommandResult(ok=False, error="no matching registers")
        return CommandResult(
            ok=True,
            text=self._format_registers(registers),
            data={"registers": list(registers.values())},
        )

    def cmd_wreg(self, args: list[str]) -> CommandResult:
        if len(args) != 2:
            raise ValueError("usage: wreg <name> <value>")
        self.session.write_register(args[0], self._int(args[1]))
        return CommandResult(ok=True, text=f"{args[0]} = {args[1]}", data={"name": args[0], "value": args[1]})

    def cmd_read8(self, args: list[str]) -> CommandResult:
        return self._read(args, 1)

    def cmd_read16(self, args: list[str]) -> CommandResult:
        return self._read(args, 2)

    def cmd_read32(self, args: list[str]) -> CommandResult:
        return self._read(args, 4)

    def _read(self, args: list[str], width: int) -> CommandResult:
        if not args:
            raise ValueError("usage: read<width> <address|symbol> [count]")
        address = self._address(args[0])
        count = parse_int(args[1]) if len(args) > 1 else 1
        if count <= 0:
            raise ValueError("count must be positive")
        data = self.session.read_memory(address, width * count)
        values = [
            int.from_bytes(data[i : i + width], "little") for i in range(0, len(data) - width + 1, width)
        ]
        if width == 1:
            text = hexdump(data, address)
        else:
            text = "\n".join(
                f"{format_addr(address + i * width)}: 0x{v:0{width * 2}x}" for i, v in enumerate(values)
            )
        return CommandResult(
            ok=True,
            text=text,
            data={"address": address, "width": width, "count": count, "values": values},
        )

    def cmd_write8(self, args: list[str]) -> CommandResult:
        return self._write(args, 1)

    def cmd_write16(self, args: list[str]) -> CommandResult:
        return self._write(args, 2)

    def cmd_write32(self, args: list[str]) -> CommandResult:
        return self._write(args, 4)

    def _write(self, args: list[str], width: int) -> CommandResult:
        if len(args) < 2:
            raise ValueError("usage: write<width> <address|symbol> <value> ...")
        address = self._address(args[0])
        values = [self._int(v) for v in args[1:]]
        data = b"".join(v.to_bytes(width, "little", signed=False) for v in values)
        self.session.write_memory(address, data)
        return CommandResult(
            ok=True,
            text=f"wrote {len(values)} x {width * 8}-bit to {format_addr(address)}",
            data={"address": address, "width": width, "values": values},
        )

    # ------------------------------------------------------------------
    # breakpoints
    # ------------------------------------------------------------------
    def cmd_break(self, args: list[str]) -> CommandResult:
        hardware: bool | None = None
        if args and args[0] in ("-h", "--hw"):
            hardware = True
            args = args[1:]
        elif args and args[0] in ("-s", "--sw"):
            hardware = False
            args = args[1:]
        if not args:
            raise ValueError("usage: break [-h|-s] <location>")
        if args[0].lower() in ("list", "ls"):
            return self.cmd_info(["breakpoints"])
        bp = self.session.set_breakpoint(" ".join(args), hardware=hardware)
        return CommandResult(ok=True, text=f"breakpoint {bp.number}: {bp.describe()}", data={"breakpoint": bp})

    def cmd_delete(self, args: list[str]) -> CommandResult:
        breakpoints = self.session.list_breakpoints()
        if not args or args[0].lower() == "all":
            for bp in breakpoints:
                self.session.remove_breakpoint(bp.number)
            return CommandResult(ok=True, text=f"deleted {len(breakpoints)} breakpoint(s)", data={"deleted": len(breakpoints)})
        number = parse_int(args[0])
        self.session.remove_breakpoint(number)
        return CommandResult(ok=True, text=f"deleted breakpoint {number}", data={"deleted": number})

    # ------------------------------------------------------------------
    # inspection
    # ------------------------------------------------------------------
    def cmd_info(self, args: list[str]) -> CommandResult:
        topic = args[0].lower() if args else "target"
        if topic in ("reg", "registers"):
            return self.cmd_reg(args[1:])
        if topic in ("break", "breakpoints", "bp"):
            breakpoints = self.session.list_breakpoints()
            if breakpoints:
                text = "\n".join(f"  {bp.describe()}" for bp in breakpoints)
            else:
                text = "no breakpoints"
            return CommandResult(ok=True, text=text, data={"breakpoints": breakpoints})
        if topic in ("thread", "threads"):
            return self.cmd_threads([])
        if topic in ("frame", "frames"):
            return self.cmd_bt([])
        if topic in ("target", "session", "connection"):
            info = self.session.target_info()
            conn = self.session.connection
            lines = [
                f"backend:    {conn.backend}",
                f"target:     {info.device} via {info.interface} @ {info.speed_khz} kHz",
                f"state:      {info.state}",
                f"pc:         {format_addr(info.pc) if info.pc is not None else '-'}",
                f"voltage:    {info.voltage if info.voltage is not None else '-'} V",
                f"gdb server: {conn.server_path} (pid {conn.server_pid}, port {conn.server_port})",
                f"gdb:        {conn.gdb_path} (pid {conn.gdb_pid})",
                f"elf:        {conn.elf or '-'}",
            ]
            return CommandResult(ok=True, text="\n".join(lines), data={"target": info, "connection": conn})
        raise ValueError(f"unknown info topic {topic!r} (registers|breakpoints|threads|target)")

    def cmd_bt(self, args: list[str]) -> CommandResult:
        frames = self.session.backtrace()
        if not frames:
            return CommandResult(ok=False, error="no stack frames")
        text = "\n".join(
            f"#{frame.level:<2} {format_addr(frame.addr) if frame.addr is not None else '????????'} in {frame.func or '??'} () at {frame.file or '??'}:{frame.line or '?'}"
            for frame in frames
        )
        return CommandResult(ok=True, text=text, data={"frames": frames})

    def cmd_frame(self, args: list[str]) -> CommandResult:
        if not args:
            raise ValueError("usage: frame <level>")
        level = parse_int(args[0])
        self.session.select_frame(level)
        frames = self.session.backtrace()
        current = next((f for f in frames if f.level == level), None)
        return CommandResult(
            ok=True,
            text=f"frame {level}: {current.short if current else '?'}",
            data={"frame": current},
        )

    def cmd_threads(self, args: list[str]) -> CommandResult:
        threads = self.session.threads()
        text = "\n".join(
            f"  {t.id}{' (' + t.name + ')' if t.name else ''} {t.state or ''} {t.frame.short if t.frame else ''}".rstrip()
            for t in threads
        ) or "no threads"
        return CommandResult(ok=True, text=text, data={"threads": threads})

    def cmd_print(self, args: list[str]) -> CommandResult:
        if not args:
            raise ValueError("usage: print <expression>")
        expression = " ".join(args)
        value = self.session.evaluate(expression)
        return CommandResult(ok=True, text=f"{expression} = {value}", data={"expression": expression, "value": value})

    def cmd_symbol(self, args: list[str]) -> CommandResult:
        if not args:
            raise ValueError("usage: symbol <name>")
        address = self.session.resolve_symbol(args[0])
        return CommandResult(ok=True, text=f"{args[0]} = {format_addr(address)}", data={"symbol": args[0], "address": address})

    def cmd_disassemble(self, args: list[str]) -> CommandResult:
        command = "disassemble"
        if args:
            command += " " + " ".join(args)
        text = self.session.gdb(command)
        return CommandResult(ok=True, text=text.rstrip(), data={"command": command})

    def cmd_gdb(self, args: list[str]) -> CommandResult:
        if not args or not args[0]:
            raise ValueError("usage: gdb <command>")
        text = self.session.gdb(args[0])
        return CommandResult(ok=True, text=text.rstrip(), data={"command": args[0]})

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------
    def cmd_wait(self, args: list[str]) -> CommandResult:  # noqa: F811 - intentional override order
        timeout = float(args[0]) if args else None
        event = self.session.wait_for_stop(timeout)
        return CommandResult(ok=True, text=self._format_stop(event), data={"stop": event})

    def cmd_sleep(self, args: list[str]) -> CommandResult:
        if not args:
            raise ValueError("usage: sleep <milliseconds>")
        ms = parse_int(args[0])
        time.sleep(max(0, ms) / 1000.0)
        return CommandResult(ok=True, text=f"slept {ms} ms", data={"ms": ms})

    def cmd_json(self, args: list[str]) -> CommandResult:
        if args:
            self.json_mode = args[0].lower() in ("on", "1", "true", "yes")
        return CommandResult(ok=True, text=f"json {'on' if self.json_mode else 'off'}", data={"json": self.json_mode})

    def cmd_exit(self, args: list[str]) -> CommandResult:
        self.exit_requested = True
        return CommandResult(ok=True, text="bye", data={"exit": True})

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _address(self, token: str) -> int:
        if token.startswith("&"):
            return self.session.resolve_symbol(token[1:])
        try:
            return parse_int(token)
        except ValueError:
            return self.session.resolve_symbol(token)

    def _int(self, token: str) -> int:
        try:
            return parse_int(token)
        except ValueError as exc:
            raise ValueError(f"expected an integer, got {token!r}") from exc

    def _pc(self) -> int | None:
        try:
            # GDB may annotate the value, e.g. "0x80038e2 <HAL_Init+10>".
            return int(self.session.evaluate("$pc").split(" ", 1)[0], 0)
        except (FocdbgError, ValueError, IndexError):
            return None

    def _format_stop(self, event: StopEvent) -> str:
        lines = [f"stopped: {event.summary}"]
        if event.frame is not None and event.frame.func:
            frame = event.frame
            location = f"{frame.file}:{frame.line}" if frame.file else "?"
            lines.append(f"  at {location}")
        return "\n".join(lines)

    def _format_registers(self, registers: dict[str, RegisterValue]) -> str:
        items = list(registers.values())
        rows = []
        for i in range(0, len(items), 4):
            rows.append("  ".join(f"{r.name:<6} {r.hex:>10}" for r in items[i : i + 4]))
        return "\n".join(rows)
