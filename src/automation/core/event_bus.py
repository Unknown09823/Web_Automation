"""Asynchronous publish/subscribe event bus.

Plugins and core subsystems communicate through events. Subscribers run in
isolation: an exception in one subscriber never affects others or the bus.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, DefaultDict

log = logging.getLogger(__name__)

EventHandler = Callable[["Event"], Awaitable[None] | None]


@dataclass(slots=True)
class Event:
    """A typed event flowing through the bus."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str | None = None


class EventBus:
    """Async pub/sub bus with wildcard ('*') subscriptions and isolation."""

    def __init__(self) -> None:
        self._subs: DefaultDict[str, list[EventHandler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, event_name: str, handler: EventHandler) -> None:
        async with self._lock:
            self._subs[event_name].append(handler)
        log.debug("Subscribed handler=%s to event=%s", handler, event_name)

    async def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        async with self._lock:
            if handler in self._subs.get(event_name, []):
                self._subs[event_name].remove(handler)

    async def publish(self, event: Event) -> None:
        """Publish an event to subscribers and to wildcard listeners."""
        handlers = list(self._subs.get(event.name, [])) + list(self._subs.get("*", []))
        if not handlers:
            return
        await asyncio.gather(
            *(self._invoke(h, event) for h in handlers), return_exceptions=False
        )

    @staticmethod
    async def _invoke(handler: EventHandler, event: Event) -> None:
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - exception isolation by design
            log.exception("Event handler failed for event=%s", event.name)
