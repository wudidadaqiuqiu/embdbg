"""CLI surface for plugins (kept separate to avoid a giant cli.py)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .errors import ConfigError
from .plugins.base import PluginManager
from .plugins.serial import SerialPlugin, encode_payload, list_ports


def add_serial_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", default=None, help="device or pyserial URL (COM3, loop://, socket://host:port)")
    parser.add_argument("--baud", type=int, default=None, help="baudrate (default 115200)")
    parser.add_argument("--bytesize", type=int, choices=(5, 6, 7, 8), default=None)
    parser.add_argument("--parity", choices=("N", "E", "O", "M", "S"), default=None)
    parser.add_argument("--stopbits", choices=("1", "1.5", "2"), default=None)
    parser.add_argument("--flow", choices=("none", "rtscts", "xonxoff", "dsrdtr"), default=None)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--dtr", choices=("on", "off"), default=None)
    parser.add_argument("--rts", choices=("on", "off"), default=None)
    parser.add_argument("--config", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def _serial_options(args: argparse.Namespace, config: Config) -> dict:
    options = dict((config.plugins or {}).get("serial", {}))
    options.pop("enabled", None)
    mapping = (
        ("port", "port"),
        ("baud", "baudrate"),
        ("bytesize", "bytesize"),
        ("parity", "parity"),
        ("stopbits", "stopbits"),
        ("flow", "flow"),
        ("timeout", "timeout"),
        ("encoding", "encoding"),
        ("dtr", "dtr"),
        ("rts", "rts"),
    )
    for cli_key, option_key in mapping:
        value = getattr(args, cli_key, None)
        if value is not None:
            options[option_key] = value
    return options


def _set_if_not_none(options: dict, key: str, value) -> None:
    if value is not None:
        options[key] = value


def cmd_plugins(args: argparse.Namespace, config: Config) -> int:
    available = PluginManager.available()
    configured = config.plugins or {}
    entries = []
    for name in sorted(set(available) | set(configured)):
        options = configured.get(name) or {}
        entries.append(
            {
                "name": name,
                "spec": available.get(name),
                "builtin": name in available,
                "configured": name in configured,
                "enabled": bool(options.get("enabled", True)) if options else False,
            }
        )
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "plugins": entries}, ensure_ascii=False))
    else:
        for entry in entries:
            state = "configured" if entry["configured"] else "available"
            if entry["configured"] and not entry["enabled"]:
                state = "disabled"
            print(f"{entry['name']:<12} {state:<10} {entry['spec'] or '?'}")
    return 0


def cmd_serial(args: argparse.Namespace, config: Config) -> int:
    if args.serial_cmd == "list":
        ports = list_ports()
        if getattr(args, "json", False):
            print(json.dumps({"ok": True, "ports": ports}, ensure_ascii=False))
        else:
            if not ports:
                print("no serial ports found")
            for port in ports:
                print(f"{port['device']}\t{port['description']}")
        return 0

    options = _serial_options(args, config)

    if args.serial_cmd == "send":
        if args.file:
            data = Path(args.file).read_bytes()
            if (args.newline or "none") != "none":
                data += b"\n" if args.newline == "lf" else b"\r\n"
        else:
            newline = args.newline or ("none" if args.hex else "lf")
            data = encode_payload(
                args.data,
                hex_mode=args.hex,
                encoding=options.get("encoding", "utf-8"),
                newline=newline,
            )
        plugin = SerialPlugin(options)
        plugin.open()
        try:
            written = plugin.write(data)
            result = {"ok": True, "port": options["port"], "written": written}
            if args.expect or args.expect_hex:
                encoding = options.get("encoding", "utf-8")
                pattern = (
                    bytes.fromhex(args.expect_hex.replace(" ", ""))
                    if args.expect_hex
                    else args.expect.encode(encoding)
                )
                matched, received = plugin.expect(pattern, args.expect_timeout)
                result["matched"] = matched
                result["received"] = (
                    received.hex(" ")
                    if (args.hex or args.expect_hex)
                    else received.decode(encoding, errors="replace")
                )
                if not matched:
                    result["ok"] = False
                    result["error"] = f"timeout waiting for {args.expect or args.expect_hex!r}"
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"wrote {written} byte(s) to {options['port']}")
                if "received" in result:
                    print(f"received: {result['received']}")
                if not result["ok"]:
                    print(f"error: {result['error']}", file=sys.stderr)
            return 0 if result["ok"] else 1
        finally:
            plugin.close()

    if args.serial_cmd == "monitor":
        _set_if_not_none(options, "log", args.log)
        _set_if_not_none(options, "hex", args.hex)
        _set_if_not_none(options, "timestamp", args.timestamp)
        plugin = SerialPlugin(options)
        plugin.open()
        try:
            plugin.monitor(duration=args.duration, out=sys.stdout)
        except KeyboardInterrupt:
            pass
        finally:
            plugin.close()
        return 0

    raise ConfigError(f"unknown serial command {args.serial_cmd!r}")
