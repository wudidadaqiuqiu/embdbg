"""focdbg -- scriptable debug backend for J-Link targets.

The public surface for embedding (GUI, pytest, CI) is :class:`DebugSession`; the
CLI is a thin wrapper around it.  See ``README.md`` for the architecture.
"""

from __future__ import annotations

from .config import Config
from .dwarf_symbols import DwarfSymbols, ResolvedField
from .jlink_probe import JLinkProbe
from .errors import (
    ConfigError,
    FocdbgError,
    GdbError,
    NotConnectedError,
    ServerError,
    TargetError,
    ToolNotFoundError,
)
from .models import Breakpoint, CommandResult, Frame, StopEvent, TargetInfo
from .plugins.base import Plugin, PluginManager
from .rsp import RspServer
from .sampler import Sampler, parse_fields
from .session import DebugSession

__version__ = "0.1.0"

__all__ = [
    "Config",
    "DebugSession",
    "DwarfSymbols",
    "ResolvedField",
    "Breakpoint",
    "CommandResult",
    "Frame",
    "StopEvent",
    "TargetInfo",
    "Sampler",
    "JLinkProbe",
    "Plugin",
    "PluginManager",
    "RspServer",
    "parse_fields",
    "FocdbgError",
    "ConfigError",
    "GdbError",
    "NotConnectedError",
    "ServerError",
    "TargetError",
    "ToolNotFoundError",
    "__version__",
]
