"""Plugin framework.

A plugin is a named lifecycle object that can extend embdbg without touching the
core.  Plugins are discovered from:

  * the built-in registry (``serial`` today),
  * Python entry points in the ``embdbg.plugins`` group, and
  * explicit ``module:Class`` specs (``--plugin mypkg.mymod:MyPlugin``).

Configuration lives in ``embdbg.toml``::

    [plugins.serial]
    port = "COM3"
    baudrate = 115200
    mode = "monitor"
"""

from __future__ import annotations

from .base import Plugin, PluginContext, PluginError, PluginManager

__all__ = ["Plugin", "PluginContext", "PluginError", "PluginManager"]
