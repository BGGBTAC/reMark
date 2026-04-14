"""Plugin discovery and registry.

Scans two sources for plugin classes:
  1. Local directory (config.plugins.plugin_dir) — any .py file's module-level
     subclasses of Plugin are loaded.
  2. Entry points under the 'remark_bridge.plugins' group — for pip-installed
     packages.

Loaded plugins are grouped by hook type for fast iteration at runtime.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
from pathlib import Path

from src.config import PluginConfig
from src.plugins.hooks import (
    ActionExtractorHook,
    NoteProcessorHook,
    OCRBackendHook,
    Plugin,
    SyncHook,
)

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Discovers and holds references to all loaded plugins."""

    def __init__(self, config: PluginConfig):
        self._config = config
        self._plugins: dict[str, Plugin] = {}
        self._by_hook: dict[type, list[Plugin]] = {}
        self._disabled: set[str] = set(config.disabled)

    def discover(self) -> None:
        """Find and instantiate all available plugins."""
        if not self._config.enabled:
            logger.debug("Plugin system disabled")
            return

        self._discover_directory()
        self._discover_entry_points()

        logger.info(
            "Loaded %d plugins: %s",
            len(self._plugins),
            ", ".join(self._plugins.keys()) or "(none)",
        )

    def _discover_directory(self) -> None:
        """Load plugins from the user-defined plugin directory."""
        plugin_dir = Path(self._config.plugin_dir).expanduser()
        if not plugin_dir.exists():
            logger.debug("Plugin directory not found: %s", plugin_dir)
            return

        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))

        for py_file in plugin_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            module_name = py_file.stem
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self._register_module(module)
            except Exception as e:
                logger.warning("Failed to load plugin %s: %s", py_file.name, e)

    def _discover_entry_points(self) -> None:
        """Load plugins registered via Python entry points."""
        try:
            from importlib.metadata import entry_points
            eps = entry_points(group="remark_bridge.plugins")
        except Exception as e:
            logger.debug("No entry points discovered: %s", e)
            return

        for ep in eps:
            try:
                module_or_class = ep.load()
                if inspect.isclass(module_or_class) and issubclass(module_or_class, Plugin):
                    self._instantiate(module_or_class)
                else:
                    self._register_module(module_or_class)
            except Exception as e:
                logger.warning("Failed to load entry-point plugin %s: %s", ep.name, e)

    def _register_module(self, module) -> None:
        """Instantiate every Plugin subclass defined in the given module."""
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Plugin or obj in _HOOK_BASES:
                continue
            if issubclass(obj, Plugin) and obj.__module__ == module.__name__:
                self._instantiate(obj)

    def _instantiate(self, cls: type[Plugin]) -> None:
        try:
            instance = cls()
            name = instance.metadata.name
        except Exception as e:
            logger.warning("Failed to instantiate plugin %s: %s", cls.__name__, e)
            return

        if name in self._disabled:
            logger.info("Plugin '%s' disabled by config", name)
            return

        if name in self._plugins:
            logger.warning("Plugin '%s' already loaded, skipping duplicate", name)
            return

        # Pass plugin-specific settings if available
        settings = self._config.settings.get(name, {})
        if settings:
            try:
                instance.configure(settings)
            except Exception as e:
                logger.warning("Plugin '%s' configure() failed: %s", name, e)

        self._plugins[name] = instance
        for hook_type in _HOOK_BASES:
            if isinstance(instance, hook_type):
                self._by_hook.setdefault(hook_type, []).append(instance)

        logger.debug("Registered plugin: %s (%s)", name, cls.__name__)

    # -- Public API --

    def list_plugins(self) -> list[dict]:
        """Return metadata for every loaded plugin."""
        result = []
        for name, plugin in self._plugins.items():
            meta = plugin.metadata
            hooks = [h.__name__ for h in _HOOK_BASES if isinstance(plugin, h)]
            result.append({
                "name": name,
                "version": meta.version,
                "description": meta.description,
                "author": meta.author,
                "hooks": hooks,
            })
        return result

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def hooks(self, hook_type: type) -> list[Plugin]:
        """Return all loaded plugins implementing the given hook type."""
        return list(self._by_hook.get(hook_type, []))

    def is_enabled(self, name: str) -> bool:
        return name in self._plugins and name not in self._disabled


_HOOK_BASES: tuple[type, ...] = (
    ActionExtractorHook,
    OCRBackendHook,
    NoteProcessorHook,
    SyncHook,
)
