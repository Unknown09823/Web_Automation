"""Plugin base class with lifecycle hooks.

Plugins inherit from `Plugin` and override only the hooks they care about.
All hooks are async and exception-isolated by the loader, so a plugin
failure cannot crash the engine.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from automation.core.engine import Engine
    from automation.core.event_bus import Event


class Plugin:
    """Base class for all plugins.

    Subclasses MUST set ``name`` and SHOULD set ``version``.
    Override any of the lifecycle hooks below.
    """

    #: Unique plugin identifier (used in config to enable/disable).
    name: ClassVar[str] = "unnamed-plugin"
    #: Semver-style version string.
    version: ClassVar[str] = "0.0.0"
    #: Human-readable description.
    description: ClassVar[str] = ""
    #: When True, plugin is enabled unless explicitly disabled in config.
    default_enabled: ClassVar[bool] = True

    def __init__(self, engine: "Engine", config: dict[str, Any] | None = None) -> None:
        self.engine = engine
        self.config: dict[str, Any] = config or {}
        self.log = logging.getLogger(f"plugin.{self.name}")
        self.enabled: bool = True

    # ---- Lifecycle hooks ---------------------------------------------------

    async def on_start(self) -> None:
        """Called once when the plugin is started."""

    async def on_stop(self) -> None:
        """Called once before the plugin is stopped."""

    async def on_reload(self) -> None:
        """Called when configuration is reloaded."""

    async def on_task_begin(self, event: "Event") -> None:
        """Called when any task begins (subscribed to ``task.begin``)."""

    async def on_task_complete(self, event: "Event") -> None:
        """Called when any task completes (subscribed to ``task.complete``)."""

    async def on_error(self, event: "Event") -> None:
        """Called on any task error (subscribed to ``task.error``)."""

    # ---- Health ------------------------------------------------------------

    async def healthcheck(self) -> bool:
        """Return True if the plugin is healthy. Default: always healthy."""
        return True

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<Plugin {self.name} v{self.version}>"
