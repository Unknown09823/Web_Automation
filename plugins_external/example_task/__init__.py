"""Task example: registers a sample task type that responds to events."""
from __future__ import annotations

from typing import Any

from automation.core.event_bus import Event
from automation.core.task_manager import Task
from automation.plugins.base import Plugin


class ExampleTaskPlugin(Plugin):
    name = "example_task"
    version = "1.0.0"
    description = "Submits a sample task on every heartbeat tick."
    default_enabled = False

    async def on_start(self) -> None:
        await self.engine.event_bus.subscribe("heartbeat.tick", self._on_tick)

    async def _on_tick(self, event: Event) -> None:
        await self.engine.task_manager.submit(
            Task(
                name="example.echo",
                func=self._echo,
                args=(event.payload,),
                max_retries=1,
            )
        )

    async def _echo(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.log.info("example_task echo: %s", payload)
        return payload
