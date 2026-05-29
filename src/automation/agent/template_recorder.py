"""TemplateRecorder: distill ActionRecorder ledgers into ExecutionTemplates.

Account #1 runs in full-AI mode. Every browser action it takes is logged
by :class:`automation.agent.recorder.ActionRecorder`. After the goal
succeeds, this module walks the ledger between the goal's
``GOAL_START`` and ``GOAL_END`` records and produces an
:class:`ExecutionTemplate` that can be replayed deterministically by
account #2..N.

Key responsibilities:

  * Parameterise concrete values: when a fill records the value
    ``"+1234567890"`` and the inputs dict contains ``number ==
    "+1234567890"``, the template stores ``value_template = "${number}"``.
    This keeps templates reusable across accounts.

  * Translate raw waits into named conditions. The recorder logs
    ``wait_for("page_ready")`` style calls; we keep the condition string
    so the replayer can hand it back to ``AdaptiveWaiter``. Numeric
    sleeps that may have crept in are deliberately dropped — the user's
    spec is "store conditions rather than timings".

  * Skip artefacts that do not affect outcomes: AI_DECISION,
    AI_REASONING, SCREENSHOT, HTML_SNAPSHOT records are useful for
    audit but never replayed.

  * Return one :class:`ExecutionTemplate` per (goal-type, success)
    bracket found in the ledger. If account #1 completes both
    ``register_account`` and ``login`` successfully in one run, we emit
    two templates.
"""
from __future__ import annotations

import logging
from typing import Any

from automation.agent.execution_template import (
    ActionKind,
    ExecutionTemplate,
    ExecutionTemplateStore,
    TemplateAction,
)
from automation.agent.recorder import ActionRecord, RecordType
from automation.agent.site_memory import domain_from_url

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
#  config
# -----------------------------------------------------------------------------

# RecordTypes we never copy into the deterministic template.
_NON_REPLAYABLE = {
    RecordType.AI_DECISION,
    RecordType.AI_REASONING,
    RecordType.SCREENSHOT,
    RecordType.HTML_SNAPSHOT,
    RecordType.RECOVERY,
    RecordType.CUSTOM,
    RecordType.ERROR,
}

# Map ActionRecorder RecordType → template ActionKind for the replayable subset.
_KIND_MAP: dict[RecordType, ActionKind] = {
    RecordType.NAVIGATE: ActionKind.NAVIGATE,
    RecordType.CLICK:    ActionKind.CLICK,
    RecordType.FILL:     ActionKind.FILL,
    RecordType.SELECT:   ActionKind.SELECT,
    RecordType.CHECK:    ActionKind.CHECK,
}


