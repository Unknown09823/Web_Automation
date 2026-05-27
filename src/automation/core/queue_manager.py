"""Multi-priority async queue manager with named queues."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class QueueManager:
    """Named async queues with priority support."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.PriorityQueue[tuple[int, int, Any]]] = {}
        self._counters: dict[str, int] = {}

    def get_or_create(self, name: str) -> asyncio.PriorityQueue:
        if name not in self._queues:
            self._queues[name] = asyncio.PriorityQueue()
            self._counters[name] = 0
            log.debug("Created queue=%s", name)
        return self._queues[name]

    async def put(self, name: str, item: Any, priority: int = 5) -> None:
        q = self.get_or_create(name)
        self._counters[name] = self._counters.get(name, 0) + 1
        await q.put((priority, self._counters[name], item))

    async def get(self, name: str) -> Any:
        q = self.get_or_create(name)
        _, _, item = await q.get()
        return item

    def size(self, name: str) -> int:
        if name not in self._queues:
            return 0
        return self._queues[name].qsize()

    def stats(self) -> dict[str, int]:
        return {name: q.qsize() for name, q in self._queues.items()}
