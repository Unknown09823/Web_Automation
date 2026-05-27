"""Self-healing layer.

When a plan step fails, the healer:
  1. Reinspects the page (fresh perception).
  2. Looks up successful recoveries from memory for the page signature.
  3. Tries alternate selectors, role-based queries, and visual fallbacks.
  4. Records the outcome so future failures heal faster.

The healer is best-effort and never raises — callers can always rely on a
``HealResult`` value.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from automation.ai.executor import ActionExecutor, PlanResult, StepResult
from automation.ai.perception import PagePerception, PageSnapshot
from automation.ai.planner import ActionPlanner, ActionStep, ActionType

log = logging.getLogger(__name__)


@dataclass(slots=True)
class HealResult:
    healed: bool
    attempts: int = 0
    notes: list[str] = field(default_factory=list)
    new_step: ActionStep | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "healed": self.healed,
            "attempts": self.attempts,
            "notes": self.notes,
            "new_step": self.new_step.to_dict() if self.new_step else None,
        }


class SelfHealer:
    """Try increasingly creative ways to satisfy a failed step."""

    def __init__(
        self,
        perception: PagePerception,
        planner: ActionPlanner,
        executor: ActionExecutor,
        memory: Any | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.perception = perception
        self.planner = planner
        self.executor = executor
        self.memory = memory
        self.max_attempts = max_attempts

    async def heal(
        self,
        page: Any,
        failed: StepResult,
        snapshot: PageSnapshot,
    ) -> HealResult:
        result = HealResult(healed=False)
        intent = failed.step.intent
        if not intent:
            result.notes.append("no intent on failed step; cannot heal")
            return result

        # 1. memory-driven recovery
        if self.memory is not None:
            try:
                strategies = await self.memory.best_recoveries(
                    snapshot.signature, f"{failed.step.action.value}:{intent}"
                )
                for strat in strategies[: self.max_attempts]:
                    result.attempts += 1
                    new_step = self._step_with_selector(failed.step, strat)
                    sr = await self.executor._run_step(page, new_step)  # noqa: SLF001
                    if sr.success:
                        result.healed = True
                        result.new_step = new_step
                        result.notes.append(f"healed via memory selector: {strat}")
                        await self.memory.remember_recovery(
                            snapshot.signature, f"{failed.step.action.value}:{intent}",
                            strat, success=True,
                        )
                        return result
                    await self.memory.remember_recovery(
                        snapshot.signature, f"{failed.step.action.value}:{intent}",
                        strat, success=False,
                    )
            except Exception:  # noqa: BLE001
                log.exception("healer: memory path failed")

        # 2. role-based fallback (Playwright supports this even without selectors)
        result.attempts += 1
        try:
            role_step = ActionStep(
                action=failed.step.action,
                intent=intent,
                selector=None,
                value=failed.step.value,
                confidence=failed.step.confidence * 0.8,
                rationale="role-based fallback",
                timeout_ms=failed.step.timeout_ms,
            )
            sr = await self.executor._run_step(page, role_step)  # noqa: SLF001
            if sr.success:
                result.healed = True
                result.new_step = role_step
                result.notes.append("healed via role-based fallback")
                return result
        except Exception:  # noqa: BLE001
            log.exception("healer: role fallback failed")

        # 3. fresh perception + alternative selectors
        try:
            fresh = await self.perception.capture_from_page(page)
            for el in fresh.by_intent(intent):
                if el.selector == failed.step.selector:
                    continue
                result.attempts += 1
                if result.attempts > self.max_attempts:
                    break
                alt = self._step_with_selector(failed.step, el.selector)
                sr = await self.executor._run_step(page, alt)  # noqa: SLF001
                if sr.success:
                    result.healed = True
                    result.new_step = alt
                    result.notes.append(f"healed via reinspection selector: {el.selector}")
                    if self.memory is not None:
                        try:
                            await self.memory.remember_selector(
                                fresh.signature, intent, el.selector, "css", success=True,
                            )
                        except Exception:  # noqa: BLE001
                            log.exception("healer: memory remember failed")
                    return result
        except Exception:  # noqa: BLE001
            log.exception("healer: reinspection failed")

        result.notes.append("no recovery succeeded")
        return result

    @staticmethod
    def _step_with_selector(step: ActionStep, selector: str) -> ActionStep:
        return ActionStep(
            action=step.action,
            intent=step.intent,
            selector=selector,
            value=step.value,
            confidence=step.confidence * 0.7,
            rationale=f"healer alternative selector",
            timeout_ms=step.timeout_ms,
            optional=step.optional,
            metadata={**step.metadata, "healed": True},
        )
