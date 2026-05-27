"""Heartbeat plugin: emits an event on a fixed interval.

Demonstrates use of the scheduler to run a periodic job and the event bus
to publish custom events that other plugins can subscribe to.
"""
from __future__ import annotations

import time
from typing import Any

from automation.core.event_bus import Event
from automation.plugins.base import Plugin


class HeartbeatPlugin(Plugin):
    name = "heartbeat"
    version = "1.0.0"
    description = "Emits a periodic 'heartbeat' event."

    def __init__(self, engine: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(engine, config)
        self.interval = float(self.config.get("interval", 30))
        self.beats = 0

    async def on_start(self) -> None:
        self.engine.scheduler.add_job(
            name="heartbeat", func=self._beat, interval=self.interval,
        )
        self.log.info("heartbeat scheduled every %.1fs", self.interval)

    async def on_stop(self) -> None:
        self.engine.scheduler.remove_job("heartbeat")

    async def on_reload(self) -> None:
        new_interval = float(self.config.get("interval", self.interval))
        if new_interval != self.interval:
            self.engine.scheduler.remove_job("heartbeat")
            self.interval = new_interval
            self.engine.scheduler.add_job(
                name="heartbeat", func=self._beat, interval=self.interval,
            )

    async def _beat(self) -> None:
        self.beats += 1
        await self.engine.event_bus.publish(
            Event("heartbeat.tick", {"count": self.beats, "ts": time.time()})
        )
        self.log.debug("heartbeat tick #%d", self.beats)
