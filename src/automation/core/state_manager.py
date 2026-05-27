"""Durable state manager with JSON-backed checkpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StateManager:
    """Process-wide key/value state with optional persistence."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._state: dict[str, Any] = {}
        self._path = Path(path) if path else None
        self._lock = asyncio.Lock()
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        try:
            self._state = json.loads(self._path.read_text())  # type: ignore[union-attr]
            log.info("Loaded state from %s", self._path)
        except Exception:  # noqa: BLE001
            log.exception("Failed to load state, starting fresh")
            self._state = {}

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            return self._state.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._state[key] = value
            await self._persist_locked()

    async def update(self, mapping: dict[str, Any]) -> None:
        async with self._lock:
            self._state.update(mapping)
            await self._persist_locked()

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._state.pop(key, None)
            await self._persist_locked()

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return dict(self._state)

    async def _persist_locked(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # atomic write
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            log.exception("Failed to persist state")
            try:
                os.unlink(tmp)
            except OSError:
                pass
