"""Stacked error recovery.

When a goal step fails, the recovery stack tries progressively more
aggressive strategies before giving up:

  1. Retry with same action (transient failure)
  2. Try alternate selectors from AI perception
  3. Use the existing SelfHealer (memory + role fallback)
  4. Re-perceive page and replan from scratch
  5. Navigate back and retry the whole goal
  6. Mark as failed and continue to next goal

Each strategy is attempted in order; the first success wins.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RecoveryAttempt:
    """One recovery attempt record."""

    strategy: str
    success: bool
    duration_ms: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


@dataclass(slots=True)
class RecoveryResult:
    """Outcome of the recovery stack."""

    recovered: bool
    attempts: list[RecoveryAttempt] = field(default_factory=list)
    total_duration_ms: int = 0
    final_strategy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovered": self.recovered,
            "attempts": [a.to_dict() for a in self.attempts],
            "total_duration_ms": self.total_duration_ms,
            "final_strategy": self.final_strategy,
        }



class RecoveryStack:
    """Progressively try recovery strategies for a failed action.

    Wraps the existing SelfHealer and adds higher-level strategies
    (replan, navigate back, etc.) that the healer alone doesn't handle.
    """

    def __init__(
        self,
        brain: Any = None,
        *,
        max_retries: int = 2,
        max_total_attempts: int = 6,
    ) -> None:
        self.brain = brain
        self.max_retries = max_retries
        self.max_total_attempts = max_total_attempts

    async def recover(
        self,
        page: Any,
        *,
        goal: str = "",
        last_error: str = "",
        action_fn: Any = None,
    ) -> RecoveryResult:
        """Try the recovery stack for a failed goal step.

        ``action_fn`` is an async callable that re-attempts the failed action.
        If provided, strategies 1-2 call it after adjusting state.

        For deeper recovery (replan, navigate back), the agent's outer loop
        handles those at a higher level — this stack focuses on immediate
        tactical recovery.
        """
        result = RecoveryResult(recovered=False)
        started = time.time()

        strategies = [
            ("retry_immediate", self._retry_immediate),
            ("wait_and_retry", self._wait_and_retry),
            ("dismiss_overlay", self._dismiss_overlay),
            ("scroll_into_view", self._scroll_into_view),
            ("ai_replan", self._ai_replan),
        ]

        for name, strategy_fn in strategies:
            if len(result.attempts) >= self.max_total_attempts:
                break
            attempt_start = time.time()
            try:
                success = await strategy_fn(page, goal=goal, action_fn=action_fn)
                attempt = RecoveryAttempt(
                    strategy=name,
                    success=success,
                    duration_ms=int((time.time() - attempt_start) * 1000),
                    detail=f"goal={goal}",
                )
                result.attempts.append(attempt)
                if success:
                    result.recovered = True
                    result.final_strategy = name
                    break
            except Exception as exc:  # noqa: BLE001
                result.attempts.append(RecoveryAttempt(
                    strategy=name, success=False,
                    duration_ms=int((time.time() - attempt_start) * 1000),
                    detail=f"exception: {exc!r}",
                ))

        result.total_duration_ms = int((time.time() - started) * 1000)
        return result

    async def _retry_immediate(
        self, page: Any, *, goal: str = "", action_fn: Any = None
    ) -> bool:
        """Simple immediate retry — handles transient failures."""
        if action_fn is None:
            return False
        try:
            await action_fn()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _wait_and_retry(
        self, page: Any, *, goal: str = "", action_fn: Any = None
    ) -> bool:
        """Wait for page to settle, then retry."""
        import asyncio
        await asyncio.sleep(2.0)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        if action_fn is None:
            return False
        try:
            await action_fn()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _dismiss_overlay(
        self, page: Any, *, goal: str = "", action_fn: Any = None
    ) -> bool:
        """Try to dismiss popups/overlays that might be blocking."""
        dismiss_selectors = [
            'button[class*="close"]',
            '[aria-label="Close"]',
            '[class*="dismiss"]',
            '[class*="overlay"] button',
            'button:has-text("Close")',
            'button:has-text("No thanks")',
            'button:has-text("Not now")',
            '[class*="modal"] button[class*="close"]',
        ]
        dismissed = False
        for sel in dismiss_selectors:
            try:
                if await page.is_visible(sel):
                    await page.click(sel, timeout=3000)
                    dismissed = True
                    break
            except Exception:  # noqa: BLE001
                continue

        if not dismissed:
            # Try pressing Escape
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass

        if action_fn is None:
            return dismissed
        import asyncio
        await asyncio.sleep(0.5)
        try:
            await action_fn()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _scroll_into_view(
        self, page: Any, *, goal: str = "", action_fn: Any = None
    ) -> bool:
        """Scroll page to potentially reveal hidden elements."""
        try:
            await page.evaluate("window.scrollBy(0, 300)")
        except Exception:  # noqa: BLE001
            pass
        import asyncio
        await asyncio.sleep(0.5)
        if action_fn is None:
            return False
        try:
            await action_fn()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _ai_replan(
        self, page: Any, *, goal: str = "", action_fn: Any = None
    ) -> bool:
        """Use the AI brain to re-perceive and attempt the goal fresh."""
        if not self.brain:
            return False
        try:
            decision = await self.brain.run(page, goal=goal)
            return decision.success
        except Exception:  # noqa: BLE001
            return False
