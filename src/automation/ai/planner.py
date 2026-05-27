"""Action planner: turn a high-level goal into a concrete action plan.

The planner consumes a ``PageSnapshot`` plus a goal (e.g. ``"login"`` or
``"register"``) and produces an ordered list of ``ActionStep`` instances. It
does *not* touch the browser — execution is delegated to ``ActionExecutor``.

Plans are simple, JSON-serializable, and inspectable so the dashboard and
human-review mode can present them before they run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from automation.ai.perception import DetectedElement, PageSnapshot

log = logging.getLogger(__name__)


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    WAIT_FOR = "wait_for"
    SCREENSHOT = "screenshot"
    VERIFY = "verify"
    NOOP = "noop"


@dataclass(slots=True)
class ActionStep:
    """A single planned action with everything the executor needs."""

    action: ActionType
    intent: str | None = None
    selector: str | None = None
    value: str | None = None
    confidence: float = 0.0
    rationale: str = ""
    timeout_ms: int = 10_000
    optional: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "intent": self.intent,
            "selector": self.selector,
            "value": self.value,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "timeout_ms": self.timeout_ms,
            "optional": self.optional,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class ActionPlan:
    goal: str
    steps: list[ActionStep] = field(default_factory=list)
    page_signature: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "page_signature": self.page_signature,
            "notes": self.notes,
            "steps": [s.to_dict() for s in self.steps],
        }


# Goal -> ordered list of element intents required, then a submit-ish action.
_GOAL_TEMPLATES: dict[str, list[str]] = {
    "login": ["username_field|email_field", "password_field", "submit|login"],
    "register": [
        "username_field|email_field",
        "password_field",
        "confirm_password_field?",
        "accept_terms?",
        "submit|register",
    ],
    "logout": ["logout"],
    "search": ["search", "submit?"],
    "dismiss_dialog": ["dialog_close"],
}


class ActionPlanner:
    """Goal-driven planner with template + memory hint fallbacks."""

    def __init__(self, memory: Any | None = None) -> None:
        # ``memory`` is an ``AIMemory``; typed as Any to avoid hard dependency
        # in the planner module (memory is optional).
        self.memory = memory

    async def plan(
        self,
        goal: str,
        snapshot: PageSnapshot,
        inputs: dict[str, str] | None = None,
    ) -> ActionPlan:
        inputs = inputs or {}
        plan = ActionPlan(goal=goal, page_signature=snapshot.signature)

        template = _GOAL_TEMPLATES.get(goal)
        if template is None:
            # Unknown goal: plan a single best-match click (e.g. goal == intent name)
            return await self._plan_single_intent(goal, snapshot, plan)

        for slot in template:
            optional = slot.endswith("?")
            slot_clean = slot.rstrip("?")
            choices = slot_clean.split("|")
            element, chosen_intent = self._pick(snapshot, choices)
            if element is None:
                if optional:
                    plan.notes.append(f"Skipped optional slot: {slot_clean}")
                    continue
                plan.notes.append(f"Missing required slot: {slot_clean}")
                # add a NOOP step so the executor can report the gap
                plan.steps.append(
                    ActionStep(
                        action=ActionType.NOOP,
                        intent=slot_clean,
                        rationale=f"Could not find element for {slot_clean}",
                        confidence=0.0,
                        optional=optional,
                    )
                )
                continue
            step = await self._step_for(element, chosen_intent, inputs)
            plan.steps.append(step)

        # always end with a screenshot + verify so we record outcome
        plan.steps.append(
            ActionStep(
                action=ActionType.SCREENSHOT,
                rationale="Capture post-action state",
                optional=True,
            )
        )
        plan.steps.append(
            ActionStep(
                action=ActionType.VERIFY,
                rationale=f"Verify goal '{goal}' completed",
                metadata={"goal": goal},
            )
        )
        return plan

    # ------------------------------------------------------------------ helpers
    async def _plan_single_intent(
        self, intent: str, snapshot: PageSnapshot, plan: ActionPlan
    ) -> ActionPlan:
        elements = snapshot.by_intent(intent)
        if not elements:
            plan.notes.append(f"No elements match intent '{intent}'")
            plan.steps.append(
                ActionStep(action=ActionType.NOOP, intent=intent, confidence=0.0)
            )
            return plan
        el = elements[0]
        plan.steps.append(
            ActionStep(
                action=ActionType.CLICK,
                intent=intent,
                selector=el.selector,
                confidence=el.confidence,
                rationale=f"Best match for intent '{intent}'",
            )
        )
        return plan

    def _pick(
        self, snapshot: PageSnapshot, choices: list[str]
    ) -> tuple[DetectedElement | None, str | None]:
        best: tuple[DetectedElement, str] | None = None
        best_score = 0.0
        for choice in choices:
            for el in snapshot.elements:
                if not el.visible:
                    continue
                for m in el.intents:
                    if m.intent == choice and m.score > best_score:
                        best = (el, choice)
                        best_score = m.score
        return (best[0], best[1]) if best else (None, None)

    async def _step_for(
        self,
        element: DetectedElement,
        intent: str,
        inputs: dict[str, str],
    ) -> ActionStep:
        # Prefer learned selector when memory is available and confident
        selector = element.selector
        if self.memory is not None:
            try:
                learned = await self.memory.get_selectors(
                    element.intents[0].matched_on if element.intents else "",
                    intent,
                    limit=1,
                )
                if learned and learned[0].confidence >= 0.7:
                    selector = learned[0].selector
            except Exception:  # noqa: BLE001
                log.exception("planner: memory lookup failed")

        if intent in {"username_field", "email_field"}:
            value = inputs.get("username") or inputs.get("email") or ""
            return ActionStep(
                action=ActionType.FILL,
                intent=intent,
                selector=selector,
                value=value,
                confidence=element.confidence,
                rationale=f"Fill {intent}",
            )
        if intent in {"password_field", "confirm_password_field"}:
            value = inputs.get("password") or ""
            return ActionStep(
                action=ActionType.FILL,
                intent=intent,
                selector=selector,
                value=value,
                confidence=element.confidence,
                rationale=f"Fill {intent}",
            )
        if intent == "search":
            return ActionStep(
                action=ActionType.FILL,
                intent=intent,
                selector=selector,
                value=inputs.get("query", ""),
                confidence=element.confidence,
                rationale="Fill search query",
            )
        if intent == "accept_terms":
            return ActionStep(
                action=ActionType.CHECK,
                intent=intent,
                selector=selector,
                confidence=element.confidence,
                rationale="Accept terms checkbox",
            )
        # default to click (submit, login, register, dialog_close, etc.)
        return ActionStep(
            action=ActionType.CLICK,
            intent=intent,
            selector=selector,
            confidence=element.confidence,
            rationale=f"Click {intent}",
        )
