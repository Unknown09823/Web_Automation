"""Watchdog: monitors components and triggers automatic recovery."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CheckFunc = Callable[[], Awaitable[bool]]
RecoverFunc = Callable[[], Awaitable[None]]


class Watchdog:
    """Periodic health checks with bounded recovery attempts."""

    def __init__(self, interval: float = 15.0, max_recoveries: int = 5) -> None:
        self._interval = interval
        self._max_recoveries = max_recoveries
        self._checks: dict[str, tuple[CheckFunc, RecoverFunc | None]] = {}
        self._failures: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_check: float = 0.0

    def register(self, name: str, check: CheckFunc, recover: RecoverFunc | None = None) -> None:
        self._checks[name] = (check, recover)
        self._failures[name] = 0
        log.info("Watchdog registered component=%s", name)

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)
        self._failures.pop(name, None)

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="watchdog-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._last_check = time.time()
            for name, (check, recover) in list(self._checks.items()):
                try:
                    healthy = await check()
                except Exception:  # noqa: BLE001
                    log.exception("Watchdog check error for %s", name)
                    healthy = False
                if healthy:
                    self._failures[name] = 0
                    continue
                self._failures[name] += 1
                log.warning(
                    "Watchdog: %s unhealthy (%d/%d)",
                    name,
                    self._failures[name],
                    self._max_recoveries,
                )
                if recover and self._failures[name] <= self._max_recoveries:
                    try:
                        await recover()
                    except Exception:  # noqa: BLE001
                        log.exception("Watchdog recovery failed for %s", name)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
