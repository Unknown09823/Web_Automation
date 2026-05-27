"""Task manager: lifecycle, retries, timeouts, and metrics."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from automation.core.event_bus import Event, EventBus

log = logging.getLogger(__name__)

TaskFunc = Callable[..., Awaitable[Any]]


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


@dataclass(slots=True)
class Task:
    name: str
    func: TaskFunc
    args: tuple[Any, ...] = field(default_factory=tuple)
    kwargs: dict[str, Any] = field(default_factory=dict)
    timeout: float | None = None
    max_retries: int = 0
    retry_delay: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    error: str | None = None
    result: Any = None
    started_at: float | None = None
    completed_at: float | None = None


class TaskManager:
    """Executes tasks with retries, timeouts and event emission."""

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._tasks: dict[str, Task] = {}
        self._running: dict[str, asyncio.Task[Any]] = {}

    @property
    def tasks(self) -> dict[str, Task]:
        return self._tasks

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list(self, status: TaskStatus | None = None) -> list[Task]:
        if status is None:
            return list(self._tasks.values())
        return [t for t in self._tasks.values() if t.status == status]

    async def submit(self, task: Task) -> Task:
        self._tasks[task.id] = task
        await self._bus.publish(Event("task.submitted", {"task_id": task.id, "name": task.name}))
        coro = self._run(task)
        self._running[task.id] = asyncio.create_task(coro, name=f"task:{task.name}")
        return task

    async def cancel(self, task_id: str) -> bool:
        running = self._running.get(task_id)
        if running and not running.done():
            running.cancel()
            return True
        return False

    async def _run(self, task: Task) -> None:
        await self._bus.publish(Event("task.begin", {"task_id": task.id, "name": task.name}))
        task.started_at = time.time()
        while True:
            task.attempts += 1
            task.status = TaskStatus.RUNNING
            try:
                if task.timeout:
                    task.result = await asyncio.wait_for(
                        task.func(*task.args, **task.kwargs), timeout=task.timeout
                    )
                else:
                    task.result = await task.func(*task.args, **task.kwargs)
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                await self._bus.publish(
                    Event("task.complete", {"task_id": task.id, "name": task.name})
                )
                return
            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED
                task.completed_at = time.time()
                await self._bus.publish(Event("task.cancelled", {"task_id": task.id}))
                return
            except Exception as exc:  # noqa: BLE001
                task.error = repr(exc)
                log.warning("Task %s attempt %d failed: %s", task.name, task.attempts, exc)
                await self._bus.publish(
                    Event(
                        "task.error",
                        {"task_id": task.id, "name": task.name, "error": task.error},
                    )
                )
                if task.attempts <= task.max_retries:
                    task.status = TaskStatus.RETRYING
                    await asyncio.sleep(task.retry_delay * task.attempts)
                    continue
                task.status = TaskStatus.FAILED
                task.completed_at = time.time()
                await self._bus.publish(
                    Event("task.failed", {"task_id": task.id, "name": task.name})
                )
                return

    async def shutdown(self, timeout: float = 10.0) -> None:
        if not self._running:
            return
        log.info("Shutting down task manager (%d running)", len(self._running))
        for t in self._running.values():
            t.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()
