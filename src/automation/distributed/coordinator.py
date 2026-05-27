"""Coordinator: distributes work across worker nodes.

The coordinator exposes a small HTTP-ish protocol over the same FastAPI
process: workers poll for assignments and post heartbeats. The default
configuration uses *one* node (the same machine), but you can run multiple
EC2 instances by pointing each worker's ``coordinator_url`` at the leader.

Workers are identified by ``worker_id``. The coordinator tracks heartbeats
and reassigns dead workers' tasks back to the queue.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Worker:
    id: str
    host: str
    started_at: float
    last_heartbeat: float
    capabilities: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    assigned: list[str] = field(default_factory=list)
    healthy: bool = True


@dataclass(slots=True)
class Assignment:
    """A unit of work distributed to a worker."""

    id: str
    kind: str  # e.g. "workflow", "account_run"
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    worker_id: str | None = None
    status: str = "queued"  # queued | running | done | failed | requeued


class Coordinator:
    """Central work distributor.

    Stateless wrt durability: persistence is up to the caller. For most
    deployments a single coordinator is plenty; for HA, run a Postgres-backed
    queue and point workers at it.
    """

    HEARTBEAT_TIMEOUT = 60.0

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}
        self._queue: asyncio.Queue[Assignment] = asyncio.Queue()
        self._assignments: dict[str, Assignment] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # ---------------------------------------------------------------- workers
    async def register_worker(
        self, host: str, capabilities: list[str] | None = None,
    ) -> Worker:
        wid = uuid.uuid4().hex[:12]
        now = time.time()
        worker = Worker(
            id=wid, host=host, started_at=now, last_heartbeat=now,
            capabilities=capabilities or [],
        )
        self._workers[wid] = worker
        log.info("worker registered id=%s host=%s", wid, host)
        return worker

    async def heartbeat(self, worker_id: str, metrics: dict[str, Any] | None = None) -> bool:
        w = self._workers.get(worker_id)
        if not w:
            return False
        w.last_heartbeat = time.time()
        w.metrics = metrics or {}
        w.healthy = True
        return True

    async def deregister(self, worker_id: str) -> None:
        w = self._workers.pop(worker_id, None)
        if not w:
            return
        # requeue everything they had
        for aid in list(w.assigned):
            a = self._assignments.get(aid)
            if a and a.status == "running":
                a.status = "requeued"
                a.worker_id = None
                await self._queue.put(a)
        log.info("worker deregistered id=%s", worker_id)

    def workers(self) -> list[Worker]:
        return list(self._workers.values())

    # ----------------------------------------------------------- assignments
    async def submit(self, kind: str, payload: dict[str, Any]) -> Assignment:
        a = Assignment(id=uuid.uuid4().hex[:12], kind=kind, payload=payload)
        self._assignments[a.id] = a
        await self._queue.put(a)
        return a

    async def claim(self, worker_id: str) -> Assignment | None:
        if worker_id not in self._workers:
            return None
        try:
            a = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        a.worker_id = worker_id
        a.status = "running"
        self._workers[worker_id].assigned.append(a.id)
        return a

    async def report(
        self, worker_id: str, assignment_id: str, status: str, info: dict[str, Any] | None = None,
    ) -> bool:
        a = self._assignments.get(assignment_id)
        if not a or a.worker_id != worker_id:
            return False
        a.status = status
        if info:
            a.payload.setdefault("_result", info)
        if status in ("done", "failed"):
            w = self._workers.get(worker_id)
            if w and assignment_id in w.assigned:
                w.assigned.remove(assignment_id)
        return True

    def stats(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for a in self._assignments.values():
            by_status[a.status] = by_status.get(a.status, 0) + 1
        return {
            "workers": len(self._workers),
            "healthy_workers": sum(1 for w in self._workers.values() if w.healthy),
            "queue_size": self._queue.qsize(),
            "assignments_by_status": by_status,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "workers": [asdict(w) for w in self._workers.values()],
            "stats": self.stats(),
        }

    # ----------------------------------------------------------------- janitor
    async def start_janitor(self, interval: float = 15.0) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._janitor_loop(interval), name="coord-janitor")

    async def stop_janitor(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _janitor_loop(self, interval: float) -> None:
        while not self._stop.is_set():
            now = time.time()
            for wid, w in list(self._workers.items()):
                if now - w.last_heartbeat > self.HEARTBEAT_TIMEOUT and w.healthy:
                    w.healthy = False
                    log.warning("worker %s missed heartbeats; will requeue assignments", wid)
                    await self.deregister(wid)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
