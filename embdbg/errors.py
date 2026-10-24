"""Exception hierarchy for embdbg.

Everything user-facing derives from :class:`FocdbgError`, which carries an
``exit_code`` so the CLI can map failures to shell exit codes and the GUI can
distinguish recoverable from fatal conditions.
"""

from __future__ import annotations


class FocdbgError(Exception):
    """Base class for all embdbg errors."""

    exit_code = 1

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.detail:
            return f"{self.message}\n{self.detail}"
        return self.message


class ConfigError(FocdbgError):
    """Bad or missing configuration."""

    exit_code = 2


class ToolNotFoundError(ConfigError):
    """A required external executable (gdb / J-Link) could not be located."""

    def __init__(self, tool: str, hint: str = ""):
        msg = f"cannot find {tool}"
        if hint:
            msg += f" ({hint})"
        super().__init__(msg)
        self.tool = tool


class ServerError(FocdbgError):
    """The J-Link GDB server failed to start or died."""


class GdbError(FocdbgError):
    """GDB returned an error record, or the MI channel broke."""

    def __init__(self, message: str, *, command: str | None = None, detail: str | None = None):
        super().__init__(message, detail=detail)
        self.command = command


class GdbTimeoutError(GdbError):
    """GDB did not answer within the configured timeout."""


class GdbProtocolError(GdbError):
    """Unparsable GDB/MI output (should not happen)."""


class TargetError(FocdbgError):
    """The target reported an error (memory fault, no symbol, ...)."""


class NotConnectedError(TargetError):
    def __init__(self) -> None:
        super().__init__("not connected to a target")
