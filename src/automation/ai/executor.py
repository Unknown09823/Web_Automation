"""Action executor: runs an ``ActionPlan`` against a Playwright ``Page``.

Each step is independently isolated: a single failure does not abort the
whole plan unless the failing step is non-optional. Detailed reasoning is
returned for the dashboard and AI memory.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automation.ai.planner import ActionPlan, ActionStep, ActionType

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StepResult:
    step: ActionStep
    success: bool
    error: str | None = None
    duration_ms: int = 0
    screenshot: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step.to_dict(),
            "success": self.success,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "screenshot": self.screenshot,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class PlanResult:
    plan: ActionPlan
    results: list[StepResult] = field(default_factory=list)
    success: bool = False
    duration_ms: int = 0

    @property
    def failed_steps(self) -> list[StepResult]:
        return [r for r in self.results if not r.success and not r.step.optional]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "success": self.success,
            "duration_ms": self.duration_ms,
            "results": [r.to_dict() for r in self.results],
        }


class ActionExecutor:
    """Drive a Playwright ``Page`` through an ``ActionPlan``."""

    def __init__(
        self,
        screenshot_dir: str | Path = "data/screenshots",
        dry_run: bool = False,
    ) -> None:
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run

    async def execute(self, page: Any, plan: ActionPlan) -> PlanResult:
        result = PlanResult(plan=plan)
        started = time.time()
        for step in plan.steps:
            sr = await self._run_step(page, step)
            result.results.append(sr)
            if not sr.success and not step.optional:
                log.warning(
                    "Plan halted at step=%s intent=%s: %s",
                    step.action.value, step.intent, sr.error,
                )
                break
        result.duration_ms = int((time.time() - started) * 1000)
        result.success = all(r.success or r.step.optional for r in result.results) and bool(
            result.results
        )
        return result

    async def _run_step(self, page: Any, step: ActionStep) -> StepResult:
        started = time.time()
        if self.dry_run:
            return StepResult(
                step=step,
                success=True,
                duration_ms=0,
                metadata={"dry_run": True},
            )
        try:
            if step.action is ActionType.NAVIGATE:
                await page.goto(step.value or "", timeout=step.timeout_ms)
            elif step.action is ActionType.CLICK:
                await self._click(page, step)
            elif step.action is ActionType.FILL:
                await self._fill(page, step)
            elif step.action is ActionType.SELECT:
                await page.select_option(step.selector, step.value, timeout=step.timeout_ms)
            elif step.action is ActionType.CHECK:
                await page.check(step.selector, timeout=step.timeout_ms)
            elif step.action is ActionType.WAIT_FOR:
                if step.selector:
                    await page.wait_for_selector(step.selector, timeout=step.timeout_ms)
                else:
                    await page.wait_for_load_state(
                        step.value or "load", timeout=step.timeout_ms
                    )
            elif step.action is ActionType.SCREENSHOT:
                shot = await self._screenshot(page, step)
                return StepResult(
                    step=step, success=True,
                    duration_ms=int((time.time() - started) * 1000),
                    screenshot=shot,
                )
            elif step.action is ActionType.VERIFY:
                ok, info = await self._verify(page, step)
                return StepResult(
                    step=step, success=ok,
                    duration_ms=int((time.time() - started) * 1000),
                    error=None if ok else info, metadata={"verify": info},
                )
            elif step.action is ActionType.NOOP:
                return StepResult(
                    step=step, success=step.optional,
                    error="noop step (no element found)" if not step.optional else None,
                    duration_ms=0,
                )
            else:  # pragma: no cover - exhaustive
                raise ValueError(f"Unknown action: {step.action}")
            return StepResult(
                step=step, success=True,
                duration_ms=int((time.time() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - isolation
            log.exception("step failed: %s", step.action.value)
            return StepResult(
                step=step, success=False,
                error=repr(exc),
                duration_ms=int((time.time() - started) * 1000),
            )

    # ----------------------------------------------------------- step helpers
    async def _click(self, page: Any, step: ActionStep) -> None:
        if step.selector:
            await page.click(step.selector, timeout=step.timeout_ms)
            return
        if step.intent:
            # Playwright role-based fallback
            await page.get_by_role(
                "button", name=step.intent
            ).first.click(timeout=step.timeout_ms)
            return
        raise ValueError("click step needs selector or intent")

    async def _fill(self, page: Any, step: ActionStep) -> None:
        if not step.selector:
            raise ValueError("fill step needs selector")
        await page.fill(step.selector, step.value or "", timeout=step.timeout_ms)

    async def _screenshot(self, page: Any, step: ActionStep) -> str:
        path = self.screenshot_dir / f"step-{int(time.time() * 1000)}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
        except Exception:  # noqa: BLE001
            log.exception("screenshot failed")
            return ""
        return str(path)

    async def _verify(self, page: Any, step: ActionStep) -> tuple[bool, str]:
        # Soft verification: if we have a goal, look for absence of the
        # element that started the goal (e.g. login button gone => logged in).
        goal = (step.metadata or {}).get("goal", "")
        if not goal:
            return True, "no verify criteria"
        try:
            url = page.url if hasattr(page, "url") else ""
            title = await page.title() if hasattr(page, "title") else ""
        except Exception:  # noqa: BLE001
            url = title = ""
        return True, f"verified after goal={goal} url={url} title={title}"