# -----------------------------------------------------------------------------
#  recorder
# -----------------------------------------------------------------------------
class TemplateRecorder:
    """Convert successful ledgers into reusable execution templates."""

    # Minimum number of replayable actions we require before emitting a
    # template. Below this we'd be saving something so trivial replay
    # would be no faster than the full pipeline.
    MIN_ACTIONS_PER_TEMPLATE = 1

    def __init__(self, store: ExecutionTemplateStore) -> None:
        self.store = store

    # ----------------------------------------------------------------- API
    def distill(
        self,
        records: list[ActionRecord],
        *,
        inputs: dict[str, Any],
        target_url: str,
        run_id: str = "",
        account_id: str = "",
        default_wait_for: str | None = "page_load",
    ) -> list[ExecutionTemplate]:
        """Distill a *successful* run's ledger into one template per goal.

        Args:
            records: Output of ``ActionRecorder.records``. Must include the
                ``GOAL_START`` / ``GOAL_END`` brackets.
            inputs: The values the run was given (account number, password,
                email, …). Used to parameterise concrete fill values.
            target_url: Best-effort domain key when records contain no URL.
            run_id, account_id: Provenance, written into the template.
            default_wait_for: Wait condition stamped onto fills/clicks
                that have no explicit follow-up wait in the ledger. The
                user's spec mandates *some* wait condition between every
                deterministic action so replay never depends on timing.

        Returns:
            List of saved :class:`ExecutionTemplate`s (one per
            successful goal). On no successful goals: empty list.
        """
        domain = self.store.normalize_domain(target_url)
        if not domain:
            # Try to discover it from the first navigate
            for r in records:
                if r.type == RecordType.NAVIGATE and r.url:
                    domain = self.store.normalize_domain(r.url)
                    if domain:
                        break
        if not domain:
            log.debug("template_recorder: no domain identified, skipping")
            return []

        templates: list[ExecutionTemplate] = []
        for goal_records, goal_name in self._iter_successful_goals(records):
            actions = self._records_to_actions(
                goal_records,
                inputs=inputs,
                default_wait_for=default_wait_for,
            )
            if len(actions) < self.MIN_ACTIONS_PER_TEMPLATE:
                continue

            workflow = _normalize_workflow(goal_name)
            version = self.store.next_version(domain, workflow)
            tpl = ExecutionTemplate(
                domain=domain,
                workflow=workflow,
                version=version,
                actions=actions,
                created_from_run_id=run_id,
                created_from_account_id=account_id,
                origin_url=target_url,
            )
            self.store.save(tpl)
            templates.append(tpl)
            log.info(
                "execution template recorded: domain=%s workflow=%s v%d "
                "actions=%d", domain, workflow, version, len(actions),
            )
        return templates

    # --------------------------------------------------------- ledger walk
    def _iter_successful_goals(
        self, records: list[ActionRecord],
    ) -> list[tuple[list[ActionRecord], str]]:
        """Yield (records-inside-bracket, goal-name) for each succeeded goal."""
        out: list[tuple[list[ActionRecord], str]] = []
        i = 0
        while i < len(records):
            r = records[i]
            if r.type == RecordType.GOAL_START:
                goal_name = r.goal or ""
                # Find the matching GOAL_END
                j = i + 1
                inner: list[ActionRecord] = []
                while j < len(records):
                    rr = records[j]
                    if rr.type == RecordType.GOAL_END and rr.goal == goal_name:
                        if rr.success:
                            out.append((inner, goal_name))
                        break
                    inner.append(rr)
                    j += 1
                i = j + 1
                continue
            i += 1
        return out

    # ------------------------------------------------------- record→action
    def _records_to_actions(
        self,
        records: list[ActionRecord],
        *,
        inputs: dict[str, Any],
        default_wait_for: str | None,
    ) -> list[TemplateAction]:
        """Convert ledger records into TemplateActions, attaching waits."""
        actions: list[TemplateAction] = []
        i = 0
        n = len(records)
        while i < n:
            r = records[i]
            if r.type in _NON_REPLAYABLE:
                i += 1
                continue
            if r.type == RecordType.WAIT:
                # A standalone wait without a preceding action attaches to
                # the previous action's wait_for if missing, otherwise it
                # becomes a no-op wait step (kept so verifier can chain).
                cond = (r.data or {}).get("condition") or default_wait_for
                if actions and not actions[-1].wait_for:
                    actions[-1].wait_for = _normalize_condition(cond)
                else:
                    actions.append(TemplateAction(
                        kind=ActionKind.WAIT,
                        wait_for=_normalize_condition(cond),
                        timeout_ms=max(int(r.duration_ms or 0) * 4, 30_000),
                    ))
                i += 1
                continue
            if r.type == RecordType.VERIFICATION:
                hints = list((r.data or {}).get("hints") or [])
                actions.append(TemplateAction(
                    kind=ActionKind.VERIFY, hints=hints,
                ))
                i += 1
                continue

            kind = _KIND_MAP.get(r.type)
            if kind is None:
                i += 1
                continue

            action = self._build_action(r, kind, inputs)

            # Look ahead one step: if the next record is a WAIT, attach it
            # to this action's wait_for and skip the WAIT record.
            if i + 1 < n and records[i + 1].type == RecordType.WAIT:
                w = records[i + 1]
                cond = (w.data or {}).get("condition") or default_wait_for
                action.wait_for = _normalize_condition(cond)
                actions.append(action)
                i += 2
                continue
            # Otherwise stamp a sensible default so we never replay raw.
            if not action.wait_for and default_wait_for:
                action.wait_for = _normalize_condition(default_wait_for)
            actions.append(action)
            i += 1

        return actions

    def _build_action(
        self,
        record: ActionRecord,
        kind: ActionKind,
        inputs: dict[str, Any],
    ) -> TemplateAction:
        """Single ledger record → one TemplateAction with parameterisation."""
        if kind == ActionKind.NAVIGATE:
            return TemplateAction(
                kind=kind,
                url_template=_parameterise(record.url, inputs),
                timeout_ms=max(int(record.duration_ms or 0) * 4, 30_000),
                meta={"source_url": record.url},
            )
        if kind == ActionKind.CLICK:
            return TemplateAction(
                kind=kind,
                selector=record.selector or "",
                meta={"source_url": record.url},
            )
        if kind in (ActionKind.FILL, ActionKind.SELECT, ActionKind.PRESS):
            return TemplateAction(
                kind=kind,
                selector=record.selector or "",
                value_template=_parameterise(record.value or "", inputs),
                meta={"source_url": record.url},
            )
        if kind == ActionKind.CHECK:
            return TemplateAction(
                kind=kind,
                selector=record.selector or "",
                meta={"source_url": record.url},
            )
        # Fallback (shouldn't happen)
        return TemplateAction(
            kind=kind, selector=record.selector,
            value_template=record.value,
        )


