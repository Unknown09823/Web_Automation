"""The 6-level priority engine: deterministic-first, AI-as-last-resort.

Every "what should the agent do *next*?" question goes through this
module. It evaluates levels in priority order and returns the first
viable plan, recording exactly which level fired so the reasoning
panel can explain the decision:

    1. REPLAY        — a saved site template matches; replay it.
    2. SITE_MEMORY   — cached selectors from previous successes.
    3. HEURISTIC     — semantic element detection without AI.
    4. RULE          — IF/THEN rules apply (popup dismiss, mark human, …).
    5. AI            — invoke the AIBrain for free-form reasoning.
    6. HUMAN         — emit a "needs human" marker step.

Rules are *also* evaluated once at the top of the call and their
recommendations are blended into the chosen plan (see ``pre_actions``
and ``post_actions`` on :class:`DecisionPlan`). That way the engine
gets to dismiss a cookie banner in front of a deterministic plan
rather than spending an AI call on it.

The engine itself does **not** drive the browser. It returns plans;
the loop module (``loop.py``) executes them and feeds results back
via :py:meth:`feedback`. Keeping execution out of the engine lets us:

* unit-test the decision logic with cheap fakes;
* swap in a different executor (replay-only mode, dry-run mode);
* run the engine offline against a snapshot for diagnostics.

The engine also owns *learning*: when the loop finalizes a goal as
successful, the engine asks the template store to record / promote
the template, asks the site memory to update its button counters,
and asks the rule engine which signals confirmed success.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from automation.agent.heuristics import Candidate, Heuristics, HeuristicPlan
from automation.agent.popup_guard import PopupGuardResult
from automation.agent.reasoning import DecisionSource
from automation.agent.rule_engine import (
    ActionKind as RuleActionKind,
    Observation,
    RuleEngine,
    TriggeredAction,
)
from automation.agent.site_memory import SiteMemory
from automation.agent.site_templates import (
    SiteTemplate,
    TemplateRecording,
    TemplateStep,
    TemplateStore,
)
from automation.ai.perception import PageSnapshot
from automation.ai.planner import ActionStep, ActionType

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- goal map
@dataclass(slots=True, frozen=True)
class GoalShape:
    """How a high-level goal decomposes into form fields and a submit click.

    For a form-fill goal (register / login) we know which intent
    groups to fill and what to press to submit. For a click-only
    goal (claim_reward / open_rewards_page) ``form_fields`` is empty
    and ``click_intent`` does the work.
    """

    form_fields: tuple[str, ...] = ()  # heuristic group names, in fill order
    click_intent: str = ""              # heuristic group used as submit/CTA
    require_form: bool = False          # at least one field must be found


GOAL_SHAPES: dict[str, GoalShape] = {
    "register": GoalShape(
        form_fields=("email_field", "password_field", "confirm_password_field"),
        click_intent="register",
        require_form=True,
    ),
    "login": GoalShape(
        form_fields=("username_field", "password_field"),
        click_intent="login",
        require_form=True,
    ),
    "logout": GoalShape(click_intent="logout"),
    "claim_reward": GoalShape(click_intent="claim_reward"),
    "open_rewards_page": GoalShape(click_intent="open_rewards_page"),
    "open_betting_page": GoalShape(click_intent="open_betting_page"),
    "place_bet": GoalShape(click_intent="place_bet"),
    "deposit": GoalShape(click_intent="deposit"),
    "withdraw": GoalShape(click_intent="withdraw"),
    "verify_email": GoalShape(click_intent="verify_email"),
    "submit": GoalShape(click_intent="submit"),
}


# ---------------------------------------------------------------- plan
@dataclass(slots=True)
class DecisionPlan:
    """The engine's answer to "what should we do for this goal?"

    The plan is deliberately self-describing: every consumer (the
    loop, the reasoning log, the dashboard) can introspect every
    field without separate context.
    """

    goal: str
    source: DecisionSource
    confidence: float
    rationale: str
    steps: list[ActionStep] = field(default_factory=list)
    pre_actions: list[TriggeredAction] = field(default_factory=list)
    post_actions: list[TriggeredAction] = field(default_factory=list)
    alternatives: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # Internal — not for serialization. The loop hands these objects
    # back via feedback() so the engine can update its stores.
    template: SiteTemplate | None = None
    recording: TemplateRecording | None = None
    host: str = ""
    initial_url: str = ""
    initial_signature: str = ""

    @property
    def usable(self) -> bool:
        return any(s.action is not ActionType.NOOP for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "source": self.source.value,
            "confidence": round(self.confidence, 3),
            "rationale": self.rationale,
            "steps": [s.to_dict() for s in self.steps],
            "pre_actions": [a.to_dict() for a in self.pre_actions],
            "post_actions": [a.to_dict() for a in self.post_actions],
            "alternatives": [a.to_dict() for a in self.alternatives],
            "notes": list(self.notes),
            "host": self.host,
            "template_version": self.template.version if self.template else None,
        }


# ---------------------------------------------------------------- engine
class DeterministicEngine:
    """Pick the cheapest level of the stack that can satisfy a goal.

    Components are *all* optional except heuristics — the engine still
    works with no AI brain, no site memory, and no templates. Each
    missing component just removes that level from the priority
    ladder.

    The engine is stateless across calls (each ``decide`` is fresh),
    but it does mutate its component stores via ``feedback`` /
    ``finalize`` to learn from outcomes.
    """

    def __init__(
        self,
        *,
        rules: RuleEngine | None = None,
        heuristics: Heuristics | None = None,
        site_memory: SiteMemory | None = None,
        templates: TemplateStore | None = None,
        brain: Any = None,
        # Confidence thresholds:
        replay_min_match: float = 0.5,
        site_memory_min_confidence: float = 0.6,
        heuristic_min_confidence: float = 0.5,
        ai_enabled: bool = True,
    ) -> None:
        self.rules = rules or RuleEngine()
        self.heuristics = heuristics or Heuristics()
        self.site_memory = site_memory
        self.templates = templates
        self.brain = brain
        self.replay_min_match = float(replay_min_match)
        self.site_memory_min_confidence = float(site_memory_min_confidence)
        self.heuristic_min_confidence = float(heuristic_min_confidence)
        self.ai_enabled = bool(ai_enabled)

    # ----------------------------------------------------------- decide
    async def decide(
        self,
        *,
        goal: str,
        page_url: str,
        page_signature: str,
        snapshot: PageSnapshot,
        observation: Observation,
        inputs: dict[str, str] | None = None,
        host: str = "",
    ) -> DecisionPlan:
        """Build a :class:`DecisionPlan` for ``goal``.

        ``inputs`` carries credentials / values for form-fill goals.
        ``host`` overrides URL-derived host (useful when crossing
        sub-domains within one workflow).
        """
        inputs = dict(inputs or {})
        host = host or _host_from_url(page_url)

        # ---- Rules first (they may short-circuit everything else) ----
        rule_eval = self.rules.evaluate(observation)
        pre_actions: list[TriggeredAction] = []
        post_actions: list[TriggeredAction] = []
        for ta in rule_eval.triggered:
            kind = ta.action.kind
            if kind is RuleActionKind.MARK_HUMAN:
                # Hard escalation. Skip the rest.
                return _human_handoff_plan(
                    goal=goal,
                    rationale=f"rule {ta.rule_name} requires human intervention",
                    pre_actions=pre_actions, post_actions=post_actions,
                    rule_action=ta, host=host, page_url=page_url,
                    page_signature=page_signature,
                )
            if kind is RuleActionKind.REPORT_SUCCESS:
                post_actions.append(ta)
            elif kind in (
                RuleActionKind.DISMISS_POPUP,
                RuleActionKind.WAIT,
                RuleActionKind.WAIT_FOR_DOWNLOAD,
                RuleActionKind.WAIT_FOR_NETWORK_IDLE,
                RuleActionKind.NAVIGATE,
                RuleActionKind.BACKOFF,
                RuleActionKind.LOG,
            ):
                pre_actions.append(ta)
            elif kind is RuleActionKind.RETRY_STEP:
                # Caller (loop) interprets this — it isn't a step we run.
                post_actions.append(ta)
            elif kind is RuleActionKind.CLICK_INTENT:
                # Rule says "click X first, then continue" — express as a
                # heuristic-built click step prepended to the plan.
                pre_actions.append(ta)

        # ---- Level 1: REPLAY ------------------------------------------
        if self.templates is not None:
            tpl = self.templates.find_template(
                host=host, goal=goal, url=page_url,
                signature=page_signature, min_match=self.replay_min_match,
            )
            if tpl is not None:
                return self._plan_from_template(
                    tpl=tpl, goal=goal, host=host,
                    page_url=page_url, page_signature=page_signature,
                    inputs=inputs, pre_actions=pre_actions,
                    post_actions=post_actions,
                )

        # ---- Level 2: SITE_MEMORY ------------------------------------
        sm_plan = self._plan_from_site_memory(
            goal=goal, host=host, page_url=page_url,
            inputs=inputs,
        )
        if sm_plan is not None:
            sm_plan.pre_actions = pre_actions
            sm_plan.post_actions = post_actions
            sm_plan.recording = self._open_recording(
                host=host, goal=goal,
                page_url=page_url, page_signature=page_signature,
            )
            return sm_plan

        # ---- Level 3: HEURISTIC ---------------------------------------
        h_plan = self._plan_from_heuristics(
            goal=goal, snapshot=snapshot, inputs=inputs,
        )
        if h_plan is not None and h_plan.confidence >= self.heuristic_min_confidence:
            h_plan.pre_actions = pre_actions
            h_plan.post_actions = post_actions
            h_plan.host = host
            h_plan.initial_url = page_url
            h_plan.initial_signature = page_signature
            h_plan.recording = self._open_recording(
                host=host, goal=goal,
                page_url=page_url, page_signature=page_signature,
            )
            return h_plan

        # ---- Level 4: RULE-only path (when rules alone produce work) ---
        # If a rule emitted DISMISS_POPUP / WAIT etc. but there's no
        # action plan beneath it, we still want a valid plan to run.
        if pre_actions and not (h_plan and h_plan.usable):
            return DecisionPlan(
                goal=goal,
                source=DecisionSource.RULE,
                confidence=0.5,
                rationale=(
                    "no deterministic plan available; "
                    f"rules produced {len(pre_actions)} pre-action(s)"
                ),
                steps=[ActionStep(
                    action=ActionType.NOOP, intent=goal,
                    rationale="rule-only step; loop applies pre_actions",
                    optional=True,
                )],
                pre_actions=pre_actions,
                post_actions=post_actions,
                host=host,
                initial_url=page_url,
                initial_signature=page_signature,
            )

        # ---- Level 5: AI ---------------------------------------------
        if self.ai_enabled and self.brain is not None:
            ai_plan = await self._plan_from_ai(
                goal=goal, snapshot=snapshot, inputs=inputs,
            )
            if ai_plan is not None:
                ai_plan.pre_actions = pre_actions
                ai_plan.post_actions = post_actions
                ai_plan.host = host
                ai_plan.initial_url = page_url
                ai_plan.initial_signature = page_signature
                ai_plan.recording = self._open_recording(
                    host=host, goal=goal,
                    page_url=page_url, page_signature=page_signature,
                )
                return ai_plan

        # ---- Level 6: HUMAN -----------------------------------------
        return _human_handoff_plan(
            goal=goal,
            rationale="no level of the deterministic stack produced a plan",
            pre_actions=pre_actions, post_actions=post_actions,
            host=host, page_url=page_url, page_signature=page_signature,
        )

    # ----------------------------------------------------------- feedback
    def record_popup_dismissals(
        self, plan: DecisionPlan, popup_result: PopupGuardResult,
    ) -> None:
        """Tell the engine which popups the loop just dismissed.

        Currently only used to enrich the plan's notes for the
        reasoning panel; the popup_guard already updates its own
        internal counters.
        """
        if popup_result.any_dismissed:
            plan.notes.append(popup_result.render())

    def record_step_success(
        self,
        plan: DecisionPlan,
        executed: ActionStep,
        *,
        observed_url: str = "",
        observed_text_fragment: str = "",
        duration_ms: int = 0,
    ) -> None:
        """Persist that a step worked.

        For replay plans this is essentially a no-op — counters bump
        on goal-level finalize. For heuristic / AI plans this appends
        the step to the in-progress recording so the next account
        gets a template to replay.

        Site memory always benefits, regardless of source: a working
        ``(intent, selector)`` pair on a host is durable knowledge.
        """
        if executed.intent and executed.selector and self.site_memory is not None:
            self.site_memory.record_button(
                plan.host or _host_from_url(observed_url) or _host_from_url(plan.initial_url),
                executed.intent,
                executed.selector,
                success=True,
            )
        if plan.recording is not None and executed.action is not ActionType.NOOP:
            plan.recording.add_step(TemplateStep(
                action=executed.action.value,
                selector=executed.selector,
                # Never persist the literal value — it's per-account.
                # Replay uses a value_override at execution time.
                value=None,
                intent=executed.intent,
                timeout_ms=executed.timeout_ms,
                optional=executed.optional,
                expected_url_fragment=observed_url[-40:] if observed_url else "",
                expected_text_fragment=observed_text_fragment,
                duration_ms_observed=int(duration_ms),
                rationale=executed.rationale,
            ))

    def record_step_failure(
        self,
        plan: DecisionPlan,
        executed: ActionStep,
    ) -> None:
        """Persist that a step failed. Updates site_memory counters.

        For an in-progress recording we abort it — a partially failed
        flow is not a template anyone wants to replay.
        """
        if executed.intent and executed.selector and self.site_memory is not None:
            self.site_memory.record_button(
                plan.host or _host_from_url(plan.initial_url),
                executed.intent,
                executed.selector,
                success=False,
            )
        if plan.recording is not None:
            plan.recording.abort(reason=f"{executed.action.value} failed")

    def finalize(
        self,
        plan: DecisionPlan,
        *,
        success: bool,
        final_url: str = "",
        success_signal_kind: str = "",
        success_signal_value: str = "",
    ) -> None:
        """Goal-level wrap-up: bump replay counters, commit recordings,
        update site_memory success signals, flush stores.

        Called once per goal by the loop. Safe to call when ``plan`` is
        a HUMAN handoff (it just won't have anything to commit).
        """
        host = plan.host or _host_from_url(plan.initial_url)

        # Replay path — bump the template's counters.
        if plan.template is not None and self.templates is not None:
            self.templates.record_replay_result(plan.template, success=success)

        # Recording path — commit on success, drop on failure.
        if plan.recording is not None and self.templates is not None:
            if success:
                # Annotate with final-url pattern + signal so future replays
                # know what to look for as confirmation.
                if final_url:
                    plan.recording.final_url_pattern = final_url[-40:]
                if success_signal_kind:
                    plan.recording.success_signals.append({
                        "kind": success_signal_kind,
                        "value": success_signal_value,
                    })
                self.templates.commit(plan.recording)
            else:
                plan.recording.abort(reason="goal failed")

        # Site memory: signal counters.
        if (
            success
            and self.site_memory is not None
            and success_signal_kind
            and success_signal_value
        ):
            self.site_memory.record_success_signal(
                final_url or plan.initial_url,
                goal=plan.goal,
                kind=success_signal_kind,
                value=success_signal_value,
                success=True,
            )

        # Persist on every finalize so a crash never loses learning.
        if self.site_memory is not None:
            try:
                self.site_memory.flush(host)
            except Exception:  # noqa: BLE001
                log.warning("site_memory flush failed", exc_info=True)
        if self.templates is not None:
            try:
                self.templates.flush(host)
            except Exception:  # noqa: BLE001
                log.warning("templates flush failed", exc_info=True)

    # ------------------------------------------------------------ private
    def _open_recording(
        self, *, host: str, goal: str,
        page_url: str, page_signature: str,
    ) -> TemplateRecording | None:
        if self.templates is None:
            return None
        return self.templates.start_recording(
            host=host, goal=goal,
            initial_url=page_url, initial_signature=page_signature,
        )

    def _plan_from_template(
        self,
        *,
        tpl: SiteTemplate,
        goal: str,
        host: str,
        page_url: str,
        page_signature: str,
        inputs: dict[str, str],
        pre_actions: list[TriggeredAction],
        post_actions: list[TriggeredAction],
    ) -> DecisionPlan:
        steps: list[ActionStep] = []
        for tstep in tpl.steps:
            value_override: str | None = None
            if tstep.intent:
                value_override = _value_for_intent(tstep.intent, inputs)
            steps.append(tstep.to_action_step(value_override=value_override))
        plan = DecisionPlan(
            goal=goal,
            source=DecisionSource.REPLAY,
            confidence=round(tpl.confidence, 3),
            rationale=(
                f"template {tpl.host}::{tpl.goal} v{tpl.version} "
                f"(success {tpl.success_count}/{tpl.success_count + tpl.fail_count})"
            ),
            steps=steps,
            template=tpl,
            host=host,
            initial_url=page_url,
            initial_signature=page_signature,
            pre_actions=pre_actions,
            post_actions=post_actions,
        )
        return plan

    def _plan_from_site_memory(
        self,
        *,
        goal: str,
        host: str,
        page_url: str,
        inputs: dict[str, str],
    ) -> DecisionPlan | None:
        """Build a plan from cached selectors when the goal is click-only."""
        if self.site_memory is None:
            return None
        shape = GOAL_SHAPES.get(goal)
        if shape is None or shape.form_fields:
            # Form goals depend on multiple selectors per page; the
            # template path handles those uniformly. Site memory alone
            # is most useful for single-click goals.
            return None
        intent = shape.click_intent or goal
        candidates = self.site_memory.get_buttons(
            page_url, intent,
            min_confidence=self.site_memory_min_confidence,
        )
        if not candidates:
            return None
        best = candidates[0]
        return DecisionPlan(
            goal=goal,
            source=DecisionSource.SITE_MEMORY,
            confidence=round(best.confidence, 3),
            rationale=(
                f"cached selector for {host} intent={intent} "
                f"({best.success_count}/{best.success_count + best.fail_count})"
            ),
            steps=[ActionStep(
                action=ActionType.CLICK,
                intent=intent,
                selector=best.selector,
                confidence=best.confidence,
                rationale="site memory hit",
                timeout_ms=10_000,
            )],
            host=host,
            initial_url=page_url,
        )

    def _plan_from_heuristics(
        self,
        *,
        goal: str,
        snapshot: PageSnapshot,
        inputs: dict[str, str],
    ) -> DecisionPlan | None:
        shape = GOAL_SHAPES.get(goal)
        if shape and shape.form_fields:
            field_values = {
                name: _value_for_intent(name, inputs)
                for name in shape.form_fields
                if _value_for_intent(name, inputs) is not None
            }
            hp: HeuristicPlan = self.heuristics.build_form_plan(
                snapshot, fields=field_values,
                submit_group=shape.click_intent or "submit",
            )
        else:
            click_intent = (shape.click_intent if shape else "") or goal
            hp = self.heuristics.build_click_plan(snapshot, click_intent)

        if not hp.usable:
            return None
        return DecisionPlan(
            goal=goal,
            source=DecisionSource.HEURISTIC,
            confidence=hp.confidence,
            rationale="heuristic detection over snapshot",
            steps=hp.steps,
            alternatives=hp.alternatives,
            notes=list(hp.notes),
        )

    async def _plan_from_ai(
        self,
        *,
        goal: str,
        snapshot: PageSnapshot,
        inputs: dict[str, str],
    ) -> DecisionPlan | None:
        try:
            ai_plan = await self.brain.decide(goal, snapshot, inputs)
        except Exception:  # noqa: BLE001
            log.warning("AI brain decide failed", exc_info=True)
            return None
        if not ai_plan or not ai_plan.steps:
            return None
        confidence = (
            sum(s.confidence for s in ai_plan.steps)
            / max(len(ai_plan.steps), 1)
        )
        return DecisionPlan(
            goal=goal,
            source=DecisionSource.AI,
            confidence=round(confidence, 3),
            rationale="AI brain plan",
            steps=list(ai_plan.steps),
            notes=list(ai_plan.notes or []),
        )


# ---------------------------------------------------------------- helpers
def _host_from_url(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        return url.split("/", 1)[0]
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _value_for_intent(intent: str, inputs: dict[str, str]) -> str | None:
    """Pick the right entry from ``inputs`` for a heuristic field intent.

    The mapping is purposely generous so the same ``inputs`` dict
    works for register/login/recovery flows.
    """
    if intent == "username_field":
        return (
            inputs.get("username")
            or inputs.get("email")
            or inputs.get("number")
            or inputs.get("phone")
        )
    if intent == "email_field":
        return inputs.get("email") or inputs.get("username")
    if intent == "phone_field":
        return inputs.get("phone") or inputs.get("number")
    if intent == "password_field":
        return inputs.get("password")
    if intent == "confirm_password_field":
        return inputs.get("confirm_password") or inputs.get("password")
    if intent == "search":
        return inputs.get("query")
    return inputs.get(intent)


def _human_handoff_plan(
    *,
    goal: str,
    rationale: str,
    pre_actions: list[TriggeredAction],
    post_actions: list[TriggeredAction],
    rule_action: TriggeredAction | None = None,
    host: str = "",
    page_url: str = "",
    page_signature: str = "",
) -> DecisionPlan:
    """Build a HUMAN-source plan with a single NOOP step.

    The NOOP carries metadata the loop / Telegram bot can use to
    notify operators (rule_name, reason, etc.).
    """
    metadata: dict[str, Any] = {"human_required": True}
    if rule_action:
        metadata["rule"] = rule_action.rule_name
        metadata.update(rule_action.action.params_dict)
    return DecisionPlan(
        goal=goal,
        source=DecisionSource.HUMAN,
        confidence=0.0,
        rationale=rationale,
        steps=[ActionStep(
            action=ActionType.NOOP,
            intent="human_handoff",
            rationale=rationale,
            optional=False,
            metadata=metadata,
        )],
        pre_actions=pre_actions,
        post_actions=post_actions,
        host=host,
        initial_url=page_url,
        initial_signature=page_signature,
    )
