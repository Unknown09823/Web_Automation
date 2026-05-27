"""Worker node: registers with a coordinator and executes assignments.

The default deployment runs a single process that contains both the
coordinator and a worker (in-process). For multi-EC2 deployments, run
``automation-worker --coordinator-url http://leader:8080`` on each node.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


class WorkerNode:
    """HTTP client for the coordinator API."""

    def __init__(
        self,
        coordinator_url: str,
        api_token: str | None = None,
        capabilities: list[str] | None = None,
        poll_interval: float = 2.0,
        heartbeat_interval: float = 15.0,
    ) -> None:
        self.coordinator_url = coordinator_url.rstrip("/")
        self.api_token = api_token
        self.capabilities = capabilities or []
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.worker_id: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._handler = None

    def set_handler(self, handler) -> None:
        """Set the assignment handler: ``async (assignment_dict) -> dict``."""
        self._handler = handler

    async def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        await self._register()
        self._task = asyncio.create_task(self._run(), name="worker-run")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        if self.worker_id:
            await self._post(f"/distributed/deregister", {"worker_id": self.worker_id})

    # ------------------------------------------------------------------ loop
    async def _run(self) -> None:
        last_hb = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_hb >= self.heartbeat_interval:
                await self._heartbeat()
                last_hb = now
            assignment = await self._claim()
            if assignment:
                await self._handle(assignment)
            else:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def _handle(self, assignment: dict[str, Any]) -> None:
        info: dict[str, Any] = {}
        status = "done"
        if not self._handler:
            status = "failed"
            info = {"error": "no handler configured"}
        else:
            try:
                info = await self._handler(assignment) or {}
            except Exception as exc:  # noqa: BLE001
                log.exception("worker handler failed")
                status = "failed"
                info = {"error": repr(exc)}
        await self._post(
            "/distributed/report",
            {
                "worker_id": self.worker_id,
                "assignment_id": assignment["id"],
                "status": status,
                "info": info,
            },
        )

    # --------------------------------------------------------------- protocol
    async def _register(self) -> None:
        host = platform.node() or "unknown"
        data = await self._post(
            "/distributed/register",
            {"host": host, "capabilities": self.capabilities},
        )
        self.worker_id = (data or {}).get("worker", {}).get("id") or (data or {}).get("id")
        log.info("worker registered id=%s host=%s", self.worker_id, host)

    async def _heartbeat(self) -> None:
        if not self.worker_id:
            return
        await self._post(
            "/distributed/heartbeat",
            {"worker_id": self.worker_id, "metrics": {"ts": time.time()}},
        )

    async def _claim(self) -> dict[str, Any] | None:
        if not self.worker_id:
            return None
        data = await self._post("/distributed/claim", {"worker_id": self.worker_id})
        return (data or {}).get("assignment")

    async def _post(self, path: str, body: dict) -> dict:
        url = f"{self.coordinator_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _http_call, url, json.dumps(body).encode("utf-8"), headers,
        )


def _http_call(url: str, body: bytes, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}
    except urllib.error.URLError as exc:
        log.warning("worker http error: %s", exc)
        return {}