# -----------------------------------------------------------------------------
#  helpers
# -----------------------------------------------------------------------------
def _parameterise(value: str, inputs: dict[str, Any]) -> str:
    """Replace concrete user values inside ``value`` with ``${name}`` refs.

    Done by reverse lookup: for every (name, value) in inputs where the
    string is non-trivial (>= 3 chars, not a digit) and shows up in the
    captured value, swap it. We do longest-value-first so the more
    specific replacement wins (e.g. ``email`` before ``username`` if
    they happen to share a prefix).
    """
    if not value:
        return value
    out = value
    candidates = sorted(
        ((str(k), str(v)) for k, v in (inputs or {}).items()
         if isinstance(v, (str, int, float)) and str(v).strip()
         and len(str(v)) >= 3),
        key=lambda kv: len(kv[1]),
        reverse=True,
    )
    for key, raw in candidates:
        if raw and raw in out:
            out = out.replace(raw, "${" + key + "}")
    return out


def _normalize_condition(cond: str | None) -> str | None:
    """Map free-form wait labels to AdaptiveWaiter condition names.

    The ledger uses labels like ``page_ready`` / ``download_complete`` /
    ``success_signal``. We translate those to the canonical condition
    enum values the replayer understands. Unknown labels fall back to
    ``page_load`` rather than raising — the user's spec wants
    "intelligent waiting" to be the default everywhere.
    """
    if not cond:
        return None
    c = cond.strip().lower()
    aliases = {
        "page_ready":     "page_load",
        "page_loaded":    "page_load",
        "page_load":      "page_load",
        "navigation":     "navigation_complete",
        "nav":            "navigation_complete",
        "url_change":     "url_change",
        "url_changed":    "url_change",
        "network_idle":   "network_idle",
        "dom_stable":     "dom_stable",
        "dom_settled":    "dom_stable",
        "element_visible": "element_visible",
        "visible":        "element_visible",
        "element_gone":   "element_gone",
        "gone":           "element_gone",
        "download":          "download_complete",
        "download_complete": "download_complete",
        "success":           "success_message",
        "success_signal":    "success_message",
        "success_message":   "success_message",
    }
    return aliases.get(c, c if c in {
        "page_load", "dom_stable", "network_idle", "url_change",
        "element_visible", "element_gone", "download_complete",
        "success_message", "no_loading", "navigation_complete",
    } else "page_load")


def _normalize_workflow(goal_name: str) -> str:
    """Goal description → snake_case workflow id."""
    name = (goal_name or "").strip().lower()
    if not name:
        return "unknown"
    name = "".join(c if c.isalnum() else "_" for c in name)
    while "__" in name:
        name = name.replace("__", "_")
    return name.strip("_")[:48] or "unknown"


__all__ = ["TemplateRecorder"]
