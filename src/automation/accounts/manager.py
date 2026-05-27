"""Account manager: load from JSON, track status, retry, checkpoint.

Account state is durable: it is mirrored to disk after every change so a
crash never loses progress. Recovery on startup re-reads the durable state
and merges it with the (possibly updated) source JSON file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


class AccountStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass(slots=True)
class Account:
    id: str
    username: str = ""
    email: str = ""
    password: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    status: AccountStatus = AccountStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Account":
        return cls(
            id=str(raw.get("id") or raw.get("username") or uuid.uuid4().hex[:8]),
            username=raw.get("username", ""),
            email=raw.get("email", ""),
            password=raw.get("password", ""),
            metadata=raw.get("metadata", {}) or {},
            status=AccountStatus(raw.get("status", AccountStatus.PENDING.value)),
            attempts=int(raw.get("attempts", 0)),
            last_error=raw.get("last_error"),
            started_at=raw.get("started_at"),
            completed_at=raw.get("completed_at"),
            checkpoint=raw.get("checkpoint", {}) or {},
        )


class AccountManager:
    """Coordinator for hundreds of test accounts."""

    def __init__(
        self,
        source_file: str | Path,
        state_file: str | Path = "data/state/accounts.json",
        max_attempts: int = 3,
    ) -> None:
        self.source_file = Path(source_file)
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self._accounts: dict[str, Account] = {}
        self._lock = asyncio.Lock()
        self._listeners: list[Any] = []

    # ----------------------------------------------------------------- loading
    def load(self) -> int:
        """Read source file, merge with persisted state, return count loaded."""
        source: list[dict[str, Any]] = []
        if self.source_file.exists():
            try:
                source = json.loads(self.source_file.read_text())
            except Exception:  # noqa: BLE001
                log.exception("failed to parse accounts source %s", self.source_file)
                source = []
        else:
            log.warning("accounts source %s does not exist", self.source_file)

        persisted: dict[str, Account] = {}
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                persisted = {a["id"]: Account.from_dict(a) for a in raw}
            except Exception:  # noqa: BLE001
                log.exception("failed to parse accounts state %s", self.state_file)

        out: dict[str, Account] = {}
        for raw in source:
            acc = Account.from_dict(raw)
            if acc.id in persisted:
                # carry forward runtime state
                p = persisted[acc.id]
                acc.status = p.status
                acc.attempts = p.attempts
                acc.last_error = p.last_error
                acc.started_at = p.started_at
                acc.completed_at = p.completed_at
                acc.checkpoint = p.checkpoint
            out[acc.id] = acc
        # also keep persisted accounts that no longer appear in source
        for pid, p in persisted.items():
            out.setdefault(pid, p)
        self._accounts = out
        log.info("loaded %d accounts (source=%d persisted=%d)",
                 len(out), len(source), len(persisted))
        return len(out)

    async def reload(self) -> int:
        async with self._lock:
            return self.load()

    # ----------------------------------------------------------------- queries
    def all(self) -> list[Account]:
        return list(self._accounts.values())

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def by_status(self, status: AccountStatus) -> list[Account]:
        return [a for a in self._accounts.values() if a.status == status]

    def stats(self) -> dict[str, int]:
        out = {s.value: 0 for s in AccountStatus}
        out["total"] = 0
        for a in self._accounts.values():
            out[a.status.value] += 1
            out["total"] += 1
        return out

    def progress(self) -> dict[str, Any]:
        s = self.stats()
        total = max(s["total"], 1)
        return {
            **s,
            "completion_rate": s["completed"] / total,
            "failure_rate": s["failed"] / total,
        }

    # ---------------------------------------------------------------- mutators
    async def claim_next(self) -> Account | None:
        """Return the next ``pending`` account and mark it ``running``."""
        async with self._lock:
            for acc in self._accounts.values():
                if acc.status == AccountStatus.PENDING:
                    acc.status = AccountStatus.RUNNING
                    acc.started_at = time.time()
                    acc.attempts += 1
                    await self._persist_locked()
                    return acc
        return None

    async def mark_completed(self, account_id: str) -> None:
        await self._update(account_id, status=AccountStatus.COMPLETED, completed_at=time.time())

    async def mark_failed(self, account_id: str, error: str) -> None:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return
            acc.last_error = error
            if acc.attempts >= self.max_attempts:
                acc.status = AccountStatus.FAILED
                acc.completed_at = time.time()
            else:
                acc.status = AccountStatus.PENDING  # retry later
            await self._persist_locked()

    async def pause(self, account_id: str) -> None:
        await self._update(account_id, status=AccountStatus.PAUSED)

    async def resume(self, account_id: str) -> None:
        await self._update(account_id, status=AccountStatus.PENDING)

    async def reset(self, account_id: str) -> None:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return
            acc.status = AccountStatus.PENDING
            acc.attempts = 0
            acc.last_error = None
            acc.started_at = None
            acc.completed_at = None
            acc.checkpoint = {}
            await self._persist_locked()

    async def checkpoint(self, account_id: str, data: dict[str, Any]) -> None:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return
            acc.checkpoint.update(data)
            await self._persist_locked()

    async def _update(self, account_id: str, **fields: Any) -> None:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return
            for k, v in fields.items():
                if hasattr(acc, k):
                    setattr(acc, k, v)
            await self._persist_locked()

    # ----------------------------------------------------------------- persist
    async def _persist_locked(self) -> None:
        try:
            self.state_file.write_text(
                json.dumps([a.to_dict() for a in self._accounts.values()], indent=2)
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to persist accounts state")

    # ------------------------------------------------------------------ helpers
    def iter_pending(self) -> Iterable[Account]:
        for a in self._accounts.values():
            if a.status == AccountStatus.PENDING:
                yield a
