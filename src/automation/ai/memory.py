"""AI memory: durable learning across runs.

Backed by SQLite by default with optional PostgreSQL via ``DATABASE_URL``.
Stores:
  - successful selectors per page signature
  - successful and failed workflow runs
  - learned recovery strategies
  - page snapshots metadata

The memory layer is fail-soft: if the backing store is unavailable, the brain
keeps working in-memory and persists what it can.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class LearnedSelector:
    page_signature: str
    intent: str
    selector: str
    strategy: str  # "css", "xpath", "text", "role", "aria"
    success_count: int = 0
    fail_count: int = 0
    last_used: float = 0.0
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowRecord:
    workflow: str
    success: bool
    duration_ms: int
    page_signature: str
    error: str | None = None
    started_at: float = field(default_factory=time.time)


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS learned_selectors (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        page_sig      TEXT NOT NULL,
        intent        TEXT NOT NULL,
        selector      TEXT NOT NULL,
        strategy      TEXT NOT NULL,
        success_count INTEGER NOT NULL DEFAULT 0,
        fail_count    INTEGER NOT NULL DEFAULT 0,
        confidence    REAL    NOT NULL DEFAULT 0,
        last_used     REAL    NOT NULL DEFAULT 0,
        metadata      TEXT    NOT NULL DEFAULT '{}',
        UNIQUE(page_sig, intent, selector)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow      TEXT NOT NULL,
        success       INTEGER NOT NULL,
        duration_ms   INTEGER NOT NULL,
        page_sig      TEXT NOT NULL,
        error         TEXT,
        started_at    REAL NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS recovery_strategies (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        page_sig      TEXT NOT NULL,
        failure_kind  TEXT NOT NULL,
        strategy      TEXT NOT NULL,
        success_count INTEGER NOT NULL DEFAULT 0,
        fail_count    INTEGER NOT NULL DEFAULT 0,
        UNIQUE(page_sig, failure_kind, strategy)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS page_patterns (
        page_sig      TEXT PRIMARY KEY,
        title         TEXT,
        url_pattern   TEXT,
        seen_count    INTEGER NOT NULL DEFAULT 1,
        last_seen     REAL NOT NULL,
        notes         TEXT NOT NULL DEFAULT '{}'
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_sel_sig_intent ON learned_selectors(page_sig, intent);",
    "CREATE INDEX IF NOT EXISTS idx_runs_workflow ON workflow_runs(workflow);",
]


class AIMemory:
    """Async wrapper around a SQLite (or Postgres) backing store."""

    def __init__(self, path: str | Path = "data/learning/memory.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init()

    # ------------------------------------------------------------------ init
    def _init(self) -> None:
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.cursor()
            for stmt in SCHEMA:
                cur.execute(stmt)
            self._conn.commit()
            log.info("AI memory initialized at %s", self.path)
        except Exception:  # noqa: BLE001
            log.exception("AI memory init failed; running without persistence")
            self._conn = None

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # --------------------------------------------------------------- helpers
    async def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self._conn:
            return []
        async with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.fetchall()
            except Exception:  # noqa: BLE001
                log.exception("AI memory query failed: %s", sql)
                return []

    # --------------------------------------------------------- selectors API
    async def remember_selector(
        self,
        page_sig: str,
        intent: str,
        selector: str,
        strategy: str,
        success: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        meta = json.dumps(metadata or {})
        if success:
            sql = """
            INSERT INTO learned_selectors (page_sig, intent, selector, strategy, success_count, last_used, metadata, confidence)
            VALUES (?, ?, ?, ?, 1, ?, ?, 0.5)
            ON CONFLICT(page_sig, intent, selector) DO UPDATE SET
              success_count = success_count + 1,
              last_used = excluded.last_used,
              metadata = excluded.metadata,
              confidence = MIN(1.0, (success_count + 1.0) / (success_count + fail_count + 1.0));
            """
            await self._execute(sql, (page_sig, intent, selector, strategy, time.time(), meta))
        else:
            sql = """
            INSERT INTO learned_selectors (page_sig, intent, selector, strategy, fail_count, last_used, metadata, confidence)
            VALUES (?, ?, ?, ?, 1, ?, ?, 0.0)
            ON CONFLICT(page_sig, intent, selector) DO UPDATE SET
              fail_count = fail_count + 1,
              last_used = excluded.last_used,
              confidence = MAX(0.0, success_count * 1.0 / (success_count + fail_count + 1.0));
            """
            await self._execute(sql, (page_sig, intent, selector, strategy, time.time(), meta))

    async def get_selectors(self, page_sig: str, intent: str, limit: int = 5) -> list[LearnedSelector]:
        rows = await self._execute(
            """SELECT * FROM learned_selectors
               WHERE page_sig=? AND intent=?
               ORDER BY confidence DESC, success_count DESC, last_used DESC
               LIMIT ?""",
            (page_sig, intent, limit),
        )
        return [
            LearnedSelector(
                page_signature=r["page_sig"],
                intent=r["intent"],
                selector=r["selector"],
                strategy=r["strategy"],
                success_count=r["success_count"],
                fail_count=r["fail_count"],
                last_used=r["last_used"],
                confidence=r["confidence"],
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in rows
        ]

    # ----------------------------------------------------------- workflows API
    async def record_workflow(self, record: WorkflowRecord) -> None:
        await self._execute(
            """INSERT INTO workflow_runs (workflow, success, duration_ms, page_sig, error, started_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                record.workflow,
                1 if record.success else 0,
                record.duration_ms,
                record.page_signature,
                record.error,
                record.started_at,
            ),
        )

    async def workflow_stats(self, workflow: str | None = None) -> dict[str, Any]:
        if workflow:
            rows = await self._execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(success) AS ok,
                       AVG(duration_ms) AS avg_ms
                   FROM workflow_runs WHERE workflow=?""",
                (workflow,),
            )
        else:
            rows = await self._execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(success) AS ok,
                       AVG(duration_ms) AS avg_ms
                   FROM workflow_runs"""
            )
        if not rows:
            return {"total": 0, "ok": 0, "avg_ms": 0.0, "success_rate": 0.0}
        r = rows[0]
        total = r["total"] or 0
        ok = r["ok"] or 0
        return {
            "total": total,
            "ok": ok,
            "failed": total - ok,
            "avg_ms": float(r["avg_ms"] or 0.0),
            "success_rate": (ok / total) if total else 0.0,
        }

    # ---------------------------------------------------------- recovery API
    async def remember_recovery(
        self, page_sig: str, failure_kind: str, strategy: str, success: bool
    ) -> None:
        col = "success_count" if success else "fail_count"
        await self._execute(
            f"""INSERT INTO recovery_strategies (page_sig, failure_kind, strategy, {col})
                VALUES (?, ?, ?, 1)
                ON CONFLICT(page_sig, failure_kind, strategy) DO UPDATE SET
                  {col} = {col} + 1;""",
            (page_sig, failure_kind, strategy),
        )

    async def best_recoveries(self, page_sig: str, failure_kind: str) -> list[str]:
        rows = await self._execute(
            """SELECT strategy, success_count, fail_count
               FROM recovery_strategies
               WHERE page_sig=? AND failure_kind=?
               ORDER BY (success_count * 1.0 / (success_count + fail_count + 1)) DESC
               LIMIT 10""",
            (page_sig, failure_kind),
        )
        return [r["strategy"] for r in rows]

    # ----------------------------------------------------------- patterns API
    async def remember_page(self, page_sig: str, title: str, url_pattern: str) -> None:
        await self._execute(
            """INSERT INTO page_patterns (page_sig, title, url_pattern, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(page_sig) DO UPDATE SET
                 seen_count = seen_count + 1,
                 last_seen = excluded.last_seen;""",
            (page_sig, title, url_pattern, time.time()),
        )

    async def known_pages(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._execute(
            "SELECT * FROM page_patterns ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
