"""Lightweight async scheduler: cron-ish intervals with concurrency control."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

JobFunc = Callable[[], Awaitable[Any]]


@dataclass(slots=True)
class Job:
    name: str
    func: JobFunc
    interval: float
    last_run: float = 0.0
    next_run: float = 0.0
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class Scheduler:
    """Run interval-based async jobs with bounded concurrency."""

    def __init__(self, max_workers: int = 10) -> None:
        self._jobs: dict[str, Job] = {}
        self._semaphore = asyncio.Semaphore(max_workers)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._max_workers = max_workers

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def set_max_workers(self, n: int) -> None:
        self._max_workers = n
        self._semaphore = asyncio.Semaphore(n)

    def add_job(self, name: str, func: JobFunc, interval: float) -> Job:
        job = Job(name=name, func=func, interval=interval, next_run=time.time() + interval)
        self._jobs[name] = job
        log.info("Scheduled job=%s every %.1fs", name, interval)
        return job

    def remove_job(self, name: str) -> bool:
        return self._jobs.pop(name, None) is not None

    def list_jobs(self) -> list[Job]:
        return list(self._jobs.values())

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="scheduler-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _loop(self) -> None:
        log.info("Scheduler started (max_workers=%d)", self._max_workers)
        while not self._stop.is_set():
            now = time.time()
            for job in list(self._jobs.values()):
                if not job.enabled:
                    continue
                if now >= job.next_run:
                    job.last_run = now
                    job.next_run = now + job.interval
                    asyncio.create_task(self._run_job(job), name=f"job:{job.name}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
        log.info("Scheduler stopped")

    async def _run_job(self, job: Job) -> None:
        async with self._semaphore:
            try:
                await job.func()
            except Exception:  # noqa: BLE001
                log.exception("Scheduled job failed: %s", job.name)
