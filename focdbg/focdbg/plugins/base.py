"""Base classes for focdbg plugins."""

from __future__ import annotations

import abc
import importlib
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Callable

from ..errors import FocdbgError


class PluginError(FocdbgError):
    """Plugin could not be loaded or configured."""

    exit_code = 2


@dataclass
class PluginContext:
    """Everything a plugin may need from the running process."""

    config: Any = None
    session: Any = None
    probe: Any = None
    log: Callable[[str], None] | None = None

    def emit(self, message: str) -> None:
        if self.log is not None:
            self.log(message)


class Plugin(abc.ABC):
    """Lifecycle contract.

    ``start()`` is called once, after the debug session / probe is ready (for
    ``serve``) or immediately (for standalone CLI use).  ``stop()`` must be
    idempotent and must not raise.
    """

    name = ""
    description = ""

    def __init__(self, options: dict[str, Any] | None = None, context: PluginContext | None = None):
        self.options = dict(options or {})
        self.context = context or PluginContext()

    def start(self) -> None:  # noqa: B027 - intentional no-op default
        """Bring the plugin up."""

    def stop(self) -> None:  # noqa: B027 - intentional no-op default
        """Bring the plugin down."""

    def commands(self) -> dict[str, Callable[[list[str]], Any]]:
        """Optional console commands contributed by this plugin."""
        return {}

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


#: Plugins shipped with focdbg (``name -> "module:Class"``).
BUILTIN_PLUGINS: dict[str, str] = {
    "serial": "focdbg.plugins.serial:SerialPlugin",
}

ENTRY_POINT_GROUP = "focdbg.plugins"


class PluginManager:
    def __init__(self, context: PluginContext | None = None):
        self.context = context or PluginContext()
        self.plugins: dict[str, Plugin] = {}
        self._started = False

    # ------------------------------------------------------------------
    @staticmethod
    def available() -> dict[str, str]:
        """Map plugin name -> import spec (built-ins + entry points)."""
        found = dict(BUILTIN_PLUGINS)
        try:
            discovered = entry_points()
            if hasattr(discovered, "select"):
                selected = discovered.select(group=ENTRY_POINT_GROUP)
            else:  # pragma: no cover - Python < 3.10
                selected = discovered.get(ENTRY_POINT_GROUP, [])
            for entry in selected:
                found.setdefault(entry.name, entry.value)
        except Exception:
            pass
        return found

    def load(self, spec: str, options: dict[str, Any] | None = None) -> Plugin:
        """Load a plugin by built-in name, entry-point name or ``module:Class``."""
        if spec in self.plugins:
            return self.plugins[spec]

        available = self.available()
        import_spec = available.get(spec, spec if ":" in spec else None)
        if import_spec is None:
            raise PluginError(
                f"unknown plugin {spec!r}; known: {', '.join(sorted(available))}"
            )
        module_name, _, class_name = import_spec.partition(":")
        if not class_name:
            raise PluginError(f"plugin spec must be 'module:Class', got {import_spec!r}")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise PluginError(f"cannot import plugin module {module_name!r}: {exc}") from exc
        try:
            plugin_class = getattr(module, class_name)
        except AttributeError as exc:
            raise PluginError(f"{module_name!r} has no attribute {class_name!r}") from exc
        try:
            plugin = plugin_class(options or {}, self.context)
        except TypeError as exc:
            raise PluginError(
                f"plugin {spec!r} must accept (options, context): {exc}"
            ) from exc
        if not isinstance(plugin, Plugin):
            raise PluginError(f"{import_spec!r} is not a focdbg.plugins.Plugin")
        if not plugin.name:
            plugin.name = spec.split(":")[-1]
        self.plugins[plugin.name] = plugin
        self.context.emit(f"plugin loaded: {plugin.name}")
        return plugin

    def load_from_config(self, config: Any) -> list[Plugin]:
        """Load every ``[plugins.<name>]`` table that is not disabled."""
        loaded: list[Plugin] = []
        for name, options in (getattr(config, "plugins", None) or {}).items():
            if not isinstance(options, dict):
                raise PluginError(f"[plugins.{name}] must be a table")
            if not options.get("enabled", True):
                continue
            loaded.append(self.load(name, {k: v for k, v in options.items() if k != "enabled"}))
        return loaded

    # ------------------------------------------------------------------
    def get(self, name: str) -> Plugin | None:
        return self.plugins.get(name)

    def start_all(self) -> None:
        for name, plugin in self.plugins.items():
            try:
                plugin.start()
                self.context.emit(f"plugin started: {name}")
            except Exception as exc:
                plugin.stop()
                raise PluginError(f"plugin {name!r} failed to start: {exc}") from exc
        self._started = True

    def stop_all(self) -> None:
        for name, plugin in reversed(list(self.plugins.items())):
            try:
                plugin.stop()
            except Exception as exc:  # never mask shutdown
                self.context.emit(f"plugin {name!r} stop failed: {exc}")
        self._started = False

    def command_map(self) -> dict[str, Callable[[list[str]], Any]]:
        commands: dict[str, Callable[[list[str]], Any]] = {}
        for plugin in self.plugins.values():
            for name, handler in plugin.commands().items():
                if name in commands:
                    raise PluginError(f"duplicate plugin command {name!r}")
                commands[name] = handler
        return commands
