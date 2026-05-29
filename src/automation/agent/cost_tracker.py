"""AI cost tracking — token counter, call counter, template replay counter.

Every time the framework invokes AI (brain.decide, brain.run, or any
LLM-backed helper), the cost tracker increments counters. Every time a
template replay successfully avoids an AI call, the replay counter goes
up. The difference between the two tells operators exactly how much the
learn-once/replay-many architecture is saving.

The tracker is designed to be:

  * **Thread-safe** — counters use simple int additions under the GIL
    (no locks needed for CPython's single-writer pattern).
  * **Persistent** — flush() writes a JSON snapshot to disk so stats
    survive restarts. Loaded on init if the file exists.
  * **Singleton-friendly** — one instance per process, shared across
    all accounts and runs.
  * **API-ready** — ``to_dict()`` returns the shape the ``/ai/status``
    endpoint and the Telegram ``/stats`` command expect.

Counters tracked
================

  * ``ai_calls`` — total invocations of the AI brain (any model call)
  * ``ai_calls_by_goal`` — breakdown by goal name
  * ``tokens_input`` — estimated input tokens (prompt)
  * ``tokens_output`` — estimated output tokens (completion)
  * ``template_replays`` — goals resolved via template replay (no AI)
  * ``heuristic_hits`` — goals resolved via heuristic detection (no AI)
  * ``site_memory_hits`` — goals resolved via cached selectors (no AI)
  * ``rule_hits`` — goals resolved via rule engine (no AI)
  * ``human_handoffs`` — goals escalated to human intervention
  * ``recovery_attempts`` — total recovery invocations
  * ``recovery_successes`` — recoveries that actually worked

The tracker also computes derived metrics:
  * ``total_tokens`` = input + output
  * ``ai_savings_pct`` = replays / (replays + ai_calls) * 100
  * ``estimated_cost_usd`` = tokens * price_per_token (configurable)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Default token pricing (GPT-4o-mini ballpark as of 2025)
_DEFAULT_INPUT_PRICE_PER_1K = 0.00015   # $0.15 per 1M input tokens
_DEFAULT_OUTPUT_PRICE_PER_1K = 0.0006   # $0.60 per 1M output tokens


@dataclass
class CostTracker:
    """Global AI cost and efficiency tracker."""

    # --- counters ---
    ai_calls: int = 0
    ai_calls_by_goal: dict[str, int] = field(default_factory=dict)
    tokens_input: int = 0
    tokens_output: int = 0
    template_replays: int = 0
    heuristic_hits: int = 0
    site_memory_hits: int = 0
    rule_hits: int = 0
    human_handoffs: int = 0
    recovery_attempts: int = 0
    recovery_successes: int = 0

    # --- config ---
    input_price_per_1k: float = _DEFAULT_INPUT_PRICE_PER_1K
    output_price_per_1k: float = _DEFAULT_OUTPUT_PRICE_PER_1K
    persist_path: Path | None = None

    # --- timing ---
    started_at: float = field(default_factory=time.time)
    last_flush: float = 0.0

    # ---------------------------------------------------------- record
    def record_ai_call(
        self,
        *,
        goal: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Record one AI brain invocation."""
        self.ai_calls += 1
        if goal:
            self.ai_calls_by_goal[goal] = self.ai_calls_by_goal.get(goal, 0) + 1
        self.tokens_input += max(0, input_tokens)
        self.tokens_output += max(0, output_tokens)

    def record_template_replay(self, goal: str = "") -> None:
        """Record a goal resolved via template replay (no AI needed)."""
        self.template_replays += 1

    def record_heuristic_hit(self, goal: str = "") -> None:
        """Record a goal resolved via heuristic detection."""
        self.heuristic_hits += 1

    def record_site_memory_hit(self, goal: str = "") -> None:
        """Record a goal resolved via cached site memory."""
        self.site_memory_hits += 1

    def record_rule_hit(self, goal: str = "") -> None:
        """Record a goal resolved via rule engine."""
        self.rule_hits += 1

    def record_human_handoff(self, goal: str = "") -> None:
        """Record a goal escalated to human intervention."""
        self.human_handoffs += 1

    def record_recovery(self, *, success: bool) -> None:
        """Record a recovery attempt and its outcome."""
        self.recovery_attempts += 1
        if success:
            self.recovery_successes += 1

    # ---------------------------------------------------------- derived
    @property
    def total_tokens(self) -> int:
        return self.tokens_input + self.tokens_output

    @property
    def total_deterministic(self) -> int:
        """Total goals resolved without AI."""
        return (
            self.template_replays
            + self.heuristic_hits
            + self.site_memory_hits
            + self.rule_hits
        )

    @property
    def ai_savings_pct(self) -> float:
        """Percentage of goals resolved without AI."""
        total = self.total_deterministic + self.ai_calls
        if total <= 0:
            return 0.0
        return (self.total_deterministic / total) * 100.0

    @property
    def estimated_cost_usd(self) -> float:
        """Estimated cost based on token counts and configured pricing."""
        input_cost = (self.tokens_input / 1000) * self.input_price_per_1k
        output_cost = (self.tokens_output / 1000) * self.output_price_per_1k
        return round(input_cost + output_cost, 4)

    @property
    def recovery_rate(self) -> float:
        if self.recovery_attempts <= 0:
            return 0.0
        return (self.recovery_successes / self.recovery_attempts) * 100.0

    # ---------------------------------------------------------- serialize
    def to_dict(self) -> dict[str, Any]:
        """Return the full stats dict for the API / Telegram."""
        return {
            "enabled": True,
            "ai_calls": self.ai_calls,
            "ai_calls_by_goal": dict(self.ai_calls_by_goal),
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "total_tokens": self.total_tokens,
            "template_replays": self.template_replays,
            "heuristic_hits": self.heuristic_hits,
            "site_memory_hits": self.site_memory_hits,
            "rule_hits": self.rule_hits,
            "total_deterministic": self.total_deterministic,
            "human_handoffs": self.human_handoffs,
            "recovery_attempts": self.recovery_attempts,
            "recovery_successes": self.recovery_successes,
            "recovery_rate": round(self.recovery_rate, 1),
            "ai_savings_pct": round(self.ai_savings_pct, 1),
            "estimated_cost_usd": self.estimated_cost_usd,
            "uptime_seconds": int(time.time() - self.started_at),
        }

    # ---------------------------------------------------------- persist
    def flush(self) -> bool:
        """Write current counters to disk. Returns True on success."""
        if self.persist_path is None:
            return False
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            self.persist_path.write_text(
                json.dumps(self.to_dict(), indent=2, default=str)
            )
            self.last_flush = time.time()
            return True
        except OSError:  # noqa: BLE001
            log.warning("cost_tracker flush failed", exc_info=True)
            return False

    def load(self) -> bool:
        """Load counters from disk if the file exists."""
        if self.persist_path is None or not self.persist_path.exists():
            return False
        try:
            data = json.loads(self.persist_path.read_text())
            self.ai_calls = int(data.get("ai_calls", 0))
            self.ai_calls_by_goal = dict(data.get("ai_calls_by_goal") or {})
            self.tokens_input = int(data.get("tokens_input", 0))
            self.tokens_output = int(data.get("tokens_output", 0))
            self.template_replays = int(data.get("template_replays", 0))
            self.heuristic_hits = int(data.get("heuristic_hits", 0))
            self.site_memory_hits = int(data.get("site_memory_hits", 0))
            self.rule_hits = int(data.get("rule_hits", 0))
            self.human_handoffs = int(data.get("human_handoffs", 0))
            self.recovery_attempts = int(data.get("recovery_attempts", 0))
            self.recovery_successes = int(data.get("recovery_successes", 0))
            return True
        except (OSError, json.JSONDecodeError, ValueError):  # noqa: BLE001
            log.warning("cost_tracker load failed", exc_info=True)
            return False

    def reset(self) -> None:
        """Reset all counters to zero."""
        self.ai_calls = 0
        self.ai_calls_by_goal.clear()
        self.tokens_input = 0
        self.tokens_output = 0
        self.template_replays = 0
        self.heuristic_hits = 0
        self.site_memory_hits = 0
        self.rule_hits = 0
        self.human_handoffs = 0
        self.recovery_attempts = 0
        self.recovery_successes = 0
        self.started_at = time.time()
