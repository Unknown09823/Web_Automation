"""RunContext: execution memory and run-folder layout.

Each agent run gets an isolated directory:

    data/runs/run_<timestamp>_<short_id>/
    ├── <account_id>/
    │   ├── screenshots/
    │   ├── html/
    │   ├── cookies/
    │   ├── logs/
    │   ├── memory.json       # AI reasoning + state
    │   └── replay.json       # action ledger
    ├── plan.json             # original execution plan
    ├── status.json           # overall run status
    └── events.jsonl          # streaming event log
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class RunStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class RunContext:
    """Manages the execution state and file layout for one agent run.

    Provides methods to create account subdirs, save screenshots / HTML /
    cookies, update status, and persist the plan + events.
    """

    run_id: str
    base_dir: Path
    status: RunStatus = RunStatus.CREATED
    instruction: str = ""
    goals: list[dict[str, Any]] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        runs_root: str | Path = "data/runs",
        *,
        instruction: str = "",
        goals: list[dict[str, Any]] | None = None,
        accounts: list[str] | None = None,
    ) -> "RunContext":
        """Create a new run with a unique directory."""
        short_id = uuid.uuid4().hex[:8]
        ts = int(time.time())
        run_id = f"run_{ts}_{short_id}"
        base = Path(runs_root) / run_id
        base.mkdir(parents=True, exist_ok=True)

        ctx = cls(
            run_id=run_id,
            base_dir=base,
            instruction=instruction,
            goals=goals or [],
            accounts=accounts or [],
        )
        ctx._save_status()
        return ctx

    @classmethod
    def load(cls, run_dir: str | Path) -> "RunContext":
        """Load an existing run from its status.json."""
        base = Path(run_dir)
        status_file = base / "status.json"
        if not status_file.exists():
            raise FileNotFoundError(f"No status.json in {base}")
        data = json.loads(status_file.read_text())
        return cls(
            run_id=data["run_id"],
            base_dir=base,
            status=RunStatus(data.get("status", "created")),
            instruction=data.get("instruction", ""),
            goals=data.get("goals", []),
            accounts=data.get("accounts", []),
            created_at=data.get("created_at", 0),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            metadata=data.get("metadata", {}),
        )

    # ---------------------------------------------------------------- dirs
    def account_dir(self, account_id: str) -> Path:
        """Get or create the account-specific subdirectory."""
        d = self.base_dir / _safe(account_id)
        d.mkdir(parents=True, exist_ok=True)
        for sub in ("screenshots", "html", "cookies", "logs"):
            (d / sub).mkdir(exist_ok=True)
        return d

    def screenshots_dir(self, account_id: str) -> Path:
        return self.account_dir(account_id) / "screenshots"

    def html_dir(self, account_id: str) -> Path:
        return self.account_dir(account_id) / "html"

    def cookies_dir(self, account_id: str) -> Path:
        return self.account_dir(account_id) / "cookies"

    def logs_dir(self, account_id: str) -> Path:
        return self.account_dir(account_id) / "logs"

    # ---------------------------------------------------------------- state
    def set_status(self, status: RunStatus, error: str | None = None) -> None:
        self.status = status
        if error:
            self.error = error
        if status == RunStatus.RUNNING and self.started_at is None:
            self.started_at = time.time()
        if status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            self.completed_at = time.time()
        self._save_status()

    def _save_status(self) -> None:
        data = {
            "run_id": self.run_id,
            "status": self.status.value,
            "instruction": self.instruction,
            "goals": self.goals,
            "accounts": self.accounts,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metadata": self.metadata,
        }
        (self.base_dir / "status.json").write_text(
            json.dumps(data, indent=2, default=str)
        )

    def save_plan(self, plan: dict[str, Any]) -> None:
        (self.base_dir / "plan.json").write_text(
            json.dumps(plan, indent=2, default=str)
        )

    # ---------------------------------------------------------------- events
    def append_event(self, event: dict[str, Any]) -> None:
        """Append an event to the JSONL log."""
        with open(self.base_dir / "events.jsonl", "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def load_events(self) -> list[dict[str, Any]]:
        path = self.base_dir / "events.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return events

    # ---------------------------------------------------------------- account memory
    def save_memory(self, account_id: str, memory: dict[str, Any]) -> None:
        """Save agent reasoning / state for an account."""
        path = self.account_dir(account_id) / "memory.json"
        path.write_text(json.dumps(memory, indent=2, default=str))

    def load_memory(self, account_id: str) -> dict[str, Any]:
        path = self.account_dir(account_id) / "memory.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}

    def save_replay(self, account_id: str, replay: list[dict[str, Any]]) -> None:
        path = self.account_dir(account_id) / "replay.json"
        path.write_text(json.dumps(replay, indent=2, default=str))

    def load_replay(self, account_id: str) -> list[dict[str, Any]]:
        path = self.account_dir(account_id) / "replay.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return []

    # ---------------------------------------------------------------- screenshots
    async def save_screenshot(self, page: Any, account_id: str, label: str = "") -> str | None:
        """Take and save a screenshot. Returns the path or None."""
        ts = int(time.time() * 1000)
        name = f"{label}_{ts}.png" if label else f"shot_{ts}.png"
        path = self.screenshots_dir(account_id) / name
        try:
            await page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:  # noqa: BLE001
            log.debug("screenshot save failed for %s", account_id)
            return None

    async def save_html(self, page: Any, account_id: str, label: str = "") -> str | None:
        """Save page HTML snapshot."""
        ts = int(time.time() * 1000)
        name = f"{label}_{ts}.html" if label else f"page_{ts}.html"
        path = self.html_dir(account_id) / name
        try:
            content = await page.content()
            path.write_text(content[:500_000])  # cap at 500KB
            return str(path)
        except Exception:  # noqa: BLE001
            log.debug("html save failed for %s", account_id)
            return None

    async def save_cookies(self, context: Any, account_id: str) -> str | None:
        """Save browser storage state (cookies + localStorage)."""
        ts = int(time.time() * 1000)
        path = self.cookies_dir(account_id) / f"state_{ts}.json"
        try:
            state = await context.storage_state()
            path.write_text(json.dumps(state, indent=2))
            return str(path)
        except Exception:  # noqa: BLE001
            log.debug("cookies save failed for %s", account_id)
            return None

    # ---------------------------------------------------------------- summary
    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "instruction": self.instruction,
            "goals": self.goals,
            "accounts": self.accounts,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metadata": self.metadata,
            "dir": str(self.base_dir),
        }


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:128]


def list_runs(runs_root: str | Path = "data/runs", limit: int = 50) -> list[dict[str, Any]]:
    """List recent runs from the runs directory."""
    root = Path(runs_root)
    if not root.exists():
        return []
    runs = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        status_file = d / "status.json"
        if status_file.exists():
            try:
                data = json.loads(status_file.read_text())
                runs.append(data)
            except json.JSONDecodeError:
                pass
        if len(runs) >= limit:
            break
    return runs
