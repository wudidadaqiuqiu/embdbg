"""Command line interface.

    embdbg probes / info / doctor
    embdbg flash <file> / reset
    embdbg gdbserver / serve        (GDB RSP for Ozone / plain gdb, breakpoints)
    embdbg serial / plugins
    embdbg hss                      (high-speed DWARF-aware sampling)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config
from .discovery import find_elf, find_gdb, find_jlink_exe, find_jlink_server, gdb_version, list_probes
from .errors import ConfigError, FocdbgError, TargetError, ToolNotFoundError
from .dwarf_symbols import DwarfSymbols, ResolvedField
from .elf_symbols import ElfSymbols
from .gdbserver import JLinkGDBServer
from .jlink_probe import JLinkProbe
from .rsp import RspServer
from .models import to_dict
from .plugin_cli import add_serial_options, cmd_plugins, cmd_serial
from .plugins.base import PluginContext, PluginManager
from .sampler import Sampler, csv_header, decode_fields, fields_size, format_sample, parse_fields
from .session import DebugSession
from .util import find_free_ports, hexdump

_OVERRIDE_KEYS = (
    "device",
    "interface",
    "speed_khz",
    "elf",
    "gdb",
    "jlink_server",
    "jlink_exe",
    "base_port",
    "probe_serial",
)


# ----------------------------------------------------------------------
# argument parsing
# ----------------------------------------------------------------------
def _add_common(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    default = argparse.SUPPRESS if suppress else None
    flag_default = argparse.SUPPRESS if suppress else False
    parser.add_argument("--config", default=default, help="path to embdbg.toml")
    parser.add_argument("--device", default=default, help="target device (e.g. STM32G431CB)")
    parser.add_argument("--interface", default=default, choices=("SWD", "JTAG"), help="debug interface")
    parser.add_argument("--speed", dest="speed_khz", type=int, default=default, help="interface speed in kHz")
    parser.add_argument("--elf", default=default, help="ELF file with symbols")
    parser.add_argument("--gdb", default=default, help="path to arm-none-eabi-gdb")
    parser.add_argument("--jlink-server", default=default, help="path to JLinkGDBServerCL")
    parser.add_argument("--jlink-exe", default=default, help="path to JLink.exe")
    parser.add_argument("--base-port", type=int, default=default, help="first TCP port to try")
    parser.add_argument("--probe-serial", default=default, help="select a J-Link by serial number")
    parser.add_argument("--json", action="store_true", default=flag_default, help="machine-readable output")
    parser.add_argument("--server-log", action="store_true", default=flag_default, help="keep the J-Link GDB server log")
    parser.add_argument("-v", "--verbose", action="store_true", default=flag_default, help="verbose diagnostics")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embdbg",
        description="Scriptable J-Link debug backend (J-Link GDB server + arm-none-eabi-gdb).",
    )
    parser.add_argument("--version", action="version", version=f"embdbg {__version__}")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probes", aliases=["list"], help="list connected J-Link probes")
    _add_common(p, suppress=True)

    p = sub.add_parser("doctor", help="check the local toolchain and configuration")
    _add_common(p, suppress=True)

    p = sub.add_parser("info", help="connect and show target/session information")
    _add_common(p, suppress=True)

    p = sub.add_parser("flash", help="program an ELF/HEX/BIN into the target")
    p.add_argument("file", help="firmware image")
    p.add_argument("--base", default=None, help="load address (required for .bin)")
    p.add_argument("--run", dest="run", action="store_true", default=None, help="run after flashing")
    p.add_argument("--no-run", dest="run", action="store_false", help="stay halted after flashing")
    p.add_argument("--verify", dest="verify", action="store_true", default=None, help="verify after flashing")
    p.add_argument("--no-verify", dest="verify", action="store_false", help="skip verification")
    _add_common(p, suppress=True)

    p = sub.add_parser("reset", help="reset the target")
    p.add_argument("--run", action="store_true", help="run instead of staying halted")
    _add_common(p, suppress=True)

    p = sub.add_parser("gdbserver", help="run only the J-Link GDB server (for IDEs / plain gdb)")
    p.add_argument("--port", type=int, default=None, help="explicit TCP port")
    p.add_argument("--timeout", type=float, default=None, help="seconds to wait for a client")
    _add_common(p, suppress=True)

    p = sub.add_parser("plugins", help="list available and configured plugins")
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    p = sub.add_parser("serial", help="serial port plugin: list / send / monitor")
    serial_sub = p.add_subparsers(dest="serial_cmd", required=True)

    sp = serial_sub.add_parser("list", help="list serial ports")
    sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    sp = serial_sub.add_parser("send", help="send text/hex/file, optionally wait for a reply")
    sp.add_argument("data", nargs="?", default="")
    sp.add_argument("--file", default=None)
    sp.add_argument("--hex", action="store_true")
    sp.add_argument("--newline", choices=("lf", "crlf", "none"), default=None)
    sp.add_argument("--expect", default=None)
    sp.add_argument("--expect-hex", default=None)
    sp.add_argument("--expect-timeout", type=float, default=2.0)
    add_serial_options(sp)

    sp = serial_sub.add_parser("monitor", help="print received data until Ctrl-C / --duration")
    sp.add_argument("--duration", type=float, default=None)
    sp.add_argument("--log", default=None)
    sp.add_argument("--hex", dest="hex", action="store_true", default=None)
    sp.add_argument("--timestamp", dest="timestamp", action="store_true", default=None)
    sp.add_argument("--no-timestamp", dest="timestamp", action="store_false")
    add_serial_options(sp)

    p = sub.add_parser(
        "serve",
        aliases=["gdb"],
        help="one process owns the probe: GDB RSP server + live sampling (Ozone-style)",
    )
    p.add_argument("--host", default="127.0.0.1", help="RSP listen address")
    p.add_argument("--port", type=int, default=None, help="RSP TCP port (default: base_port)")
    p.add_argument("--sample", default=None, help="symbol to sample continuously")
    p.add_argument("--sample-address", default=None, help="raw address instead of --sample")
    p.add_argument("--sample-fields", default=None, help="layout: name:type[@offset],...")
    p.add_argument("--sample-size", type=int, default=None, help="bytes per sample")
    p.add_argument("--rate", type=float, default=100.0, help="samples per second (default 100)")
    p.add_argument("--sample-out", default=None, help="CSV/JSONL file (default stdout)")
    p.add_argument("--sample-format", choices=("csv", "jsonl"), default="csv")
    p.add_argument("--plugin", action="append", default=[], help="extra plugin to load (repeatable)")
    _add_common(p, suppress=True)

    p = sub.add_parser(
        "hss",
        help="live sample variables at high speed via J-Link DLL (DWARF-aware, no halt)",
    )
    p.add_argument(
        "--expr",
        action="append",
        default=[],
        metavar="EXPR",
        help="DWARF expression, e.g. sys.foc_controller.speed (repeatable)",
    )
    p.add_argument(
        "--struct",
        action="append",
        default=[],
        metavar="EXPR",
        help="expand all scalar members of a struct expression (repeatable)",
    )
    p.add_argument("--recursive", action="store_true", help="recursively expand nested structs/arrays")
    p.add_argument("--list-fields", action="store_true", help="print DWARF layout and exit")
    p.add_argument("--rate", type=float, default=500.0, help="samples per second (default 500)")
    p.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    p.add_argument("--count", type=int, default=None, help="stop after N samples")
    p.add_argument("--out", default=None, help="write CSV/JSONL here instead of stdout")
    p.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    p.add_argument("--no-run", action="store_true", help="leave the core halted (default: resume)")
    p.add_argument("--no-native", action="store_true",
                   help="disable the probe-side HSS and sample manually (slow, per-sample USB)")
    p.add_argument("--leave-halted", action="store_true", help="halt the core when sampling ends")
    p.add_argument("--max-span", type=int, default=512, help="max bytes per coalesced read (default 512)")
    p.add_argument("--coalesce-gap", type=int, default=32, help="max gap in bytes between fields to coalesce")
    _add_common(p, suppress=True)

    return parser


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    # never let the debug tool crash on console codecs (GBK/CP936)
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
        sys.stderr.reconfigure(errors="backslashreplace")
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except FocdbgError as exc:
        _report_error(exc, getattr(args, "json", False))
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - last-resort guard
        import traceback

        print(f"embdbg: internal error: {exc}", file=sys.stderr)
        if getattr(args, "verbose", False):
            traceback.print_exc()
        return 70


def _dispatch(args: argparse.Namespace) -> int:
    handlers = {
        "probes": _cmd_probes,
        "list": _cmd_probes,
        "doctor": _cmd_doctor,
        "info": _cmd_info,
        "flash": _cmd_flash,
        "reset": _cmd_reset,
        "gdbserver": _cmd_gdbserver,
        "plugins": _cmd_plugins,
        "serial": _cmd_serial,
        "serve": _cmd_serve,
        "gdb": _cmd_serve,
        "hss": _cmd_hss,
    }
    return handlers[args.command](args)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _make_config(args: argparse.Namespace) -> Config:
    overrides = {key: getattr(args, key) for key in _OVERRIDE_KEYS if hasattr(args, key)}
    if getattr(args, "server_log", False):
        overrides["keep_server_log"] = True
    return Config.load(getattr(args, "config", None), overrides)


def _open_session(args: argparse.Namespace) -> DebugSession:
    config = _make_config(args)
    if config.elf is None:
        discovered = find_elf(None, Path.cwd())
        if discovered is not None:
            config.elf = str(discovered)
            config.validate()
    if getattr(args, "verbose", False):
        print(
            f"embdbg: device={config.device} interface={config.interface} "
            f"speed={config.speed_khz}kHz elf={config.elf}",
            file=sys.stderr,
        )
    session = DebugSession(config)
    session.connect()
    return session


def _print_json(payload: dict[str, Any]) -> None:
    """Print JSON without ever failing on the console codec.

    Windows consoles are often GBK; vendor DLL messages can contain
    characters that cannot be encoded, which used to mask the real
    error with a UnicodeEncodeError.
    """
    try:
        print(json.dumps(payload, ensure_ascii=False))
    except UnicodeEncodeError:
        print(json.dumps(payload, ensure_ascii=True))


def _emit(payload: Any, text: str, json_mode: bool) -> None:
    if json_mode:
        _print_json(to_dict(payload))
    elif text:
        print(text)


def _report_error(exc: FocdbgError, json_mode: bool) -> None:
    if json_mode:
        payload: dict[str, Any] = {"ok": False, "error": exc.message}
        if exc.detail:
            payload["detail"] = exc.detail
        _print_json(payload)
    else:
        print(f"embdbg: error: {exc.message}", file=sys.stderr)
        if exc.detail:
            for line in str(exc.detail).splitlines():
                print(f"  | {line}", file=sys.stderr)


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def _cmd_probes(args: argparse.Namespace) -> int:
    config = _make_config(args)
    jlink = find_jlink_exe(config.jlink_exe)
    probes = list_probes(jlink)
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        print(json.dumps({"ok": True, "probes": to_dict(probes)}))
    else:
        if not probes:
            print("no J-Link probes found")
        for probe in probes:
            print(f"#{probe.index}  S/N {probe.serial}  {probe.product}  ({probe.connection})")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    config = _make_config(args)
    report: dict[str, Any] = {"ok": True, "checks": {}}
    checks = report["checks"]

    def record(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": ok, "detail": detail}
        if not ok:
            report["ok"] = False

    try:
        gdb = find_gdb(config.gdb)
        record("gdb", True, f"{gdb} [{gdb_version(gdb) or 'version unknown'}]")
    except FocdbgError as exc:
        record("gdb", False, str(exc))
    try:
        server = find_jlink_server(config.jlink_server)
        record("jlink_server", True, str(server))
    except FocdbgError as exc:
        record("jlink_server", False, str(exc))
    try:
        exe = find_jlink_exe(config.jlink_exe)
        probes = list_probes(exe)
        detail = f"{exe}; {len(probes)} probe(s)"
        if probes:
            detail += " -> " + ", ".join(f"S/N {p.serial}" for p in probes)
        record("jlink_probe", bool(probes), detail)
    except FocdbgError as exc:
        record("jlink_probe", False, str(exc))

    discovered = find_elf(None, Path.cwd())
    elf = config.elf or (str(discovered) if discovered else None)
    record("elf", bool(elf and Path(elf).is_file()), elf or "not found (pass --elf)")
    record("device", bool(config.device), config.device or "not configured (pass --device)")
    record("config", True, config.config_path or "defaults + environment")

    if json_mode:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for name, item in checks.items():
            mark = "OK  " if item["ok"] else "FAIL"
            print(f"[{mark}] {name:<14} {item['detail']}")
        print(f"config: {config.config_path or 'defaults + environment'}")
    return 0 if report["ok"] else 1


def _cmd_info(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    session = _open_session(args)
    try:
        info = session.target_info()
        conn = session.connection
        if json_mode:
            print(json.dumps({"ok": True, "target": to_dict(info), "connection": to_dict(conn)}))
        else:
            print(f"backend:    {conn.backend}")
            print(f"target:     {info.device} via {info.interface} @ {info.speed_khz} kHz")
            print(f"state:      {info.state}")
            print(f"pc:         {to_hex(info.pc)}")
            print(f"voltage:    {info.voltage if info.voltage is not None else '-'} V")
            print(f"gdb server: {conn.server_path} (pid {conn.server_pid}, port {conn.server_port})")
            print(f"gdb:        {conn.gdb_path} (pid {conn.gdb_pid})")
            print(f"elf:        {conn.elf or '-'}")
    finally:
        session.disconnect()
    return 0


def to_hex(value: int | None) -> str:
    return f"0x{value:08x}" if value is not None else "-"


def _cmd_flash(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    base = None
    if args.base is not None:
        try:
            base = int(str(args.base), 0)
        except ValueError as exc:
            raise ConfigError(f"invalid --base: {args.base!r}") from exc
    session = _open_session(args)
    try:
        result = session.flash(args.file, base=base, run=args.run, verify=args.verify)
        if json_mode:
            print(json.dumps({"ok": True, "flash": to_dict(result)}))
        else:
            print(f"flashed {result.path} in {result.duration_s:.2f}s")
            if result.verified is True:
                print("verify: OK")
            elif result.verified is False:
                print(f"verify: MISMATCH in {len(result.mismatch_sections)} section(s)", file=sys.stderr)
                for line in result.mismatch_sections:
                    print(f"  {line}", file=sys.stderr)
                return 1
            print("target: running" if result.ran_after else "target: halted")
    finally:
        session.disconnect()
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    session = _open_session(args)
    try:
        session.reset(halt=not args.run)
        if json_mode:
            print(json.dumps({"ok": True, "halted": not args.run}))
        else:
            print("reset; target " + ("running" if args.run else "halted"))
    finally:
        session.disconnect()
    return 0


def _cmd_gdbserver(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    config = _make_config(args)
    if not config.device:
        raise ConfigError("no target device configured (pass --device)")
    server_exe = find_jlink_server(config.jlink_server)
    port = args.port if args.port else find_free_ports(config.base_port, 3)
    server = JLinkGDBServer(
        server_exe,
        device=config.device,
        interface=config.interface,
        speed_khz=config.speed_khz,
        port=port,
        base_port=config.base_port,
        singlerun=config.singlerun,
        verify=config.verify,
        probe_serial=config.probe_serial,
        extra_args=config.extra_server_args,
    )
    server.start()
    try:
        banner = server.info()
        spec = f"127.0.0.1:{port}"
        if json_mode:
            print(json.dumps({"ok": True, "port": port, "target_spec": spec, "info": banner}))
        else:
            print(f"J-Link GDB server listening on {spec}")
            print(f"target: {config.device} via {config.interface} @ {config.speed_khz} kHz")
            if banner.get("voltage") is not None:
                print(f"target voltage: {banner['voltage']} V")
            elf = config.elf or ""
            print("connect with:")
            print(f'  arm-none-eabi-gdb {elf} -ex "target extended-remote {spec}"'.strip())
        while server.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _cmd_plugins(args: argparse.Namespace) -> int:
    return cmd_plugins(args, _make_config(args))


def _cmd_serial(args: argparse.Namespace) -> int:
    return cmd_serial(args, _make_config(args))


# ----------------------------------------------------------------------
# one-process server: GDB RSP + sampling
# ----------------------------------------------------------------------
def _cmd_serve(args: argparse.Namespace) -> int:
    config = _make_config(args)
    if not config.device:
        raise ConfigError("no target device configured (pass --device)")
    probe = JLinkProbe(
        config.device,
        interface=config.interface,
        speed_khz=config.speed_khz,
        serial=config.probe_serial,
    )
    probe.open()
    server = None
    sampler_thread = None
    stop_event = threading.Event()
    sampler = None
    out = None
    manager = None
    try:
        info = {}
        try:
            info = probe.info()
        except Exception:
            pass
        print(
            f"J-Link: {info.get('product', 'J-Link')} S/N {info.get('serial', '?')}  "
            f"{config.device} {config.interface} {config.speed_khz} kHz",
            file=sys.stderr,
        )

        manager = PluginManager(
            PluginContext(config=config, probe=probe, log=lambda m: print(f"[plugin] {m}", file=sys.stderr))
        )
        for name in getattr(args, "plugin", None) or []:
            manager.load(name)
        manager.load_from_config(config)
        if manager.plugins:
            manager.start_all()
            print("plugins: " + ", ".join(sorted(manager.plugins)), file=sys.stderr)

        if args.sample or args.sample_address:
            if args.sample_address:
                address = int(str(args.sample_address), 0)
            else:
                if not config.elf:
                    raise ConfigError("--elf is required with --sample (symbol resolution)")
                address = ElfSymbols(config.elf).resolve(args.sample)
            fields = parse_fields(args.sample_fields) if args.sample_fields else []
            layout_size = fields_size(fields) if fields else 0
            size = args.sample_size if args.sample_size is not None else layout_size
            if size <= 0:
                raise ConfigError("--sample-size is required when --sample-fields is absent")
            if layout_size and size < layout_size:
                raise ConfigError(f"--sample-size {size} is smaller than the layout ({layout_size})")
            out = open(args.sample_out, "w", newline="", encoding="utf-8") if args.sample_out else sys.stdout
            if args.sample_format == "csv":
                print(csv_header(fields), file=out)

            def on_sample(sample) -> None:
                print(format_sample(sample, fields, args.sample_format), file=out)
                if sample.index % 64 == 0:
                    out.flush()

            sampler = Sampler(
                probe,
                address,
                size,
                fields,
                rate_hz=args.rate,
                halt=False,
                stop_event=stop_event,
            )

            def run_sampler() -> None:
                try:
                    sampler.run(on_sample)
                except Exception as exc:
                    print(f"sampling stopped: {exc}", file=sys.stderr)
                finally:
                    out.flush()

            sampler_thread = threading.Thread(target=run_sampler, name="embdbg-sampler", daemon=True)
            sampler_thread.start()
            target = args.sample or args.sample_address
            where = f" -> {args.sample_out}" if args.sample_out else ""
            print(f"sampling {target} @ {args.rate:g} Hz{where}", file=sys.stderr)

        port = args.port if args.port is not None else config.base_port
        server = RspServer(probe, host=args.host, port=port, on_event=lambda m: print(f"[rsp] {m}", file=sys.stderr))
        server.start()
        spec = f"{args.host}:{server.port}"
        print(f"GDB server listening on {spec}", file=sys.stderr)
        elf = config.elf or "<firmware.elf>"
        print(
            f"connect with: arm-none-eabi-gdb {elf} -ex \"target extended-remote {spec}\"",
            file=sys.stderr,
        )
        print("Ctrl-C to stop", file=sys.stderr)

        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if server is not None:
            server.stop()
        if sampler_thread is not None:
            sampler_thread.join(timeout=2)
        if out is not None and out is not sys.stdout:
            out.close()
        if manager is not None:
            manager.stop_all()
        probe.close()
    return 0


# ----------------------------------------------------------------------
# sampling / dumps
# ----------------------------------------------------------------------
def _target_address(session: DebugSession, args: argparse.Namespace) -> int:
    if args.symbol and args.address:
        raise ConfigError("use --symbol or --address, not both")
    if args.symbol:
        return session.resolve_symbol(args.symbol)
    if args.address:
        try:
            return int(str(args.address), 0)
        except ValueError as exc:
            raise ConfigError(f"invalid address {args.address!r}") from exc
    raise ConfigError("specify --symbol or --address")


def _hss_fields(dwarf: DwarfSymbols, exprs: list[str], structs: list[str],
                recursive: bool) -> list[ResolvedField]:
    fields: list[ResolvedField] = []
    seen: set[str] = set()
    for expr in exprs:
        field = dwarf.resolve(expr)
        if field.path not in seen:
            fields.append(field)
            seen.add(field.path)
    for expr in structs:
        for field in dwarf.expand(expr, recursive=recursive):
            if field.path not in seen:
                fields.append(field)
                seen.add(field.path)
    if not fields:
        raise ConfigError("nothing to sample (use --expr or --struct)")
    return fields


def _hss_blocks(fields: list[ResolvedField], max_span: int, gap: int) -> list[dict[str, Any]]:
    """Coalesce nearby fields into as few background reads as possible."""
    blocks: list[dict[str, Any]] = []
    for field in sorted(fields, key=lambda f: (f.read_address, f.read_size)):
        start = field.read_address
        end = start + field.read_size
        if blocks:
            block = blocks[-1]
            block_end = block["start"] + block["size"]
            if end <= block["start"] + max_span and start - block_end <= gap:
                block["size"] = max(block["size"], end - block["start"])
                block["fields"].append((field, start - block["start"]))
                continue
        blocks.append({"start": start, "size": end - start, "fields": [(field, 0)]})
    return blocks


def _hss_format(value: float | int) -> str:
    return f"{value:.9g}" if isinstance(value, float) else str(value)


def _hss_sleep_until(deadline: float) -> None:
    """Sleep to an absolute perf_counter() deadline (Windows-friendly)."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.0008:
            time.sleep(remaining - 0.0005)
        else:
            while time.perf_counter() < deadline:
                pass
            return


