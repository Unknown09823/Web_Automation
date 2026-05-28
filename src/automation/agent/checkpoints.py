"""Checkpoint and resume system.

Stores per-goal checkpoints so the agent can resume from the last
successful stage after a crash, restart, or network disconnect.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Checkpoint:
    """A saved point of progress for one account in a run."""

    account_id: str
    goal_index: int
    goal_type: str
    goal_description: str
    status: str  # "completed" | "failed"
    url: str = ""
    cookies_path: str | None = None
    screenshot_path: str | None = None
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "goal_index": self.goal_index,
            "goal_type": self.goal_type,
            "goal_description": self.goal_description,
            "status": self.status,
            "url": self.url,
            "cookies_path": self.cookies_path,
            "screenshot_path": self.screenshot_path,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        return cls(
            account_id=data["account_id"],
            goal_index=data["goal_index"],
            goal_type=data["goal_type"],
            goal_description=data.get("goal_description", ""),
            status=data["status"],
            url=data.get("url", ""),
            cookies_path=data.get("cookies_path"),
            screenshot_path=data.get("screenshot_path"),
            timestamp=data.get("timestamp", 0),
            data=data.get("data", {}),
        )



class CheckpointManager:
    """Persist and query checkpoints for resume capability.

    Checkpoints are stored in the run folder as checkpoints.json per account.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def _path(self, account_id: str) -> Path:
        safe = "".join(
            c if c.isalnum() or c in "-_." else "_" for c in account_id
        )[:128]
        return self.run_dir / safe / "checkpoints.json"

    def save(self, checkpoint: Checkpoint) -> None:
        """Append a checkpoint for an account."""
        path = self._path(checkpoint.account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.load_all(checkpoint.account_id)
        existing.append(checkpoint)
        path.write_text(
            json.dumps([c.to_dict() for c in existing], indent=2, default=str)
        )

    def load_all(self, account_id: str) -> list[Checkpoint]:
        """Load all checkpoints for an account."""
        path = self._path(account_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            return [Checkpoint.from_dict(d) for d in data]
        except (json.JSONDecodeError, KeyError):
            return []

    def last_successful(self, account_id: str) -> Checkpoint | None:
        """Get the latest successful checkpoint for resume."""
        checkpoints = self.load_all(account_id)
        for cp in reversed(checkpoints):
            if cp.status == "completed":
                return cp
        return None

    def resume_index(self, account_id: str) -> int:
        """Return the goal index to resume from (next after last success).

        Returns 0 if no checkpoints exist.
        """
        last = self.last_successful(account_id)
        if last is None:
            return 0
        return last.goal_index + 1

    def clear(self, account_id: str) -> None:
        """Remove all checkpoints for an account (fresh start)."""
        path = self._path(account_id)
        if path.exists():
            path.unlink()

    def summary(self, account_id: str) -> dict[str, Any]:
        """Get a summary of checkpoint state for an account."""
        checkpoints = self.load_all(account_id)
        if not checkpoints:
            return {"total": 0, "last_successful": None, "resume_from": 0}
        last_ok = self.last_successful(account_id)
        return {
            "total": len(checkpoints),
            "completed": sum(1 for c in checkpoints if c.status == "completed"),
            "failed": sum(1 for c in checkpoints if c.status == "failed"),
            "last_successful": last_ok.to_dict() if last_ok else None,
            "resume_from": self.resume_index(account_id),
        }
