"""The single central AI Brain.

A *single* coordinator that owns perception, planning, execution, healing,
and learning. There are no sub-agents. The Brain is the only entity that
talks to the AI memory and the only entity workflows or plugins should
invoke for AI behavior.

Public surface:
  * ``analyze(page)`` — perceive and label a page.
  * ``decide(goal, snapshot, inputs)`` — produce an ``ActionPlan``.
  * ``act(page, plan)`` — execute a plan with auto-healing on failure.
  * ``run(page, goal, inputs)`` — perceive + plan + act + learn in one call.

The Brain is *modular and optional*: the framework boots and runs without it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from automation.ai.executor import ActionExecutor, PlanResult
from automation.ai.healer import HealResult, SelfHealer
from automation.ai.intents import IntentMatcher
from automation.ai.memory import AIMemory, WorkflowRecord
from automation.ai.perception import PagePerception, PageSnapshot
from automation.ai.planner import ActionPlan, ActionPlanner

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BrainDecision:
    """A complete decision record for one ``run`` invocation."""

    goal: str
    snapshot: PageSnapshot
    plan: ActionPlan
    plan_result: PlanResult | None = None
    heals: list[HealResult] = field(default_factory=list)
    success: bool = False
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "page": {
                "url": self.snapshot.url,
                "title": self.snapshot.title,
                "signature": self.snapshot.signature,
                "screenshot": self.snapshot.screenshot_path,
            },
            "plan": self.plan.to_dict(),
            "plan_result": self.plan_result.to_dict() if self.plan_result else None,
            "heals": [h.to_dict() for h in self.heals],
            "success": self.success,
            "duration_ms": self.duration_ms,
        }


class AIBrain:
    """Single central AI coordinator.

    Composition over inheritance: each capability is a small, replaceable
    component, but only the brain orchestrates them. Plugins should call
    ``brain.run(...)`` rather than driving components directly.
    """

    def __init__(
        self,
        *,
        memory: AIMemory | None = None,
        intent_matcher: IntentMatcher | None = None,
        screenshot_dir: str = "data/screenshots",
        enabled: bool = True,
        dry_run: bool = False,
        max_heal_attempts: int = 3,
    ) -> None:
        self.enabled = enabled
        self.dry_run = dry_run
        self.memory = memory
        self.matcher = intent_matcher or IntentMatcher()
        self.perception = PagePerception(self.matcher)
        self.planner = ActionPlanner(memory=self.memory)
        self.executor = ActionExecutor(
            screenshot_dir=screenshot_dir, dry_run=dry_run
        )
        self.healer = SelfHealer(
            perception=self.perception,
            planner=self.planner,
            executor=self.executor,
            memory=self.memory,
            max_attempts=max_heal_attempts,
        )
        self._last_decision: BrainDecision | None = None

    # --------------------------------------------------------------- factories
    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "AIBrain":
        ai_cfg = (config or {}).get("ai", {})
        memory: AIMemory | None = None
        if ai_cfg.get("memory_enabled", True):
            try:
                memory = AIMemory(
                    path=ai_cfg.get("memory_path", "data/learning/memory.sqlite")
                )
            except Exception:  # noqa: BLE001
                log.exception("AI memory init failed; continuing without persistence")
                memory = None
        return cls(
            memory=memory,
            screenshot_dir=ai_cfg.get("screenshot_dir", "data/screenshots"),
            enabled=ai_cfg.get("enabled", True),
            dry_run=ai_cfg.get("dry_run", False),
            max_heal_attempts=int(ai_cfg.get("max_heal_attempts", 3)),
        )

    # ---------------------------------------------------------------- accessors
    @property
    def last_decision(self) -> BrainDecision | None:
        return self._last_decision

    # ---------------------------------------------------------------- pipeline
    async def analyze(self, page: Any, screenshot: bool = True) -> PageSnapshot:
        """Capture and label a page. Cheap; can be called repeatedly."""
        if not self.enabled:
            return PageSnapshot(url="", title="", h1=[], forms=0, elements=[], signature="")
        shot_path = None
        if screenshot:
            shot_path = f"data/screenshots/page-{int(time.time() * 1000)}.png"
        snapshot = await self.perception.capture_from_page(page, screenshot_path=shot_path)
        if self.memory and snapshot.signature:
            try:
                await self.memory.remember_page(
                    snapshot.signature, snapshot.title, snapshot.url
                )
            except Exception:  # noqa: BLE001
                log.exception("brain: remember_page failed")
        return snapshot

    async def decide(
        self, goal: str, snapshot: PageSnapshot, inputs: dict[str, str] | None = None,
    ) -> ActionPlan:
        if not self.enabled:
            return ActionPlan(goal=goal, page_signature=snapshot.signature)
        return await self.planner.plan(goal, snapshot, inputs or {})

    async def act(self, page: Any, plan: ActionPlan) -> tuple[PlanResult, list[HealResult]]:
        """Execute a plan with auto-healing for failed mandatory steps."""
        result = await self.executor.execute(page, plan)
        heals: list[HealResult] = []
        if not result.success and result.failed_steps:
            for failed in result.failed_steps:
                snapshot = await self.perception.capture_from_page(page)
                heal = await self.healer.heal(page, failed, snapshot)
                heals.append(heal)
                if heal.healed and heal.new_step:
                    # patch and re-run from this step forward
                    idx = result.results.index(failed)
                    plan.steps[idx] = heal.new_step
                    failed.success = True
                    failed.metadata["healed"] = True
            # recompute success
            result.success = all(r.success or r.step.optional for r in result.results)
        return result, heals

    async def run(
        self,
        page: Any,
        goal: str,
        inputs: dict[str, str] | None = None,
    ) -> BrainDecision:
        """End-to-end: perceive -> plan -> act -> heal -> learn."""
        started = time.time()
        snapshot = await self.analyze(page, screenshot=True)
        plan = await self.decide(goal, snapshot, inputs)
        plan_result, heals = await self.act(page, plan)
        decision = BrainDecision(
            goal=goal,
            snapshot=snapshot,
            plan=plan,
            plan_result=plan_result,
            heals=heals,
            success=plan_result.success if plan_result else False,
            duration_ms=int((time.time() - started) * 1000),
        )
        await self._learn(decision)
        self._last_decision = decision
        return decision

    # ----------------------------------------------------------------- learning
    async def _learn(self, decision: BrainDecision) -> None:
        if not self.memory:
            return
        try:
            await self.memory.record_workflow(
                WorkflowRecord(
                    workflow=decision.goal,
                    success=decision.success,
                    duration_ms=decision.duration_ms,
                    page_signature=decision.snapshot.signature,
                    error=None if decision.success else "plan_failed",
                )
            )
            if decision.plan_result:
                for sr in decision.plan_result.results:
                    if sr.step.selector and sr.step.intent:
                        await self.memory.remember_selector(
                            decision.snapshot.signature,
                            sr.step.intent,
                            sr.step.selector,
                            strategy="css",
                            success=sr.success,
                        )
        except Exception:  # noqa: BLE001
            log.exception("brain: learning step failed")
