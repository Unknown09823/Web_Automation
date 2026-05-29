"""AdaptiveExecutor: replay-first / AI-fallback dispatcher.

The user's spec, in one diagram::

    Account #1     →  Full AI       (records template)
    Account #2..N  →  Replay        (no LLM, deterministic)
       ↓ replay fails
       AI Recovery →  records new template version
       ↓
    Account #N+1.. →  Replay (updated template)

This module is the orchestrator that wires those modes together. It is
called once per goal attempt by :class:`automation.agent.agent.BrowserAgent`
in the place where the brain used to be invoked unconditionally.

Behaviour::

    executor.execute(page, goal, inputs, ...) ->
        ExecutionMode in {REPLAY_OK, REPLAY_FAILED, AI_USED, NO_OP}

  * REPLAY_OK    — replayed an existing high-confidence template; no
                   LLM call was made; stats updated, confidence bumped.
  * REPLAY_FAILED — a replay was tried and failed mid-way. The caller
                   now falls back to its existing AI path. Stats updated,
                   confidence decayed.
  * AI_USED      — no eligible template existed (cold-start) OR every
                   eligible template had already failed; the caller's
                   AI path will run.
  * NO_OP        — the goal isn't replay-eligible at all (e.g. it has no
                   parsable target_url, or it's the agent's own NAVIGATE
                   sub-goal we'd rather have BrowserAgent handle directly).

Force-AI override:
    The caller can pass ``force_ai=True`` (e.g. when the user requests
    /relearn from Telegram) to skip replay even if a confident template
    is available.

Recording:
    On account success, the BrowserAgent calls
    ``executor.record_from_run(...)`` which delegates to
    :class:`TemplateRecorder` to distill the run's ledger into one or
    more new templates / versions.
"""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from automation.agent.execution_template import (
    ExecutionTemplate,
    ExecutionTemplateStore,
)
from automation.agent.goals import AgentGoal, GoalType
from automation.agent.template_recorder import TemplateRecorder
from automation.agent.template_replayer import ReplayResult, TemplateReplayer
from automation.agent.site_memory import domain_from_url

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
#  result type
# -----------------------------------------------------------------------------
class ExecutionMode(str, enum.Enum):
    REPLAY_OK     = "replay_ok"
    REPLAY_FAILED = "replay_failed"
    AI_USED       = "ai_used"
    NO_OP         = "no_op"


@dataclass(slots=True)
class AdaptiveResult:
    """What the executor did for one goal attempt.

    The BrowserAgent uses this to decide its next move:

      * ``mode == REPLAY_OK``      → goal is done, skip AI
      * ``mode == REPLAY_FAILED``  → fall back to brain.run()
      * ``mode == AI_USED``        → goal handled here, no further work
      * ``mode == NO_OP``          → executor opted out, caller handles all
    """
    mode: ExecutionMode
    success: bool
    template: ExecutionTemplate | None = None
    replay: ReplayResult | None = None
    reason: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "success": self.success,
            "template": (
                {
                    "domain": self.template.domain,
                    "workflow": self.template.workflow,
                    "version": self.template.version,
                    "confidence": self.template.confidence,
                } if self.template else None
            ),
            "replay": self.replay.to_dict() if self.replay else None,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
        }


