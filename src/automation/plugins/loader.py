"""Dynamic plugin loader.

Discovers ``Plugin`` subclasses across configured directories and the built-in
namespace package, instantiates them, wires them to the event bus, and runs
their lifecycle hooks under exception isolation.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from automation.core.event_bus import Event
from automation.plugins.base import Plugin
from automation.plugins.registry import PluginRecord, PluginRegistry

if TYPE_CHECKING:
    from automation.core.engine import Engine

log = logging.getLogger(__name__)


class PluginLoader:
    """Discover, instantiate, and manage the lifecycle of plugins."""

    def __init__(self, engine: "Engine") -> None:
        self.engine = engine
        self.registry = PluginRegistry()

    # ------------------------------------------------------------------ paths
    def _discovery_paths(self) -> list[Path]:
        cfg_paths: list[str] = self.engine.config.get("plugins.paths", []) or []
        defaults = ["plugins_external"]
        paths: list[Path] = []
        for p in [*cfg_paths, *defaults]:
            path = Path(p).resolve()
            if path.exists() and path.is_dir():
                paths.append(path)
        return paths

    # -------------------------------------------------------------- discovery
    async def discover(self) -> None:
        """Discover plugins from filesystem and import any ``automation.contrib`` namespace."""
        paths = self._discovery_paths()
        log.info("Plugin discovery paths: %s", [str(p) for p in paths])
        for base in paths:
            for entry in sorted(base.iterdir()):
                if entry.is_dir():
                    pkg_init = entry / "__init__.py"
                    if pkg_init.exists():
                        self._load_module_file(entry / "__init__.py", entry.name)
                    else:
                        # also scan loose .py files inside the dir
                        for py in entry.glob("*.py"):
                            self._load_module_file(py, f"{entry.name}.{py.stem}")
                elif entry.suffix == ".py" and not entry.name.startswith("_"):
                    self._load_module_file(entry, entry.stem)

    def _load_module_file(self, path: Path, mod_name: str) -> None:
        full_name = f"automation_plugin.{mod_name}"
        try:
            spec = importlib.util.spec_from_file_location(full_name, path)
            if not spec or not spec.loader:
                return
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001
            log.exception("Failed importing plugin module %s", path)
            return
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Plugin or not issubclass(obj, Plugin):
                continue
            if obj.__module__ != module.__name__:
                continue
            self._instantiate(obj, str(path))

    def _instantiate(self, cls: type[Plugin], module_path: str) -> None:
        plugin_cfg = self.engine.config.get(f"plugins.config.{cls.name}", {}) or {}
        enabled_default = bool(cls.default_enabled)
        enabled = bool(
            self.engine.config.get(f"plugins.enabled.{cls.name}", enabled_default)
        )
        try:
            instance = cls(engine=self.engine, config=plugin_cfg)
            instance.enabled = enabled
        except Exception as exc:  # noqa: BLE001
            log.exception("Plugin %s failed to instantiate", cls.name)
            self.registry.register(
                PluginRecord(
                    name=cls.name,
                    instance=Plugin(self.engine),  # placeholder
                    module_path=module_path,
                    enabled=False,
                    error=f"init error: {exc}",
                )
            )
            return
        self.registry.register(
            PluginRecord(
                name=cls.name,
                instance=instance,
                module_path=module_path,
                enabled=enabled,
                metadata={"version": cls.version, "description": cls.description},
            )
        )
        log.info("Loaded plugin=%s v%s enabled=%s", cls.name, cls.version, enabled)

    # ----------------------------------------------------------------- wiring
    async def _subscribe_hooks(self, plugin: Plugin) -> None:
        bus = self.engine.event_bus
        await bus.subscribe("task.begin", plugin.on_task_begin)
        await bus.subscribe("task.complete", plugin.on_task_complete)
        await bus.subscribe("task.error", plugin.on_error)

    # --------------------------------------------------------------- lifecycle
    async def start_all(self) -> None:
        for record in self.registry.all():
            if not record.enabled:
                continue
            await self._safe_start(record)

    async def stop_all(self) -> None:
        for record in self.registry.all():
            if record.started:
                await self._safe_stop(record)

    async def reload_all(self) -> None:
        for record in self.registry.all():
            await self._safe_reload(record)

    async def _safe_start(self, record: PluginRecord) -> None:
        try:
            await self._subscribe_hooks(record.instance)
            await record.instance.on_start()
            record.started = True
            record.error = None
            self.engine.health.set_component(f"plugin:{record.name}", "ok")
            await self.engine.event_bus.publish(
                Event("plugin.started", {"name": record.name})
            )
        except Exception as exc:  # noqa: BLE001 - isolation
            record.error = f"{exc}\n{traceback.format_exc()}"
            self.engine.health.set_component(f"plugin:{record.name}", "error")
            log.exception("Plugin %s on_start failed", record.name)

    async def _safe_stop(self, record: PluginRecord) -> None:
        try:
            await record.instance.on_stop()
        except Exception:  # noqa: BLE001
            log.exception("Plugin %s on_stop failed", record.name)
        record.started = False
        self.engine.health.remove_component(f"plugin:{record.name}")

    async def _safe_reload(self, record: PluginRecord) -> None:
        try:
            await record.instance.on_reload()
            await self.engine.event_bus.publish(
                Event("plugin.reloaded", {"name": record.name})
            )
        except Exception:  # noqa: BLE001
            log.exception("Plugin %s on_reload failed", record.name)

    # --------------------------------------------------------------- controls
    async def enable(self, name: str) -> bool:
        record = self.registry.get(name)
        if not record:
            return False
        if not record.enabled:
            record.enabled = True
            await self._safe_start(record)
        return True

    async def disable(self, name: str) -> bool:
        record = self.registry.get(name)
        if not record:
            return False
        if record.enabled:
            await self._safe_stop(record)
            record.enabled = False
        return True

    async def restart(self, name: str) -> bool:
        record = self.registry.get(name)
        if not record:
            return False
        if record.started:
            await self._safe_stop(record)
        await self._safe_start(record)
        return True

    def list_records(self) -> Iterable[PluginRecord]:
        return self.registry.all()
