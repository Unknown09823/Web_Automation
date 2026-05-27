"""Metrics plugin: counts framework events for the dashboard."""
from __future__ import annotations

from typing import Any

from automation.core.event_bus import Event
from automation.plugins.base import Plugin


class MetricsPlugin(Plugin):
    name = "metrics"
    version = "1.0.0"
    description = "Counts framework events for visibility."

    def __init__(self, engine: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(engine, config)
        self.counters: dict[str, int] = {}

    async def on_start(self) -> None:
        await self.engine.event_bus.subscribe("*", self._count)

    async def _count(self, event: Event) -> None:
        self.counters[event.name] = self.counters.get(event.name, 0) + 1

    async def on_reload(self) -> None:
        # keep counters across reload
        pass

    async def healthcheck(self) -> bool:
        return True
