"""SQLite-backed account state store.

A thin synchronous wrapper around ``sqlite3`` that the async ``AccountManager``
serializes through an ``asyncio.Lock``. Storing state in SQLite (instead of a
JSON file) gives us:

* Atomic per-row updates for safe lease-based locking across many workers.
* Append-only result history without rewriting the whole file each time.
* Crash-safe checkpoints (WAL mode) so an EC2 reboot loses nothing.

Three tables:

``accounts``
    One row per account. Mirrors the loaded JSON entry plus runtime state
    (status, attempts, lock holder, checkpoint blob).

``account_results``
    Append-only record of each workflow run. Used for processing-speed
    metrics, reporting, and audit.

``account_rejected``
    Entries that failed JSON validation. They never enter the work queue
    but are kept for visibility in the dashboard / API.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id            TEXT PRIMARY KEY,
        number        TEXT NOT NULL DEFAULT '',
        username      TEXT NOT NULL DEFAULT '',
        email         TEXT NOT NULL DEFAULT '',
        password      TEXT NOT NULL DEFAULT '',
        metadata      TEXT NOT NULL DEFAULT '{}',
        status        TEXT NOT NULL DEFAULT 'pending',
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        started_at    REAL,
        completed_at  REAL,
        checkpoint    TEXT NOT NULL DEFAULT '{}',
        locked_by     TEXT,
        locked_at     REAL,
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS account_results (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id    TEXT NOT NULL,
        workflow      TEXT NOT NULL DEFAULT '',
        success       INTEGER NOT NULL,
        started_at    REAL NOT NULL,
        ended_at      REAL NOT NULL,
        duration_ms   INTEGER NOT NULL,
        error         TEXT,
        result        TEXT NOT NULL DEFAULT '{}'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS account_rejected (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        raw           TEXT NOT NULL,
        reason        TEXT NOT NULL,
        source_index  INTEGER,
        ts            REAL NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_accounts_status   ON accounts(status);",
    "CREATE INDEX IF NOT EXISTS idx_accounts_locked   ON accounts(locked_by);",
    "CREATE INDEX IF NOT EXISTS idx_results_account   ON account_results(account_id);",
    "CREATE INDEX IF NOT EXISTS idx_results_completed ON account_results(ended_at);",
]


@dataclass(slots=True)
class StoredAccount:
    """Plain row representation; the manager wraps this in ``Account``."""

    id: str
    number: str = ""
    username: str = ""
    email: str = ""
    password: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    locked_by: str | None = None
    locked_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(slots=True)
class StoredResult:
    """One workflow run against an account."""

    id: int
    account_id: str
    workflow: str
    success: bool
    started_at: float
    ended_at: float
    duration_ms: int
    error: str | None
    result: dict[str, Any]


def _row_to_account(row: sqlite3.Row) -> StoredAccount:
    return StoredAccount(
        id=row["id"],
        number=row["number"] or "",
        username=row["username"] or "",
        email=row["email"] or "",
        password=row["password"] or "",
        metadata=json.loads(row["metadata"] or "{}"),
        status=row["status"],
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        checkpoint=json.loads(row["checkpoint"] or "{}"),
        locked_by=row["locked_by"],
        locked_at=row["locked_at"],
        created_at=row["created_at"] or 0.0,
        updated_at=row["updated_at"] or 0.0,
    )


def _row_to_result(row: sqlite3.Row) -> StoredResult:
    return StoredResult(
        id=int(row["id"]),
        account_id=row["account_id"],
        workflow=row["workflow"],
        success=bool(row["success"]),
        started_at=float(row["started_at"]),
        ended_at=float(row["ended_at"]),
        duration_ms=int(row["duration_ms"]),
        error=row["error"],
        result=json.loads(row["result"] or "{}"),
    )


class AccountStore:
    """Synchronous SQLite store for account state and results."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` is safe because the manager serializes
        # every call through a single asyncio.Lock.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        for stmt in SCHEMA:
            self._conn.execute(stmt)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing account store")

    # -------------------------------------------------------------- accounts
    def upsert(self, acc: StoredAccount, *, preserve_runtime: bool = True) -> None:
        """Insert or update an account.

        When ``preserve_runtime`` is True (the default for hot-reload), only
        the static fields (number/username/email/password/metadata) are
        overwritten — runtime state (status/attempts/checkpoint/lock) is
        preserved so a reload doesn't reset progress.
        """
        now = time.time()
        if preserve_runtime:
            sql = """
            INSERT INTO accounts (
                id, number, username, email, password, metadata,
                status, attempts, checkpoint, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, '{}', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                number    = excluded.number,
                username  = excluded.username,
                email     = excluded.email,
                password  = excluded.password,
                metadata  = excluded.metadata,
                updated_at = excluded.updated_at;
            """
            self._conn.execute(
                sql,
                (
                    acc.id, acc.number, acc.username, acc.email, acc.password,
                    json.dumps(acc.metadata or {}),
                    now, now,
                ),
            )
        else:
            sql = """
            INSERT OR REPLACE INTO accounts (
                id, number, username, email, password, metadata,
                status, attempts, last_error, started_at, completed_at,
                checkpoint, locked_by, locked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """
            self._conn.execute(
                sql,
                (
                    acc.id, acc.number, acc.username, acc.email, acc.password,
                    json.dumps(acc.metadata or {}),
                    acc.status, acc.attempts, acc.last_error,
                    acc.started_at, acc.completed_at,
                    json.dumps(acc.checkpoint or {}),
                    acc.locked_by, acc.locked_at,
                    acc.created_at or now, now,
                ),
            )
        self._conn.commit()

    def delete(self, account_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def get(self, account_id: str) -> StoredAccount | None:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        return _row_to_account(row) if row else None

    def all(self) -> list[StoredAccount]:
        rows = self._conn.execute("SELECT * FROM accounts ORDER BY created_at, id").fetchall()
        return [_row_to_account(r) for r in rows]

    def by_status(self, status: str, limit: int | None = None) -> list[StoredAccount]:
        if limit is None:
            rows = self._conn.execute(
                "SELECT * FROM accounts WHERE status=? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM accounts WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        return [_row_to_account(r) for r in rows]

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) c FROM accounts GROUP BY status"
        ).fetchall()
        out = {r["status"]: int(r["c"]) for r in rows}
        out["total"] = sum(out.values())
        return out

    def existing_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT id FROM accounts").fetchall()
        return {r["id"] for r in rows}

    # ------------------------------------------------------------- locking
    def claim_pending(
        self,
        worker_id: str,
        lease_seconds: float,
    ) -> StoredAccount | None:
        """Atomically claim one ``pending`` account.

        Uses a transaction with ``BEGIN IMMEDIATE`` so two concurrent
        ``claim_pending`` calls cannot return the same row.

        A row is claimable if it is ``pending`` (with no active lock or an
        expired lease) **or** ``running`` whose lease has expired — this is
        the crashed-worker recovery path. Expired-leased ``running`` rows
        are first demoted to ``pending`` in the same transaction.
        """
        now = time.time()
        lease_floor = now - lease_seconds
        try:
            self._conn.execute("BEGIN IMMEDIATE;")
            # Reclaim crashed workers: running accounts whose lease expired
            self._conn.execute(
                """
                UPDATE accounts SET
                    status='pending',
                    locked_by=NULL,
                    locked_at=NULL,
                    updated_at=?
                WHERE status='running'
                  AND locked_at IS NOT NULL
                  AND locked_at < ?
                """,
                (now, lease_floor),
            )
            row = self._conn.execute(
                """
                SELECT * FROM accounts
                WHERE status = 'pending'
                  AND (locked_by IS NULL OR locked_at IS NULL OR locked_at < ?)
                ORDER BY created_at, id
                LIMIT 1
                """,
                (lease_floor,),
            ).fetchone()
            if not row:
                self._conn.commit()
                return None
            acc_id = row["id"]
            self._conn.execute(
                """
                UPDATE accounts SET
                    status='running',
                    attempts = attempts + 1,
                    started_at=?,
                    locked_by=?,
                    locked_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (now, worker_id, now, now, acc_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM accounts WHERE id=?", (acc_id,)
            ).fetchone()
            return _row_to_account(updated) if updated else None
        except Exception:  # noqa: BLE001
            self._conn.rollback()
            raise

    def release(self, account_id: str, worker_id: str | None = None) -> bool:
        """Drop a lock without changing status. Returns True if released."""
        now = time.time()
        if worker_id is None:
            cur = self._conn.execute(
                "UPDATE accounts SET locked_by=NULL, locked_at=NULL, updated_at=? WHERE id=?",
                (now, account_id),
            )
        else:
            cur = self._conn.execute(
                "UPDATE accounts SET locked_by=NULL, locked_at=NULL, updated_at=? "
                "WHERE id=? AND locked_by=?",
                (now, account_id, worker_id),
            )
        self._conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------- mutations
    def update_status(
        self,
        account_id: str,
        status: str,
        *,
        last_error: str | None = None,
        completed_at: float | None = None,
        clear_lock: bool = True,
    ) -> bool:
        now = time.time()
        sets = ["status=?", "updated_at=?"]
        params: list[Any] = [status, now]
        if last_error is not None:
            sets.append("last_error=?"); params.append(last_error)
        if completed_at is not None:
            sets.append("completed_at=?"); params.append(completed_at)
        if clear_lock:
            sets.append("locked_by=NULL"); sets.append("locked_at=NULL")
        params.append(account_id)
        cur = self._conn.execute(
            f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", params,
        )
        self._conn.commit()
        return cur.rowcount > 0

    def reset(self, account_id: str) -> bool:
        cur = self._conn.execute(
            """
            UPDATE accounts SET
                status='pending', attempts=0, last_error=NULL,
                started_at=NULL, completed_at=NULL, checkpoint='{}',
                locked_by=NULL, locked_at=NULL, updated_at=?
            WHERE id=?
            """,
            (time.time(), account_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def set_checkpoint(self, account_id: str, data: dict[str, Any]) -> bool:
        # merge with existing
        row = self._conn.execute(
            "SELECT checkpoint FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row:
            return False
        merged = json.loads(row["checkpoint"] or "{}")
        merged.update(data or {})
        cur = self._conn.execute(
            "UPDATE accounts SET checkpoint=?, updated_at=? WHERE id=?",
            (json.dumps(merged), time.time(), account_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------- results
    def record_result(self, result: StoredResult) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO account_results
                (account_id, workflow, success, started_at, ended_at,
                 duration_ms, error, result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                result.account_id, result.workflow,
                1 if result.success else 0,
                result.started_at, result.ended_at,
                result.duration_ms, result.error,
                json.dumps(result.result or {}),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def list_results(
        self, account_id: str | None = None, limit: int = 100,
    ) -> list[StoredResult]:
        if account_id:
            rows = self._conn.execute(
                "SELECT * FROM account_results WHERE account_id=? "
                "ORDER BY ended_at DESC LIMIT ?",
                (account_id, int(limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM account_results ORDER BY ended_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_result(r) for r in rows]

    # ----------------------------------------------------------- rejected
    def add_rejected(self, raw: dict[str, Any], reason: str, source_index: int) -> None:
        self._conn.execute(
            "INSERT INTO account_rejected (raw, reason, source_index, ts) VALUES (?, ?, ?, ?)",
            (json.dumps(raw, default=str), reason, int(source_index), time.time()),
        )
        self._conn.commit()

    def clear_rejected(self) -> int:
        cur = self._conn.execute("DELETE FROM account_rejected")
        self._conn.commit()
        return cur.rowcount

    def list_rejected(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM account_rejected ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "raw": json.loads(r["raw"]),
                "reason": r["reason"],
                "source_index": int(r["source_index"]) if r["source_index"] is not None else None,
                "ts": float(r["ts"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------- metrics
    def processing_speed(self, window_seconds: float = 60.0) -> dict[str, float]:
        """Accounts completed and failed within the trailing window.

        Returns counts plus per-minute rate so the dashboard can render a
        single ``speed`` number regardless of window choice.
        """
        floor = time.time() - window_seconds
        completed = int(
            self._conn.execute(
                "SELECT COUNT(*) c FROM account_results WHERE ended_at >= ? AND success=1",
                (floor,),
            ).fetchone()["c"]
        )
        failed = int(
            self._conn.execute(
                "SELECT COUNT(*) c FROM account_results WHERE ended_at >= ? AND success=0",
                (floor,),
            ).fetchone()["c"]
        )
        per_min = (completed / window_seconds) * 60.0 if window_seconds > 0 else 0.0
        return {
            "window_seconds": float(window_seconds),
            "completed": completed,
            "failed": failed,
            "per_minute": round(per_min, 3),
        }

    def reset_stuck_locks(self, lease_seconds: float) -> int:
        """Demote running rows whose lease expired back to pending.

        Returns the number of rows reset. Equivalent to the recovery step
        run inside :py:meth:`claim_pending` but exposed as a standalone
        operation (e.g. from ``POST /accounts/locks/reap``).
        """
        floor = time.time() - lease_seconds
        cur = self._conn.execute(
            """
            UPDATE accounts SET
                status='pending',
                locked_by=NULL,
                locked_at=NULL,
                updated_at=?
            WHERE status='running'
              AND locked_at IS NOT NULL
              AND locked_at < ?
            """,
            (time.time(), floor),
        )
        self._conn.commit()
        return cur.rowcount

    def iter_pending(self) -> Iterable[StoredAccount]:
        for row in self._conn.execute(
            "SELECT * FROM accounts WHERE status='pending' ORDER BY created_at, id"
        ):
            yield _row_to_account(row)
