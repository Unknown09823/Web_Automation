"""The Observe → Think → Act → Verify → Learn loop.

The user's brief asked for an *explicit* loop pattern at every step,
not a hidden one. This module is that loop. For each goal:

::

    Observe   — perception + popup guard + URL/state snapshot
       │
       ▼
    Think     — DeterministicEngine.decide() picks a plan
       │      (rules → replay → site memory → heuristics → AI → human)
       ▼
    Act-pre   — apply pre_actions (popup dismiss, wait, navigate, ...)
       │
       ▼
    Act       — run each ActionStep with adaptive waits in between
       │
       ▼
    Verify    — SuccessVerifier on the post-action page state
       │
       ▼
    Learn     — feed counters back to engine + append reasoning entry

The loop lives in its own module so the BrowserAgent can stay focused
on run-level orchestration (accounts, sessions, checkpoints, run
folders) and delegate everything goal-level to one place.

Design constraints
==================

* **No exceptions leak out.** Browser failures, network blips, and
  malformed selectors are all converted to a structured
  :class:`StepLoopResult`. The agent's outer loop only deals with
  success / failure booleans.

* **Browser-agnostic.** The loop talks to the page via the small
  protocol :class:`_PageLike`; tests drive it with a fake page that
  satisfies the protocol but never touches Playwright.

* **No hidden sleeps.** Every wait goes through ``AdaptiveWaiter``
  (already in the framework) so the agent never burns 5s on a page
  that loaded in 800ms.

* **Single source of truth for "why".** Every loop produces exactly
  one :class:`ReasoningEntry` summarising Goal/Observation/Reasoning/
  Action/Verification. The Telegram bot, the API, and the dashboard
  all read from that one log.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Protocol

from automation.agent.deterministic_engine import (
    DecisionPlan,
    DeterministicEngine,
)
from automation.agent.popup_guard import PopupGuard, PopupGuardResult
from automation.agent.reasoning import (
    DecisionSource,
    ReasoningEntry,
    ReasoningLog,
)
from automation.agent.rule_engine import (
    ActionKind as RuleActionKind,
    Observation,
    TriggeredAction,
)
from automation.agent.verifier import SuccessVerifier, VerificationResult
from automation.agent.waiter import AdaptiveWaiter
from automation.ai.executor import ActionExecutor, StepResult
from automation.ai.perception import PagePerception, PageSnapshot
from automation.ai.planner import ActionStep, ActionType

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- protocol
class _PageLike(Protocol):
    """The minimal slice of a Playwright Page the loop touches."""

    @property
    def url(self) -> str: ...
    async def title(self) -> str: ...
    async def goto(self, url: str, *, timeout: int = ...) -> Any: ...
    async def evaluate(self, expr: str) -> Any: ...
    async def is_visible(self, selector: str, *, timeout: int | None = ...) -> bool: ...
    async def click(self, selector: str, *, timeout: int = ...) -> Any: ...
    @property
    def keyboard(self) -> Any: ...


# ---------------------------------------------------------------- result
@dataclass(slots=True)
class StepLoopResult:
    """Outcome of one ``execute_goal`` call."""

    goal: str
    success: bool
    duration_ms: int
    steps_attempted: int
    steps_succeeded: int
    plan: DecisionPlan
    verification: VerificationResult | None
    reasoning: ReasoningEntry
    popup_result: PopupGuardResult | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "steps_attempted": self.steps_attempted,
            "steps_succeeded": self.steps_succeeded,
            "source": self.plan.source.value,
            "confidence": self.plan.confidence,
            "rationale": self.plan.rationale,
            "verification": self.verification.to_dict() if self.verification else None,
            "popup_result": self.popup_result.to_dict() if self.popup_result else None,
            "reasoning": self.reasoning.to_dict(),
            "error": self.error,
        }


# ---------------------------------------------------------------- loop
class StepLoop:
    """Run one goal end-to-end on a single browser page.

    The class is reusable across goals; one instance per browser
    session is the typical pattern. It carries no state between
    invocations — every ``execute_goal`` is independent.
    """

    def __init__(
        self,
        *,
        perception: PagePerception | None = None,
        popup_guard: PopupGuard | None = None,
        deterministic_engine: DeterministicEngine,
        executor: ActionExecutor | None = None,
        verifier: SuccessVerifier | None = None,
        waiter: AdaptiveWaiter | None = None,
        reasoning_log: ReasoningLog | None = None,
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        # Tuning knobs — operators rarely change these.
        per_step_settle_ms: int = 1500,
        post_action_settle_ms: int = 6_000,
        max_pre_actions: int = 4,
    ) -> None:
        self.perception = perception or PagePerception()
        self.popup_guard = popup_guard or PopupGuard()
        self.engine = deterministic_engine
        self.executor = executor or ActionExecutor()
        self.verifier = verifier or SuccessVerifier()
        self.waiter = waiter or AdaptiveWaiter()
        self.reasoning_log = reasoning_log
        self.on_event = on_event
        self.per_step_settle_ms = int(per_step_settle_ms)
        self.post_action_settle_ms = int(post_action_settle_ms)
        self.max_pre_actions = int(max_pre_actions)

    # ----------------------------------------------------------- public API
    async def execute_goal(
        self,
        *,
        page: _PageLike,
        run_id: str,
        account_id: str,
        goal_name: str,
        goal_description: str = "",
        goal_index: int = 0,
        inputs: dict[str, str] | None = None,
        verification_hints: Iterable[str] | None = None,
        before_url: str | None = None,
        host: str = "",
        cancel_check: Callable[[], bool] | None = None,
    ) -> StepLoopResult:
        """Execute one goal through the full OTAV loop."""
        started = time.time()
        inputs = dict(inputs or {})
        hints = list(verification_hints or [])
        if cancel_check is None:
            cancel_check = lambda: False

        before_url_value = before_url or _safe_url(page)
        reasoning = ReasoningEntry(
            run_id=run_id,
            account_id=account_id,
            goal_index=goal_index,
            goal=goal_description or goal_name,
        )

        # ---------------------------------------------------- 1. Observe
        try:
            snapshot = await self.perception.capture_from_page(page)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                reasoning, plan=None, started=started,
                error=f"observe phase failed: {exc!r}",
                goal_name=goal_name,
            )

        await self._emit("agent.observe.started", {
            "run_id": run_id, "account_id": account_id,
            "goal": goal_name, "url": before_url_value,
            "page_signature": snapshot.signature,
        })

        popup_result: PopupGuardResult | None = None
        try:
            popup_result = await self.popup_guard.scan_and_dismiss(page)
        except Exception:  # noqa: BLE001
            log.debug("popup_guard.scan failed", exc_info=True)

        observation = _build_observation(
            snapshot=snapshot,
            popup_result=popup_result,
            url=before_url_value,
            page_text=await _safe_body_text(page),
        )

        reasoning.observation = _format_observation(snapshot, popup_result)

        # ---------------------------------------------------- 2. Think
        if cancel_check():
            return self._cancelled(reasoning, plan=None, started=started, goal_name=goal_name)

        plan = await self.engine.decide(
            goal=goal_name,
            page_url=before_url_value,
            page_signature=snapshot.signature,
            snapshot=snapshot,
            observation=observation,
            inputs=inputs,
            host=host,
        )
        reasoning.source = plan.source
        reasoning.confidence = plan.confidence
        reasoning.reasoning = plan.rationale
        reasoning.alternatives = [
            f"{a.element.tag}.{a.matched_on}={a.element.text[:30] or a.element.aria[:30]}"
            for a in plan.alternatives[:3]
        ]
        reasoning.extra["host"] = plan.host
        if plan.notes:
            reasoning.extra["notes"] = list(plan.notes)

        await self._emit("agent.think.completed", {
            "run_id": run_id, "account_id": account_id,
            "goal": goal_name, "source": plan.source.value,
            "confidence": plan.confidence, "rationale": plan.rationale,
            "pre_actions": [a.action.kind.value for a in plan.pre_actions],
        })

        # If the plan is HUMAN-handoff, short-circuit immediately.
        if plan.source is DecisionSource.HUMAN:
            reasoning.action = "human handoff"
            reasoning.verification = "n/a"
            reasoning.success = False
            reasoning.error = plan.rationale
            self._persist_reasoning(reasoning)
            self.engine.finalize(plan, success=False)
            return StepLoopResult(
                goal=goal_name, success=False,
                duration_ms=_elapsed_ms(started),
                steps_attempted=0, steps_succeeded=0,
                plan=plan, verification=None, reasoning=reasoning,
                popup_result=popup_result,
                error=f"human handoff requested: {plan.rationale}",
            )

        # ---------------------------------------------------- 3. Act-pre
        await self._apply_pre_actions(
            page=page, plan=plan,
            run_id=run_id, account_id=account_id,
            cancel_check=cancel_check,
        )

        # ---------------------------------------------------- 4. Act
        steps_attempted = 0
        steps_succeeded = 0
        last_error: str | None = None
        executed_action_summaries: list[str] = []

        for step in plan.steps:
            if cancel_check():
                break
            if step.action is ActionType.NOOP and not plan.pre_actions:
                # An honest no-op (engine had nothing to do) — count it
                # but don't pretend it ran.
                continue
            steps_attempted += 1
            sr = await self._run_step(page, step)

            if sr.success:
                steps_succeeded += 1
                # Settle the page so observed_url for recording is accurate.
                await self._settle_after_step(page, step)
                self.engine.record_step_success(
                    plan, step,
                    observed_url=_safe_url(page),
                    observed_text_fragment="",
                    duration_ms=sr.duration_ms,
                )
                executed_action_summaries.append(_format_step_summary(step, ok=True))
            else:
                last_error = sr.error or "step failed"
                self.engine.record_step_failure(plan, step)
                executed_action_summaries.append(_format_step_summary(step, ok=False))
                if not step.optional:
                    break

        reasoning.action = " | ".join(executed_action_summaries) or "(no steps run)"

        # ---------------------------------------------------- 5. Verify
        verification: VerificationResult | None = None
        if not cancel_check():
            try:
                # Settle for a short window so dynamic success messages
                # have time to show up before we score them.
                await self.waiter.wait_for_success_signal(
                    page, hints=hints,
                    timeout_ms=self.post_action_settle_ms,
                )
            except Exception:  # noqa: BLE001
                log.debug("post-action settle failed", exc_info=True)

            try:
                verification = await self.verifier.verify(
                    page, goal=goal_name, hints=hints,
                    before_url=before_url_value,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = last_error or f"verify failed: {exc!r}"

        success = bool(
            verification and verification.passed
            and steps_attempted > 0
            and steps_succeeded == steps_attempted
            and last_error is None
        )

        reasoning.verification = (
            verification.reason if verification else "no verification result"
        )
        reasoning.success = success
        reasoning.error = last_error if not success else None
        reasoning.duration_ms = _elapsed_ms(started)

        # ---------------------------------------------------- 6. Learn
        signal_kind = ""
        signal_value = ""
        for ta in plan.post_actions:
            if ta.action.kind is RuleActionKind.REPORT_SUCCESS:
                params = ta.action.params_dict
                signal_kind = "url_contains"  # rule's signal type is implicit
                signal_value = str(params.get("signal", goal_name))
                break

        self.engine.finalize(
            plan, success=success,
            final_url=_safe_url(page),
            success_signal_kind=signal_kind,
            success_signal_value=signal_value,
        )
        self._persist_reasoning(reasoning)

        await self._emit("agent.verify.completed", {
            "run_id": run_id, "account_id": account_id,
            "goal": goal_name, "success": success,
            "confidence": (verification.confidence if verification else 0.0),
            "reason": (verification.reason if verification else ""),
        })

        return StepLoopResult(
            goal=goal_name,
            success=success,
            duration_ms=_elapsed_ms(started),
            steps_attempted=steps_attempted,
            steps_succeeded=steps_succeeded,
            plan=plan,
            verification=verification,
            reasoning=reasoning,
            popup_result=popup_result,
            error=last_error if not success else None,
        )

    # ----------------------------------------------------------- pre-actions
    async def _apply_pre_actions(
        self,
        *,
        page: _PageLike,
        plan: DecisionPlan,
        run_id: str,
        account_id: str,
        cancel_check: Callable[[], bool],
    ) -> None:
        """Run rule-recommended pre-actions, capped at ``max_pre_actions``.

        The cap prevents pathological rule sets from making the loop
        spin (e.g. a popup that re-appears after every dismiss).
        """
        applied = 0
        for ta in plan.pre_actions:
            if applied >= self.max_pre_actions or cancel_check():
                break
            try:
                await self._apply_one_pre_action(page, ta)
                applied += 1
            except Exception:  # noqa: BLE001
                log.debug("pre_action %s failed", ta.action.kind.value, exc_info=True)
                # Note the failure in the plan so the reasoning panel
                # can show "tried to dismiss popup, didn't take" without
                # blowing up the run.
                plan.notes.append(
                    f"pre_action {ta.action.kind.value} failed via rule {ta.rule_name}"
                )
        if applied:
            await self._emit("agent.pre_actions.applied", {
                "run_id": run_id, "account_id": account_id,
                "count": applied,
            })

    async def _apply_one_pre_action(
        self, page: _PageLike, ta: TriggeredAction,
    ) -> None:
        kind = ta.action.kind
        params = ta.action.params_dict

        if kind is RuleActionKind.DISMISS_POPUP:
            await self.popup_guard.scan_and_dismiss(page)
            return
        if kind is RuleActionKind.WAIT:
            seconds = float(params.get("seconds", 1.0))
            await asyncio.sleep(min(max(0.0, seconds), 30.0))
            return
        if kind is RuleActionKind.WAIT_FOR_DOWNLOAD:
            await self.waiter.wait_for_download(page)
            return
        if kind is RuleActionKind.WAIT_FOR_NETWORK_IDLE:
            await self.waiter.wait_for_page_ready(page)
            return
        if kind is RuleActionKind.NAVIGATE:
            target = str(params.get("url") or params.get("target") or "")
            if "://" in target:
                # Only navigate to absolute URLs from rules — we never
                # synthesise URLs from a logical name like "login" here
                # (the deterministic engine doesn't know the URL map).
                try:
                    await page.goto(target, timeout=30_000)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    log.debug("rule navigate to %s failed", target, exc_info=True)
            return
        if kind is RuleActionKind.BACKOFF:
            seconds = float(params.get("seconds", 5.0))
            await asyncio.sleep(min(max(0.0, seconds), 60.0))
            return
        if kind is RuleActionKind.LOG:
            log.info(
                "rule[%s] log: %s", ta.rule_name, params.get("message", ""),
            )
            return
        # Other kinds (CLICK_INTENT, RETRY_STEP, REPORT_SUCCESS) are not
        # things the loop runs — they're advisory and consumed elsewhere.

    # ----------------------------------------------------------- step exec
    async def _run_step(self, page: _PageLike, step: ActionStep) -> StepResult:
        """Execute one action through the existing ``ActionExecutor``."""
        try:
            return await self.executor._run_step(page, step)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return StepResult(
                step=step,
                success=False,
                error=f"executor raised: {exc!r}",
                duration_ms=0,
            )

    async def _settle_after_step(
        self, page: _PageLike, step: ActionStep,
    ) -> None:
        """Wait briefly after each successful step.

        For navigations / submits we wait for the page to settle; for
        text fills we use the much smaller ``per_step_settle_ms`` so
        rapid form fills don't compound into multi-second pauses.
        """
        try:
            if step.action in (ActionType.CLICK, ActionType.NAVIGATE):
                await self.waiter.wait_for_page_ready(
                    page, timeout_ms=self.post_action_settle_ms,
                )
            else:
                await asyncio.sleep(self.per_step_settle_ms / 1000)
        except Exception:  # noqa: BLE001
            log.debug("settle after step failed", exc_info=True)

    # ----------------------------------------------------------- helpers
    def _persist_reasoning(self, entry: ReasoningEntry) -> None:
        if self.reasoning_log is None:
            return
        try:
            self.reasoning_log.append(entry)
        except Exception:  # noqa: BLE001
            log.debug("reasoning log append failed", exc_info=True)

    async def _emit(self, name: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            await self.on_event(name, data)
        except Exception:  # noqa: BLE001
            log.debug("on_event failed for %s", name, exc_info=True)

    def _fail(
        self,
        reasoning: ReasoningEntry,
        *,
        plan: DecisionPlan | None,
        started: float,
        error: str,
        goal_name: str,
    ) -> StepLoopResult:
        reasoning.success = False
        reasoning.error = error
        reasoning.duration_ms = _elapsed_ms(started)
        self._persist_reasoning(reasoning)
        return StepLoopResult(
            goal=goal_name,
            success=False,
            duration_ms=_elapsed_ms(started),
            steps_attempted=0,
            steps_succeeded=0,
            plan=plan or _stub_plan(goal_name),
            verification=None,
            reasoning=reasoning,
            error=error,
        )

    def _cancelled(
        self,
        reasoning: ReasoningEntry,
        *,
        plan: DecisionPlan | None,
        started: float,
        goal_name: str,
    ) -> StepLoopResult:
        reasoning.success = False
        reasoning.error = "cancelled"
        reasoning.action = "cancelled before any step ran"
        reasoning.duration_ms = _elapsed_ms(started)
        self._persist_reasoning(reasoning)
        return StepLoopResult(
            goal=goal_name,
            success=False,
            duration_ms=_elapsed_ms(started),
            steps_attempted=0,
            steps_succeeded=0,
            plan=plan or _stub_plan(goal_name),
            verification=None,
            reasoning=reasoning,
            error="cancelled",
        )


# ---------------------------------------------------------------- helpers
def _safe_url(page: _PageLike) -> str:
    try:
        return page.url or ""
    except Exception:  # noqa: BLE001
        return ""


async def _safe_body_text(page: _PageLike) -> str:
    """Pull at most 5KB of body text. Best-effort — never raises."""
    try:
        text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 5000)"
        )
        return str(text or "")
    except Exception:  # noqa: BLE001
        return ""


def _build_observation(
    *,
    snapshot: PageSnapshot,
    popup_result: PopupGuardResult | None,
    url: str,
    page_text: str,
) -> Observation:
    """Project a snapshot + page text into an :class:`Observation`."""
    visible_intents: list[str] = []
    for el in snapshot.elements:
        if not el.visible:
            continue
        for m in el.intents:
            if m.intent and m.intent not in visible_intents:
                visible_intents.append(m.intent)
    popup_kinds: tuple[str, ...] = ()
    if popup_result is not None:
        popup_kinds = tuple(k.value for k in popup_result.found)
    return Observation(
        url=url or snapshot.url,
        title=snapshot.title,
        body_text=page_text,
        visible_intents=tuple(visible_intents),
        popup_kinds=popup_kinds,
    )


def _format_observation(
    snapshot: PageSnapshot, popup_result: PopupGuardResult | None,
) -> str:
    visible_count = sum(1 for el in snapshot.elements if el.visible)
    parts = [
        f"url={snapshot.url[:80]}" if snapshot.url else "url=?",
        f"title={snapshot.title[:60]!r}" if snapshot.title else "",
        f"forms={snapshot.forms}",
        f"visible_elements={visible_count}",
    ]
    if popup_result and popup_result.dismissed:
        parts.append(
            "dismissed=" + ",".join(d.kind.value for d in popup_result.dismissed)
        )
    elif popup_result and popup_result.found:
        parts.append(
            "popups_seen=" + ",".join(k.value for k in popup_result.found)
        )
    return ", ".join(p for p in parts if p)


def _format_step_summary(step: ActionStep, *, ok: bool) -> str:
    marker = "✓" if ok else "✗"
    target = step.intent or step.selector or "?"
    return f"{marker}{step.action.value}({target})"


def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)


def _stub_plan(goal: str) -> DecisionPlan:
    """Used when we need a placeholder plan for an early-exit failure."""
    return DecisionPlan(
        goal=goal,
        source=DecisionSource.HUMAN,
        confidence=0.0,
        rationale="early exit before plan was constructed",
    )
