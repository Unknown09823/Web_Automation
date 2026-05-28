"""Action recorder: ledger of every action for replay.

Records clicks, inputs, navigation, screenshots, DOM snapshots,
and AI decisions. The replay.json file allows post-execution review
and deterministic re-execution.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class RecordType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    HTML_SNAPSHOT = "html_snapshot"
    AI_DECISION = "ai_decision"
    AI_REASONING = "ai_reasoning"
    GOAL_START = "goal_start"
    GOAL_END = "goal_end"
    ERROR = "error"
    RECOVERY = "recovery"
    VERIFICATION = "verification"
    CUSTOM = "custom"


@dataclass(slots=True)
class ActionRecord:
    """A single recorded action."""

    type: RecordType
    timestamp: float = field(default_factory=time.time)
    url: str = ""
    selector: str | None = None
    value: str | None = None
    goal: str | None = None
    success: bool = True
    duration_ms: int = 0
    screenshot_path: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "timestamp": self.timestamp,
            "url": self.url,
            "selector": self.selector,
            "value": self.value,
            "goal": self.goal,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "screenshot_path": self.screenshot_path,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionRecord":
        return cls(
            type=RecordType(data.get("type", "custom")),
            timestamp=data.get("timestamp", 0),
            url=data.get("url", ""),
            selector=data.get("selector"),
            value=data.get("value"),
            goal=data.get("goal"),
            success=data.get("success", True),
            duration_ms=data.get("duration_ms", 0),
            screenshot_path=data.get("screenshot_path"),
            data=data.get("data", {}),
        )



class ActionRecorder:
    """Records all actions for one account within a run.

    Actions are buffered in memory and periodically flushed to replay.json.
    """

    def __init__(self, replay_path: Path) -> None:
        self.replay_path = replay_path
        self._records: list[ActionRecord] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self.replay_path.exists():
            try:
                data = json.loads(self.replay_path.read_text())
                self._records = [ActionRecord.from_dict(d) for d in data]
            except (json.JSONDecodeError, KeyError):
                self._records = []
        self._loaded = True

    def record(self, action: ActionRecord) -> None:
        """Add an action to the ledger."""
        self._ensure_loaded()
        self._records.append(action)

    def record_navigate(self, url: str, duration_ms: int = 0) -> None:
        self.record(ActionRecord(
            type=RecordType.NAVIGATE, url=url, duration_ms=duration_ms,
        ))

    def record_click(
        self, selector: str, url: str = "", duration_ms: int = 0
    ) -> None:
        self.record(ActionRecord(
            type=RecordType.CLICK, selector=selector,
            url=url, duration_ms=duration_ms,
        ))

    def record_fill(
        self, selector: str, value: str, url: str = "", duration_ms: int = 0
    ) -> None:
        self.record(ActionRecord(
            type=RecordType.FILL, selector=selector,
            value=value, url=url, duration_ms=duration_ms,
        ))

    def record_wait(
        self, condition: str, resolved: bool, duration_ms: int = 0
    ) -> None:
        self.record(ActionRecord(
            type=RecordType.WAIT, success=resolved,
            duration_ms=duration_ms, data={"condition": condition},
        ))

    def record_ai_decision(
        self, goal: str, plan: dict[str, Any], confidence: float = 0.0
    ) -> None:
        self.record(ActionRecord(
            type=RecordType.AI_DECISION, goal=goal,
            data={"plan": plan, "confidence": confidence},
        ))

    def record_error(
        self, error: str, goal: str | None = None, url: str = ""
    ) -> None:
        self.record(ActionRecord(
            type=RecordType.ERROR, goal=goal, url=url,
            success=False, data={"error": error},
        ))

    def record_goal_start(self, goal: str, index: int = 0) -> None:
        self.record(ActionRecord(
            type=RecordType.GOAL_START, goal=goal,
            data={"index": index},
        ))

    def record_goal_end(
        self, goal: str, success: bool, duration_ms: int = 0
    ) -> None:
        self.record(ActionRecord(
            type=RecordType.GOAL_END, goal=goal,
            success=success, duration_ms=duration_ms,
        ))

    def flush(self) -> None:
        """Write all records to replay.json."""
        self._ensure_loaded()
        self.replay_path.parent.mkdir(parents=True, exist_ok=True)
        self.replay_path.write_text(
            json.dumps([r.to_dict() for r in self._records], indent=2, default=str)
        )

    @property
    def records(self) -> list[ActionRecord]:
        self._ensure_loaded()
        return list(self._records)

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._records)