class _HssMemBlockDesc(ctypes.Structure):
    _fields_ = [
        ("Addr", ctypes.c_uint32),
        ("NumBytes", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("Dummy", ctypes.c_uint32),
    ]


class _HssCaps(ctypes.Structure):
    _fields_ = [
        ("MaxBlocks", ctypes.c_uint32),
        ("MaxFreq", ctypes.c_uint32),
        ("Caps", ctypes.c_uint32),
        ("Dummy", ctypes.c_uint32 * 5),
    ]


def _hss_native(probe, blocks, fields, args, out, start) -> tuple[int, float]:
    """Sample through the J-Link probe-side HSS (batch reads, no per-sample USB)."""
    link = getattr(probe, "_jlink", None)
    dll = getattr(link, "_dll", None) if link is not None else None
    if dll is None:
        raise ConfigError("native HSS: J-Link DLL handle not available")
    try:
        getcaps = dll.JLINK_HSS_GetCaps
        start_fn = dll.JLINK_HSS_Start
        read_fn = dll.JLINK_HSS_Read
        stop_fn = dll.JLINK_HSS_Stop
    except AttributeError as exc:
        raise ConfigError("native HSS: DLL has no JLINK_HSS_* exports") from exc

    getcaps.argtypes = [ctypes.POINTER(_HssCaps)]
    getcaps.restype = ctypes.c_int
    start_fn.argtypes = [
        ctypes.POINTER(_HssMemBlockDesc),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    start_fn.restype = ctypes.c_int
    read_fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    read_fn.restype = ctypes.c_int
    stop_fn.restype = ctypes.c_int

    caps = _HssCaps()
    if getcaps(ctypes.byref(caps)) < 0:
        raise ConfigError("native HSS: JLINK_HSS_GetCaps failed")
    rate = float(args.rate)
    if caps.MaxFreq and rate > caps.MaxFreq:
        print(f"hss: native rate clamped to {caps.MaxFreq} Hz", file=sys.stderr)
        rate = float(caps.MaxFreq)
    if len(blocks) > max(1, caps.MaxBlocks):
        raise ConfigError(
            f"native HSS: {len(blocks)} block(s) > MaxBlocks={caps.MaxBlocks}"
        )
    period_us = max(1, int(round(1e6 / rate)))
    print(
        f"hss: probe-side HSS {rate:.0f} Hz, {len(blocks)} block(s)",
        file=sys.stderr,
    )

    desc = (_HssMemBlockDesc * len(blocks))()
    for i, block in enumerate(blocks):
        desc[i].Addr = int(block["start"])
        desc[i].NumBytes = int(block["size"])
    rc = start_fn(desc, len(blocks), period_us, 0)
    if rc < 0:
        raise ConfigError(f"native HSS: JLINK_HSS_Start failed ({rc})")

    sample_size = 4 + sum(int(b["size"]) for b in blocks)
    buf_size = sample_size * 256
    buf = ctypes.create_string_buffer(buf_size)
    samples = 0
    last_t = 0.0
    try:
        while True:
            if args.duration is not None and time.perf_counter() - start >= args.duration:
                break
            if args.count is not None and samples >= args.count:
                break
            n = read_fn(ctypes.cast(buf, ctypes.c_void_p), buf_size)
            if n > 0:
                data = buf.raw[:n]
                for off in range(0, n - sample_size + 1, sample_size):
                    frame = data[off:off + sample_size]
                    ts = int.from_bytes(frame[0:4], "little")
                    t = ts / 1000.0
                    last_t = t
                    values: dict[str, float | int] = {}
                    pos = 4
                    for block in blocks:
                        bsize = int(block["size"])
                        bdata = frame[pos:pos + bsize]
                        for field, foff in block["fields"]:
                            values[field.path] = field.decode(bdata, foff)
                        pos += bsize
                    if args.format == "jsonl":
                        payload = {"i": samples, "t": round(t, 6)}
                        payload.update(values)
                        print(json.dumps(payload, ensure_ascii=False), file=out)
                    else:
                        cells = [f"{t:.6f}"] + [
                            _hss_format(values[f.path]) for f in fields
                        ]
                        print(",".join(cells), file=out)
                    samples += 1
                    if args.count is not None and samples >= args.count:
                        break
            if samples % 64 == 0:
                out.flush()
            time.sleep(0.01)
    finally:
        try:
            stop_fn()
        except Exception:
            pass
    rate = 0.0
    if samples > 1 and last_t > 0:
        rate = (samples - 1) / last_t   # HSS timestamps, not host wall time
    return samples, rate


def _cmd_hss(args: argparse.Namespace) -> int:
    config = _make_config(args)
    if config.elf is None:
        discovered = find_elf(None, Path.cwd())
        if discovered is not None:
            config.elf = str(discovered)
    if not config.elf:
        raise ConfigError("--elf is required (DWARF symbol resolution)")

    dwarf = DwarfSymbols(config.elf)
    exprs = list(getattr(args, "expr", None) or [])
    structs = list(getattr(args, "struct", None) or [])
    if args.list_fields:
        if not exprs and not structs:
            raise ConfigError("--list-fields needs --expr and/or --struct")
        for expr in exprs + structs:
            print(dwarf.describe(expr))
        return 0
    if args.rate <= 0:
        raise ConfigError("--rate must be positive")
    fields = _hss_fields(dwarf, exprs, structs, args.recursive)
    blocks = _hss_blocks(fields, max(1, args.max_span), max(0, args.coalesce_gap))

    if not config.device:
        raise ConfigError("no target device configured (pass --device)")
    probe = JLinkProbe(
        config.device,
        interface=config.interface,
        speed_khz=config.speed_khz,
        serial=config.probe_serial,
    )
    probe.open()

    out = None
    samples = 0
    overruns = 0
    hss_rate = None
    start = time.perf_counter()
    try:
        if not args.no_run:
            probe.go()
        out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
        if args.format == "csv":
            print(",".join(["t_s"] + [f.path for f in fields]), file=out)
        else:
            pass

        if not args.no_native:
            samples, hss_rate = _hss_native(probe, blocks, fields, args, out, start)
        else:
            period = 1.0 / args.rate
            next_deadline = start
            while True:
                if args.duration is not None and time.perf_counter() - start >= args.duration:
                    break
                if args.count is not None and samples >= args.count:
                    break
                now = time.perf_counter()
                if now < next_deadline:
                    _hss_sleep_until(next_deadline)
                    continue

                values: dict[str, float | int] = {}
                for block in blocks:
                    data = probe.read_memory(block["start"], block["size"])
                    for field, offset in block["fields"]:
                        values[field.path] = field.decode(data, offset)

                stamp = time.perf_counter() - start
                if args.format == "jsonl":
                    payload = {"i": samples, "t": round(stamp, 6)}
                    payload.update(values)
                    print(json.dumps(payload, ensure_ascii=False), file=out)
                else:
                    cells = [f"{stamp:.6f}"] + [_hss_format(values[f.path]) for f in fields]
                    print(",".join(cells), file=out)
                samples += 1
                if samples % 64 == 0:
                    out.flush()

                next_deadline += period
                if next_deadline < time.perf_counter():
                    overruns += 1
                    next_deadline = time.perf_counter()
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.perf_counter() - start
        if out is not None:
            try:
                out.flush()
            except Exception:
                pass
            if out is not sys.stdout:
                out.close()
        if args.leave_halted:
            try:
                probe.halt()
            except Exception:
                pass
        elif not args.no_run:
            try:
                probe.go()
            except Exception:
                pass
        probe.close()
        if samples:
            rate = hss_rate if hss_rate is not None else (samples / elapsed if elapsed else 0.0)
            print(
                f"hss: {samples} sample(s), {rate:.1f} Hz, "
                f"{overruns} overrun(s), {len(fields)} field(s) in {len(blocks)} block(s)",
                file=sys.stderr,
            )
    return 0


def _print_result(result, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return
    if result.ok:
        if result.text:
            print(result.text)
    else:
        print(f"error: {result.error}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
