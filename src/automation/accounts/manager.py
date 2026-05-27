"""Account manager: JSON-driven, SQLite-backed, hot-reloadable.

Workflow

  1. ``AccountManager`` reads accounts from a JSON file (envelope
     ``{"accounts": [...]}`` or a bare list).
  2. Every entry is validated. Invalid rows are routed to ``account_rejected``
     in the SQLite store so they are visible in the dashboard / API but
     never enter the work queue.
  3. Valid rows are upserted into ``accounts`` *preserving* runtime state —
     a hot reload never resets a completed account.
  4. Workers call :py:meth:`claim_next` to atomically lock and claim the
     next ``pending`` account. Locks are lease-based: a worker that crashes
     without releasing a lock leaves the account claimable again after the
     lease expires.
  5. After the workflow runs, the worker calls :py:meth:`mark_completed`
     or :py:meth:`mark_failed`, and (optionally) :py:meth:`record_result`
     for the audit / dashboard.

Backwards compatibility

  The constructor signature, ``Account`` dataclass field names, and the
  no-argument ``claim_next()`` continue to work exactly as before. The
  ``state_file`` parameter is now interpreted as the SQLite database path —
  SQLite does not care about the filename extension.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from automation.accounts.store import AccountStore, StoredAccount, StoredResult

log = logging.getLogger(__name__)


class AccountStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"


@dataclass(slots=True)
class Account:
    """Runtime view of a row in the ``accounts`` table.

    Field names match the legacy dataclass (``username``/``email``/``password``)
    so existing tests and call sites keep working. ``number`` is the new
    primary identifier introduced for the JSON envelope format.
    """

    id: str
    number: str = ""
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
    locked_by: str | None = None
    locked_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Account":
        # Accept both the new (``number`` / envelope) and legacy
        # (``id`` / ``username``) shapes.
        number = str(raw.get("number") or raw.get("phone") or "")
        username = str(raw.get("username") or "")
        email = str(raw.get("email") or "")
        ident = (
            str(raw.get("id"))
            if raw.get("id")
            else number or username or email or uuid.uuid4().hex[:8]
        )
        return cls(
            id=ident,
            number=number,
            username=username,
            email=email,
            password=str(raw.get("password") or raw.get("pass") or raw.get("pwd") or ""),
            metadata=dict(raw.get("metadata") or {}),
            status=AccountStatus(raw.get("status", AccountStatus.PENDING.value)),
            attempts=int(raw.get("attempts", 0)),
            last_error=raw.get("last_error"),
            started_at=raw.get("started_at"),
            completed_at=raw.get("completed_at"),
            checkpoint=dict(raw.get("checkpoint") or {}),
            locked_by=raw.get("locked_by"),
            locked_at=raw.get("locked_at"),
        )

    @classmethod
    def from_stored(cls, s: StoredAccount) -> "Account":
        return cls(
            id=s.id, number=s.number, username=s.username, email=s.email,
            password=s.password, metadata=dict(s.metadata),
            status=AccountStatus(s.status), attempts=s.attempts,
            last_error=s.last_error, started_at=s.started_at,
            completed_at=s.completed_at, checkpoint=dict(s.checkpoint),
            locked_by=s.locked_by, locked_at=s.locked_at,
        )

    def to_stored(self) -> StoredAccount:
        return StoredAccount(
            id=self.id, number=self.number, username=self.username,
            email=self.email, password=self.password, metadata=dict(self.metadata),
            status=self.status.value, attempts=self.attempts,
            last_error=self.last_error, started_at=self.started_at,
            completed_at=self.completed_at, checkpoint=dict(self.checkpoint),
            locked_by=self.locked_by, locked_at=self.locked_at,
        )


# ----------------------------------------------------------------- validation
@dataclass(slots=True)
class ValidationResult:
    valid: list[Account]
    rejected: list[tuple[int, dict[str, Any], str]]  # (index, raw, reason)


_NUM_RE = re.compile(r"^\+?\d[\d\s\-]{4,}$")


def _ident_for(raw: dict[str, Any]) -> str:
    return (
        str(raw.get("id") or "").strip()
        or str(raw.get("number") or raw.get("phone") or "").strip()
        or str(raw.get("username") or "").strip()
        or str(raw.get("email") or "").strip()
    )


def validate_accounts(
    raw_entries: list[Any],
    *,
    require_password: bool = True,
) -> ValidationResult:
    """Validate a raw list of dicts loaded from the source JSON.

    Returns a ``ValidationResult`` with valid ``Account`` instances and a
    parallel list of rejections (``(source_index, raw, reason)``). The
    function is pure — it does not touch the store.
    """
    valid: list[Account] = []
    rejected: list[tuple[int, dict[str, Any], str]] = []
    seen_ids: set[str] = set()

    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            rejected.append((idx, {"raw": entry}, "entry is not an object"))
            continue
        ident = _ident_for(entry)
        if not ident:
            rejected.append((idx, entry, "missing identifier (id/number/username/email)"))
            continue
        password = entry.get("password") or entry.get("pass") or entry.get("pwd") or ""
        if require_password and not str(password).strip():
            rejected.append((idx, entry, "missing or empty password"))
            continue
        number = str(entry.get("number") or entry.get("phone") or "").strip()
        if number and not _NUM_RE.match(number):
            rejected.append((idx, entry, f"invalid number format: {number!r}"))
            continue
        if ident in seen_ids:
            rejected.append((idx, entry, f"duplicate identifier: {ident}"))
            continue
        seen_ids.add(ident)
        valid.append(Account.from_dict(entry))
    return ValidationResult(valid=valid, rejected=rejected)


def _parse_source(text: str) -> list[Any]:
    """Parse the source JSON into a list of raw entries.

    Accepts either ``{"accounts": [...]}`` or a bare list ``[...]``.
    """
    data = json.loads(text)
    if isinstance(data, dict):
        entries = data.get("accounts")
        if isinstance(entries, list):
            return entries
        raise ValueError("expected 'accounts' array in JSON object")
    if isinstance(data, list):
        return data
    raise ValueError("source JSON must be an object with 'accounts' or a list")


# ------------------------------------------------------------------- manager
class AccountManager:
    """Async coordinator for account lifecycle and workflow assignment."""

    DEFAULT_LEASE_SECONDS = 600.0

    def __init__(
        self,
        source_file: str | Path,
        state_file: str | Path = "data/state/accounts.sqlite",
        max_attempts: int = 3,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        require_password: bool = True,
    ) -> None:
        self.source_file = Path(source_file)
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = int(max_attempts)
        self.lease_seconds = float(lease_seconds)
        self.require_password = bool(require_password)
        self._store = AccountStore(self.state_file)
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[dict[str, Any]], Awaitable[None]]] = []

        self.last_loaded_at: float = 0.0
        self.last_source_mtime: float = 0.0
        self.last_load_count: int = 0
        self.last_rejected_count: int = 0
        self._watch_task: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- loading
    def load(self, *, replace_static: bool = True) -> int:
        """Read source JSON, validate, and upsert into SQLite.

        Returns the number of *valid* accounts loaded. Invalid rows go to the
        ``account_rejected`` table. Runtime state of existing accounts is
        always preserved (a reload never resets ``completed`` / ``failed``).
        """
        if not self.source_file.exists():
            log.warning("accounts source %s does not exist", self.source_file)
            self._store.clear_rejected()
            self.last_loaded_at = time.time()
            self.last_load_count = 0
            self.last_rejected_count = 0
            return 0

        try:
            text = self.source_file.read_text()
        except OSError:
            log.exception("failed to read accounts source %s", self.source_file)
            return 0

        try:
            entries = _parse_source(text)
        except (ValueError, json.JSONDecodeError) as exc:
            log.error("failed to parse accounts source: %s", exc)
            return 0

        result = validate_accounts(entries, require_password=self.require_password)

        # rejected list reflects the *current* source file
        self._store.clear_rejected()
        for idx, raw, reason in result.rejected:
            self._store.add_rejected(raw, reason, idx)

        for acc in result.valid:
            stored = acc.to_stored()
            self._store.upsert(stored, preserve_runtime=replace_static)

        try:
            self.last_source_mtime = self.source_file.stat().st_mtime
        except OSError:
            self.last_source_mtime = 0.0
        self.last_loaded_at = time.time()
        self.last_load_count = len(result.valid)
        self.last_rejected_count = len(result.rejected)
        log.info(
            "loaded accounts: valid=%d rejected=%d source=%s",
            len(result.valid), len(result.rejected), self.source_file,
        )
        return len(result.valid)

    async def reload(self) -> int:
        """Re-read the source file under the manager lock."""
        async with self._lock:
            n = self.load()
            for listener in list(self._listeners):
                try:
                    await listener({
                        "loaded": n,
                        "rejected": self.last_rejected_count,
                    })
                except Exception:  # noqa: BLE001
                    log.exception("accounts reload listener failed")
            return n

    def add_reload_listener(
        self, listener: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._listeners.append(listener)

    # ----------------------------------------------------------------- queries
    def all(self) -> list[Account]:
        return [Account.from_stored(s) for s in self._store.all()]

    def get(self, account_id: str) -> Account | None:
        s = self._store.get(account_id)
        return Account.from_stored(s) if s else None

    def by_status(self, status: AccountStatus, limit: int | None = None) -> list[Account]:
        return [Account.from_stored(s) for s in self._store.by_status(status.value, limit)]

    def stats(self) -> dict[str, int]:
        out = {s.value: 0 for s in AccountStatus}
        out["total"] = 0
        store_stats = self._store.stats()
        for k, v in store_stats.items():
            if k == "total":
                out["total"] = v
            else:
                out[k] = v
        # ensure total is consistent even if store_stats had unknown statuses
        if "total" not in store_stats:
            out["total"] = sum(v for k, v in out.items() if k != "total")
        return out

    def progress(self) -> dict[str, Any]:
        s = self.stats()
        total = max(s.get("total", 0), 1)
        speed = self._store.processing_speed(window_seconds=60.0)
        return {
            **s,
            "rejected": self.last_rejected_count,
            "completion_rate": s.get("completed", 0) / total,
            "failure_rate": s.get("failed", 0) / total,
            "speed_per_minute": speed["per_minute"],
            "speed_window_completed": speed["completed"],
            "speed_window_failed": speed["failed"],
            "last_loaded_at": self.last_loaded_at,
        }

    def rejected(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._store.list_rejected(limit=limit)

    def results(
        self, account_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = self._store.list_results(account_id=account_id, limit=limit)
        return [
            {
                "id": r.id,
                "account_id": r.account_id,
                "workflow": r.workflow,
                "success": r.success,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "result": r.result,
            }
            for r in rows
        ]

    def processing_speed(self, window_seconds: float = 60.0) -> dict[str, float]:
        return self._store.processing_speed(window_seconds=window_seconds)

    # ---------------------------------------------------------------- mutators
    async def claim_next(
        self,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> Account | None:
        """Atomically claim the next pending account.

        ``worker_id`` is recorded as the lock holder. Calling without a
        ``worker_id`` (legacy API) auto-generates a stable identifier so
        every claim still gets a unique lock.
        """
        wid = worker_id or f"local-{uuid.uuid4().hex[:8]}"
        lease = float(lease_seconds or self.lease_seconds)
        async with self._lock:
            stored = self._store.claim_pending(worker_id=wid, lease_seconds=lease)
            if not stored:
                return None
            acc = Account.from_stored(stored)
            log.info(
                "claimed account id=%s number=%s by worker=%s attempts=%d",
                acc.id, acc.number or acc.username, wid, acc.attempts,
            )
            return acc

    async def mark_completed(
        self,
        account_id: str,
        *,
        result: dict[str, Any] | None = None,
        workflow: str = "",
        started_at: float | None = None,
    ) -> None:
        async with self._lock:
            now = time.time()
            self._store.update_status(
                account_id, AccountStatus.COMPLETED.value,
                completed_at=now,
            )
            self._record_result_locked(
                account_id=account_id, success=True,
                started_at=started_at, ended_at=now,
                workflow=workflow, result=result, error=None,
            )

    async def mark_failed(
        self,
        account_id: str,
        error: str,
        *,
        workflow: str = "",
        started_at: float | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Record a failed attempt.

        The account is moved back to ``pending`` for retry until
        ``attempts >= max_attempts``, then it is permanently ``failed``.
        """
        async with self._lock:
            now = time.time()
            stored = self._store.get(account_id)
            if not stored:
                return
            permanent = stored.attempts >= self.max_attempts
            new_status = (
                AccountStatus.FAILED.value if permanent else AccountStatus.PENDING.value
            )
            self._store.update_status(
                account_id, new_status,
                last_error=error,
                completed_at=(now if permanent else None),
            )
            self._record_result_locked(
                account_id=account_id, success=False,
                started_at=started_at, ended_at=now,
                workflow=workflow, result=result, error=error,
            )

    async def mark_skipped(self, account_id: str, reason: str = "") -> None:
        async with self._lock:
            self._store.update_status(
                account_id, AccountStatus.SKIPPED.value,
                last_error=reason or None,
                completed_at=time.time(),
            )

    async def pause(self, account_id: str) -> None:
        async with self._lock:
            self._store.update_status(account_id, AccountStatus.PAUSED.value)

    async def resume(self, account_id: str) -> None:
        async with self._lock:
            self._store.update_status(account_id, AccountStatus.PENDING.value)

    async def reset(self, account_id: str) -> None:
        async with self._lock:
            self._store.reset(account_id)

    async def checkpoint(self, account_id: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._store.set_checkpoint(account_id, data)

    async def release_lock(self, account_id: str, worker_id: str | None = None) -> bool:
        async with self._lock:
            return self._store.release(account_id, worker_id)

    async def reset_stuck_locks(self) -> int:
        async with self._lock:
            return self._store.reset_stuck_locks(self.lease_seconds)

    async def record_result(
        self,
        account_id: str,
        *,
        success: bool,
        started_at: float | None,
        ended_at: float | None = None,
        duration_ms: int | None = None,
        workflow: str = "",
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> int:
        """Append a result record without changing account status.

        Useful for callers that want to log intermediate runs (e.g. dry-run
        or read-only verification) outside of ``mark_completed`` /
        ``mark_failed``. Returns the inserted row id.
        """
        async with self._lock:
            return self._record_result_locked(
                account_id=account_id, success=success,
                started_at=started_at,
                ended_at=ended_at if ended_at is not None else time.time(),
                duration_ms=duration_ms,
                workflow=workflow, error=error, result=result,
            )

    # ------------------------------------------------------------------ helpers
    def _record_result_locked(
        self,
        *,
        account_id: str,
        success: bool,
        started_at: float | None,
        ended_at: float,
        workflow: str = "",
        result: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> int:
        sa = float(started_at if started_at is not None else ended_at)
        if duration_ms is None:
            duration_ms = int(max(0.0, ended_at - sa) * 1000)
        rec = StoredResult(
            id=0, account_id=account_id, workflow=workflow or "",
            success=success, started_at=sa, ended_at=ended_at,
            duration_ms=int(duration_ms), error=error, result=dict(result or {}),
        )
        return self._store.record_result(rec)

    def iter_pending(self) -> Iterable[Account]:
        for s in self._store.iter_pending():
            yield Account.from_stored(s)

    # --------------------------------------------------------------- watcher
    async def watch(self, interval: float = 2.0) -> None:
        """Polling watcher that calls :py:meth:`reload` when the source changes.

        Robust to race conditions: it compares mtime, skips reloads while a
        prior reload is in flight (via ``self._lock``), and never raises.
        """
        log.info("accounts watcher started (interval=%.1fs)", interval)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    if not self.source_file.exists():
                        continue
                    mtime = self.source_file.stat().st_mtime
                    if mtime > self.last_source_mtime:
                        log.info("accounts source changed, reloading")
                        await self.reload()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("accounts watcher tick failed")
        except asyncio.CancelledError:
            log.info("accounts watcher stopped")
            raise

    def start_watcher(self, interval: float = 2.0) -> asyncio.Task[None]:
        if self._watch_task and not self._watch_task.done():
            return self._watch_task
        self._watch_task = asyncio.create_task(self.watch(interval), name="accounts-watch")
        return self._watch_task

    async def stop_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._watch_task = None

    def close(self) -> None:
        self._store.close()
