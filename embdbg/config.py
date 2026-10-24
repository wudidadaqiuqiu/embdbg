"""Configuration loading: defaults < embdbg.toml < environment < CLI."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import ConfigError

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

CONFIG_NAME = "embdbg.toml"
VALID_INTERFACES = ("SWD", "JTAG")


@dataclass
class Config:
    """Everything the backend needs to reach a target.

    Values may come from (in increasing priority) dataclass defaults, a TOML
    file, ``FOCDBG_*`` environment variables and explicit CLI overrides.
    """

    # --- target / probe -------------------------------------------------
    device: str | None = None
    interface: str = "SWD"
    speed_khz: int = 4000
    probe_serial: str | None = None
    elf: str | None = None

    # --- tools ----------------------------------------------------------
    gdb: str | None = None
    jlink_server: str | None = None
    jlink_exe: str | None = None

    # --- server behaviour ----------------------------------------------
    base_port: int = 2331
    singlerun: bool = True
    extra_server_args: list[str] = field(default_factory=list)
    log_dir: str | None = None
    keep_server_log: bool = False

    # --- session behaviour ---------------------------------------------
    hw_breakpoints: bool = True
    verify: bool = True
    run_after_flash: bool = True
    connect_timeout: float = 30.0
    command_timeout: float = 20.0
    flash_timeout: float = 180.0

    # --- plugins --------------------------------------------------------
    plugins: dict[str, dict] = field(default_factory=dict)

    # --- bookkeeping ----------------------------------------------------
    config_path: str | None = None

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike | None = None, overrides: dict[str, Any] | None = None) -> "Config":
        cfg = cls()
        file_path, data, plugins = _read_config_file(path)
        cfg._apply(data)
        cfg.plugins = dict(plugins)
        if file_path is not None:
            cfg.config_path = str(file_path)
            cfg._resolve_relative_paths(file_path.parent)
        cfg._apply_env()
        if overrides:
            cfg._apply({k: v for k, v in overrides.items() if v is not None})
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------
    def _apply(self, data: dict[str, Any]) -> None:
        valid = {f.name for f in fields(self)}
        for key, value in data.items():
            key = key.replace("-", "_")
            if key not in valid:
                raise ConfigError(f"unknown config key: {key!r}")
            if value is None:
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                value = _as_bool(value, key)
            elif isinstance(current, int):
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise ConfigError(f"config key {key!r} must be an integer") from exc
            elif isinstance(current, float):
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise ConfigError(f"config key {key!r} must be a number") from exc
            elif isinstance(current, list) and not isinstance(value, list):
                raise ConfigError(f"config key {key!r} must be a list")
            setattr(self, key, value)

    def _resolve_relative_paths(self, base: Path) -> None:
        for key in ("elf", "gdb", "jlink_server", "jlink_exe", "log_dir"):
            value = getattr(self, key)
            if not value:
                continue
            p = Path(os.path.expandvars(str(value))).expanduser()
            if not p.is_absolute():
                p = base / p
            setattr(self, key, str(p))

    def _apply_env(self) -> None:
        mapping = {
            "FOCDBG_DEVICE": "device",
            "FOCDBG_INTERFACE": "interface",
            "FOCDBG_SPEED": "speed_khz",
            "FOCDBG_ELF": "elf",
            "FOCDBG_GDB": "gdb",
            "FOCDBG_JLINK_SERVER": "jlink_server",
            "FOCDBG_JLINK_EXE": "jlink_exe",
            "FOCDBG_PORT": "base_port",
            "FOCDBG_PROBE_SERIAL": "probe_serial",
            "FOCDBG_LOG_DIR": "log_dir",
        }
        for env_key, attr in mapping.items():
            raw = os.environ.get(env_key)
            if raw:
                self._apply({attr: raw})

    def validate(self) -> None:
        if self.interface.upper() not in VALID_INTERFACES:
            raise ConfigError(f"interface must be one of {VALID_INTERFACES}, got {self.interface!r}")
        self.interface = self.interface.upper()
        if not 1 <= self.speed_khz <= 100_000:
            raise ConfigError(f"speed_khz out of range: {self.speed_khz}")
        if not 1 <= self.base_port <= 65535:
            raise ConfigError(f"base_port out of range: {self.base_port}")
        if self.elf and not Path(self.elf).expanduser().exists():
            raise ConfigError(f"ELF file not found: {self.elf}")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"config key {key!r} must be a boolean")


def _candidate_paths(explicit: str | os.PathLike | None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(os.path.expandvars(str(explicit))).expanduser())
        return paths
    env = os.environ.get("FOCDBG_CONFIG")
    if env:
        paths.append(Path(os.path.expandvars(env)).expanduser())
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents][:8]:
        paths.append(parent / CONFIG_NAME)
    # shipped next to the checkout, so the tool works from anywhere
    paths.append(Path(__file__).resolve().parent.parent / CONFIG_NAME)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "embdbg" / CONFIG_NAME)
    else:
        paths.append(Path.home() / ".config" / "embdbg" / CONFIG_NAME)
    seen: set[Path] = set()
    unique = []
    for p in paths:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(p)
    return unique


def _read_config_file(
    path: str | os.PathLike | None,
) -> tuple[Path | None, dict[str, Any], dict[str, Any]]:
    if tomllib is None:  # pragma: no cover - Py<3.11
        return None, {}, {}
    explicit = path is not None
    for candidate in _candidate_paths(path):
        if not candidate.is_file():
            if explicit:
                raise ConfigError(f"config file not found: {candidate}")
            continue
        try:
            with open(candidate, "rb") as fh:
                raw = tomllib.load(fh)
        except OSError as exc:
            raise ConfigError(f"cannot read config {candidate}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {candidate}: {exc}") from exc
        table = raw.get("embdbg", raw)
        if not isinstance(table, dict):
            raise ConfigError(f"{candidate}: [embdbg] must be a table")
        table = dict(table)
        plugins = table.pop("plugins", {})
        top_level = raw.get("plugins", {})
        if not isinstance(plugins, dict) or not isinstance(top_level, dict):
            raise ConfigError(f"{candidate}: [plugins] must be a table")
        if top_level:
            plugins = {**plugins, **top_level}
        return candidate, table, plugins
    return None, {}, {}