# -----------------------------------------------------------------------------
#  executor
# -----------------------------------------------------------------------------
class AdaptiveExecutor:
    """Decide replay vs AI per goal, manage template lifecycle."""

    # Goals we never try to replay deterministically. NAVIGATE is owned
    # by the agent's own logic (it does its own URL handling and waits)
    # and CUSTOM goals are too generic to safely replay.
    _NEVER_REPLAY: frozenset[GoalType] = frozenset({
        GoalType.NAVIGATE,
        GoalType.CUSTOM,
    })

    def __init__(
        self,
        *,
        store: ExecutionTemplateStore,
        replayer: TemplateReplayer,
        recorder: TemplateRecorder,
        event_emitter: Any = None,
    ) -> None:
        self.store = store
        self.replayer = replayer
        self.recorder = recorder
        self.event_emitter = event_emitter

    # ------------------------------------------------------------------- API
    def find_template(
        self, goal: AgentGoal, target_url: str,
    ) -> ExecutionTemplate | None:
        """Return the eligible template for this goal+domain, or None."""
        domain = self._extract_domain(goal, target_url)
        if not domain:
            return None
        return self.store.find_best(domain, goal.type.value)

    async def execute(
        self,
        page: Any,
        goal: AgentGoal,
        inputs: dict[str, Any],
        *,
        target_url: str = "",
        recorder: Any = None,
        force_ai: bool = False,
    ) -> AdaptiveResult:
        """Try to satisfy a goal via replay; on miss/fail, return AI signal."""
        started = time.time()

        # 1) Goals the executor opts out of — let the caller handle them.
        if goal.type in self._NEVER_REPLAY:
            return AdaptiveResult(
                mode=ExecutionMode.NO_OP, success=False,
                reason=f"goal type {goal.type.value} is not replay-eligible",
                duration_ms=int((time.time() - started) * 1000),
            )

        # 2) Forced AI re-learn (operator explicitly asked).
        if force_ai:
            return AdaptiveResult(
                mode=ExecutionMode.AI_USED, success=False,
                reason="force_ai requested",
                duration_ms=int((time.time() - started) * 1000),
            )

        # 3) Look up a candidate template.
        domain = self._extract_domain(goal, target_url)
        if not domain:
            return AdaptiveResult(
                mode=ExecutionMode.AI_USED, success=False,
                reason="no domain to resolve a template against",
                duration_ms=int((time.time() - started) * 1000),
            )
        template = self.store.find_best(domain, goal.type.value)
        if template is None:
            return AdaptiveResult(
                mode=ExecutionMode.AI_USED, success=False,
                reason=f"no eligible template for {domain}/{goal.type.value}",
                duration_ms=int((time.time() - started) * 1000),
            )

        # 4) Required inputs check — refuse to replay when we'd interpolate
        # to empty strings for a key the template depends on. That would
        # invariably fail at run-time and waste a network round-trip.
        missing = template.input_keys_required - set(inputs.keys())
        # treat empty/None values as missing too
        missing |= {
            k for k in template.input_keys_required
            if not str(inputs.get(k, "") or "").strip()
        }
        if missing:
            return AdaptiveResult(
                mode=ExecutionMode.AI_USED,
                success=False,
                template=template,
                reason=(
                    f"template needs inputs {sorted(missing)}; falling back to AI"
                ),
                duration_ms=int((time.time() - started) * 1000),
            )

        # 5) Run the replay. This is the hot path — no LLM call.
        await self._emit_replay_event(
            "agent.replay.started",
            template=template, goal=goal, message="Replaying template",
        )
        replay = await self.replayer.replay(page, template, inputs, recorder=recorder)
        template.record_replay_outcome(
            success=replay.success,
            duration_ms=replay.duration_ms,
            failure_reason=replay.failure_reason,
        )
        # Persist updated stats so the next account sees the new confidence.
        self.store.save(template)

        if replay.success:
            await self._emit_replay_event(
                "agent.replay.succeeded",
                template=template, goal=goal,
                message=(
                    f"Replay v{template.version} succeeded "
                    f"(confidence={template.confidence})"
                ),
            )
            return AdaptiveResult(
                mode=ExecutionMode.REPLAY_OK, success=True,
                template=template, replay=replay,
                reason="template replay succeeded",
                duration_ms=int((time.time() - started) * 1000),
            )

        await self._emit_replay_event(
            "agent.replay.failed",
            template=template, goal=goal,
            message=(
                f"Replay v{template.version} failed at action "
                f"#{replay.failed_index}: {replay.failure_reason}"
            ),
        )
        return AdaptiveResult(
            mode=ExecutionMode.REPLAY_FAILED, success=False,
            template=template, replay=replay,
            reason=replay.failure_reason,
            duration_ms=int((time.time() - started) * 1000),
        )

    # ------------------------------------------------------------ recording
    def record_from_run(
        self,
        records: list[Any],
        *,
        inputs: dict[str, Any],
        target_url: str,
        run_id: str = "",
        account_id: str = "",
    ) -> list[ExecutionTemplate]:
        """Distill a successful run into new ExecutionTemplate(s).

        Called by the agent at the end of a successful account so account
        #2 has something to replay. Safe to call after EVERY account; the
        recorder versions up so existing templates are never overwritten,
        and we limit the number of stored versions per (domain, workflow)
        below to avoid unbounded growth.
        """
        templates = self.recorder.distill(
            records,
            inputs=inputs,
            target_url=target_url,
            run_id=run_id,
            account_id=account_id,
        )
        if templates:
            self._prune_old_versions(templates[0].domain)
        return templates

    # ------------------------------------------------------------ helpers
    def _extract_domain(self, goal: AgentGoal, target_url: str) -> str:
        """Resolve the domain key to look up templates for."""
        for candidate in (
            goal.params.get("url"),
            target_url,
            goal.params.get("origin_url"),
        ):
            d = self.store.normalize_domain(candidate or "")
            if d:
                return d
        return ""

    def _prune_old_versions(
        self, domain: str, *, keep_per_workflow: int = 5,
    ) -> None:
        """Keep at most ``keep_per_workflow`` versions per workflow.

        We keep:
          * the highest-confidence template (operator's likely default), and
          * the most recent (keep_per_workflow - 1) versions

        Older inferior versions are deleted. This bounds disk use without
        sacrificing the ability to roll back to a recent prior version.
        """
        for t in self.store.list_for_domain(domain):
            pass  # iterate to materialise; could be optimised later
        # group by workflow
        groups: dict[str, list[ExecutionTemplate]] = {}
        for t in self.store.list_for_domain(domain):
            groups.setdefault(t.workflow, []).append(t)
        for wf, versions in groups.items():
            if len(versions) <= keep_per_workflow:
                continue
            # Always keep the best by confidence
            best = max(versions, key=lambda x: (x.confidence, x.version))
            # Sort the rest by recency (descending version)
            rest = [v for v in versions if v is not best]
            rest.sort(key=lambda x: x.version, reverse=True)
            keep = {best.version} | {v.version for v in rest[:keep_per_workflow - 1]}
            for v in versions:
                if v.version not in keep:
                    self.store.delete(domain, wf, v.version)

    async def _emit_replay_event(
        self, event_type: str, *, template: ExecutionTemplate,
        goal: AgentGoal, message: str,
    ) -> None:
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter(event_type, {
                "domain": template.domain,
                "workflow": template.workflow,
                "version": template.version,
                "confidence": template.confidence,
                "goal": goal.description or goal.type.value,
                "message": message,
            })
        except Exception:  # noqa: BLE001
            log.debug("event emission for %s failed (non-fatal)", event_type)


__all__ = [
    "AdaptiveExecutor",
    "AdaptiveResult",
    "ExecutionMode",
]
