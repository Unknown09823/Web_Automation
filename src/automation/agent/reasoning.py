"""Structured reasoning log: the human-readable "why" of every step.

Every step the agent runs produces one :class:`ReasoningEntry` with five
fixed sections — Goal, Observation, Reasoning, Action, Verification — that
mirror the Observe-Think-Act-Verify loop. Two consumers depend on them:

  * The Telegram controller renders them as a *reasoning panel* the user
    can pull up on demand for any active or completed run.
  * Operators tailing the API / dashboard see the same structure live.

Entries are append-only, JSONL-encoded, and bounded per account so a
long run cannot blow up memory or disk. The file lives next to the
existing ``replay.json`` and ``checkpoints.json`` inside each account
sub-directory of a run, so one run's reasoning is fully self-contained.

Design choices
==============

* No external dependencies. The log is a thin wrapper over JSONL so it
  plays nicely with ``events.jsonl`` and the SSE event stream.
* Fail-soft. A disk error never raises into the agent loop; the entry
  is dropped and a warning is logged. The agent must be more reliable
  than its observability.
* Source-agnostic. The deterministic engine, AI brain, and rule engine
  all produce the same shape so the reader doesn't need to switch
  modes between deterministic and AI executions.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


class DecisionSource(str, Enum):
    """Which level of the deterministic-first stack produced the step.

    Ordered from cheapest/fastest to last-resort. The Telegram reasoning
    panel surfaces this verbatim so operators can see *why* a step ran
    without AI even when AI would have been a fallback.
    """

    REPLAY = "replay"            # learned site template
    SITE_MEMORY = "site_memory"  # remembered selector for this host
    HEURISTIC = "heuristic"      # semantic element detection
    RULE = "rule"                # IF/THEN rule fired
    AI = "ai"                    # AIBrain plan
    HUMAN = "human"              # marked for human intervention
    MIXED = "mixed"              # multiple sources combined within step


@dataclass(slots=True)
class ReasoningEntry:
    """One step's worth of reasoning, structured for human review.

    The five sections match the loop:

    * ``goal`` — what we are trying to accomplish at this step.
    * ``observation`` — what the perceiver saw on the page (URL, signature,
      visible buttons, popups detected, page-state flags).
    * ``reasoning`` — why the agent chose this approach (which level of
      the stack fired, confidence score, available alternatives).
    * ``action`` — what was actually executed (action type, selector,
      target value redacted for secrets).
    * ``verification`` — how success was checked and the result.

    Optional ``source`` and ``confidence`` make filtering / threshold-
    based dashboards trivial.
    """

    run_id: str
    account_id: str
    goal_index: int
    goal: str
    observation: str = ""
    reasoning: str = ""
    action: str = ""
    verification: str = ""
    source: DecisionSource = DecisionSource.HEURISTIC
    confidence: float = 0.0
    alternatives: list[str] = field(default_factory=list)
    duration_ms: int = 0
    success: bool = True
    error: str | None = None
    timestamp: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "account_id": self.account_id,
            "goal_index": self.goal_index,
            "goal": self.goal,
            "observation": self.observation,
            "reasoning": self.reasoning,
            "action": self.action,
            "verification": self.verification,
            "source": self.source.value,
            "confidence": self.confidence,
            "alternatives": list(self.alternatives),
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
            "timestamp": self.timestamp,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReasoningEntry":
        try:
            source = DecisionSource(data.get("source") or "heuristic")
        except ValueError:
            source = DecisionSource.HEURISTIC
        return cls(
            run_id=str(data.get("run_id", "")),
            account_id=str(data.get("account_id", "")),
            goal_index=int(data.get("goal_index", 0)),
            goal=str(data.get("goal", "")),
            observation=str(data.get("observation", "")),
            reasoning=str(data.get("reasoning", "")),
            action=str(data.get("action", "")),
            verification=str(data.get("verification", "")),
            source=source,
            confidence=float(data.get("confidence", 0.0) or 0.0),
            alternatives=list(data.get("alternatives") or []),
            duration_ms=int(data.get("duration_ms", 0) or 0),
            success=bool(data.get("success", True)),
            error=data.get("error"),
            timestamp=float(data.get("timestamp", time.time())),
            extra=dict(data.get("extra") or {}),
        )

    def render(self, *, max_lines: int = 12) -> str:
        """Render as a five-section block suitable for Telegram or logs."""
        confidence = f"{self.confidence:.2f}" if self.confidence else "n/a"
        lines = [
            f"Goal #{self.goal_index}: {self.goal}",
            f"Observation: {_truncate(self.observation)}",
            f"Reasoning:   {_truncate(self.reasoning)} "
            f"[{self.source.value} conf={confidence}]",
            f"Action:      {_truncate(self.action)}",
            f"Verification: {_truncate(self.verification)} "
            f"({'pass' if self.success else 'fail'})",
        ]
        if self.error:
            lines.append(f"Error: {_truncate(self.error)}")
        return "\n".join(lines[:max_lines])


_MAX_TEXT = 240


def _truncate(text: str, n: int = _MAX_TEXT) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


class ReasoningLog:
    """Append-only JSONL log of reasoning entries scoped to one account.

    Per account the log lives at ``<run>/<account_id>/reasoning.jsonl``.
    The class is *not* thread-safe by design — each account is driven by
    its own coroutine and never written to from two places at once.

    File-format choice: JSONL rather than a single JSON array so a crash
    mid-run leaves a recoverable, line-oriented file the dashboard can
    tail without parsing the whole thing.
    """

    def __init__(self, path: Path | str, *, max_entries: int = 5000) -> None:
        self.path = Path(path)
        self.max_entries = int(max_entries)
        self._buffer: list[ReasoningEntry] = []
        self._count = 0

    # ----------------------------------------------------------- mutation
    def append(self, entry: ReasoningEntry) -> None:
        """Append an entry, both to the in-memory buffer and to disk.

        Disk failures never raise — the entry is kept in the buffer so a
        subsequent call to :py:meth:`flush` can retry. Operators get a
        warning via the standard logger.
        """
        self._buffer.append(entry)
        self._count += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), default=str) + "\n")
        except OSError:  # noqa: BLE001
            log.warning("reasoning log write failed: %s", self.path, exc_info=True)
        # Trim in-memory buffer; the on-disk file remains the source of truth.
        if len(self._buffer) > self.max_entries:
            self._buffer = self._buffer[-self.max_entries:]

    def flush(self) -> None:
        """Force-rewrite the on-disk log from the in-memory buffer.

        Used after a failed append to recover; safe to call at any time.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                for entry in self._buffer:
                    fh.write(json.dumps(entry.to_dict(), default=str) + "\n")
        except OSError:  # noqa: BLE001
            log.warning("reasoning log flush failed: %s", self.path, exc_info=True)

    # --------------------------------------------------------------- read
    def tail(self, n: int = 10) -> list[ReasoningEntry]:
        """Return the most recent ``n`` entries, newest first."""
        entries = self._load_disk_or_buffer()
        return entries[-n:][::-1]

    def all(self) -> list[ReasoningEntry]:
        """Return every entry in chronological order (oldest first)."""
        return self._load_disk_or_buffer()

    def render_panel(self, n: int = 5) -> str:
        """Render the last ``n`` entries as a Telegram-friendly panel."""
        entries = self.tail(n)
        if not entries:
            return "(no reasoning yet)"
        # Tail returned newest-first; flip back so the panel reads in time order
        # which is how operators actually want to follow execution.
        blocks = [entry.render() for entry in reversed(entries)]
        return "\n\n".join(blocks)

    # ---------------------------------------------------------- internals
    def _load_disk_or_buffer(self) -> list[ReasoningEntry]:
        """Prefer on-disk log (authoritative); fall back to buffer."""
        if self.path.exists():
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            entries: list[ReasoningEntry] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(ReasoningEntry.from_dict(json.loads(line)))
                except json.JSONDecodeError:
                    continue
            return entries
        return list(self._buffer)

    @property
    def total_appended(self) -> int:
        """Total entries appended via this instance — including evicted ones."""
        return self._count


def merge_logs(logs: Iterable[ReasoningLog], *, n: int = 50) -> list[ReasoningEntry]:
    """Merge several per-account logs into one chronologically-ordered list.

    Useful for run-level summaries when several accounts shared a run.
    """
    merged: list[ReasoningEntry] = []
    for ll in logs:
        merged.extend(ll.all())
    merged.sort(key=lambda e: e.timestamp)
    return merged[-n:]
