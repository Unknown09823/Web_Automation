"""Data-driven IF/THEN rule engine.

Operators have always been able to script *workflows*, but each step
of a workflow has the same brittle "selector + click + wait" shape.
What's been missing is a way to declare cross-cutting policies like:

  * "If a cookie banner is visible, accept it."
  * "If the page says 'session expired', navigate to /login first."
  * "If a captcha appears, stop and wait for a human."
  * "If a download starts, wait for it to complete before moving on."

These are *rules*: predicates over the current observation, paired
with one or more recommended actions. The rule engine evaluates all
rules at every observe pass and returns the matching actions sorted
by priority. The deterministic engine consumes that list before it
even reaches the heuristic / AI levels of the stack.

Properties
==========

* **Data-driven.** Built-in rules are defined as plain dictionaries
  via :data:`DEFAULT_RULES_JSON`; operators add more by dropping a
  JSON file in ``config/rules/`` (loaded by :py:meth:`RuleEngine.load`).
* **Pure.** ``RuleEngine.evaluate`` is a synchronous, side-effect-free
  function. The caller decides what to do with the recommendations.
* **Composable.** Conditions are AND-combined within a rule; rules
  themselves are OR-combined across the catalog. Rule priority
  resolves the order of returned actions.

The engine deliberately does not *execute* the actions itself — that
is the deterministic engine's job. Keeping evaluation pure makes
unit tests trivial and lets the same rules fire in dry-run mode.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


# ---------------------------------------------------------- enums
class ConditionKind(str, Enum):
    """Predicates the engine knows how to evaluate.

    Conditions read fields from :class:`Observation`. Adding a new
    kind is one entry in this enum plus one ``elif`` branch in
    :py:meth:`RuleEngine._eval_condition`.
    """

    URL_CONTAINS = "url_contains"
    URL_NOT_CONTAINS = "url_not_contains"
    URL_MATCHES = "url_matches"  # regex
    TITLE_CONTAINS = "title_contains"
    BODY_CONTAINS_ANY = "body_contains_any"
    BODY_CONTAINS_ALL = "body_contains_all"
    BODY_NOT_CONTAINS = "body_not_contains"
    HAS_INTENT = "has_intent"           # any element exposes intent name
    POPUP_DETECTED = "popup_detected"   # any popup found by guard
    POPUP_KIND = "popup_kind"           # specific popup kind
    DOWNLOAD_STARTED = "download_started"
    PREVIOUS_FAILED = "previous_failed"
    RETRY_COUNT_GTE = "retry_count_gte"
    HAS_ERROR_TEXT = "has_error_text"   # convenience over BODY_CONTAINS_ANY


class ActionKind(str, Enum):
    """Recommendations the engine emits.

    Names are deliberately distinct from
    :class:`automation.ai.planner.ActionType` to avoid confusion at
    import sites.
    """

    DISMISS_POPUP = "dismiss_popup"
    WAIT = "wait"
    WAIT_FOR_DOWNLOAD = "wait_for_download"
    WAIT_FOR_NETWORK_IDLE = "wait_for_network_idle"
    CLICK_INTENT = "click_intent"
    NAVIGATE = "navigate"
    RETRY_STEP = "retry_step"
    MARK_HUMAN = "mark_human"
    BACKOFF = "backoff"
    REPORT_SUCCESS = "report_success"
    LOG = "log"


# ---------------------------------------------------------- observation
@dataclass(slots=True)
class Observation:
    """Everything a rule may inspect about the current state.

    Centralized so tests can construct one inline, and so the
    deterministic engine has a single object to populate after each
    perception pass.
    """

    url: str = ""
    title: str = ""
    body_text: str = ""
    visible_intents: tuple[str, ...] = ()
    popup_kinds: tuple[str, ...] = ()
    download_in_progress: bool = False
    previous_step_failed: bool = False
    retry_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def body_lower(self) -> str:
        # Cached repeatedly because rules typically check several phrases.
        # The tuple of immutable text means a new Observation is created per
        # observe pass, so caching as an attribute would be wrong; recomputing
        # each access is acceptable since body_text is at most ~5KB.
        return self.body_text.lower()


# ---------------------------------------------------------- rules
@dataclass(slots=True, frozen=True)
class RuleCondition:
    kind: ConditionKind
    value: Any = None  # interpretation depends on kind

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleCondition":
        return cls(kind=ConditionKind(data["kind"]), value=data.get("value"))


@dataclass(slots=True, frozen=True)
class RuleAction:
    kind: ActionKind
    params: tuple[tuple[str, Any], ...] = ()  # frozen-friendly mapping

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "params": dict(self.params)}

    @property
    def params_dict(self) -> dict[str, Any]:
        return dict(self.params)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleAction":
        params = tuple((k, v) for k, v in (data.get("params") or {}).items())
        return cls(kind=ActionKind(data["kind"]), params=params)


@dataclass(slots=True, frozen=True)
class Rule:
    name: str
    conditions: tuple[RuleCondition, ...]
    actions: tuple[RuleAction, ...]
    priority: int = 100
    enabled: bool = True
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "conditions": [c.to_dict() for c in self.conditions],
            "actions": [a.to_dict() for a in self.actions],
            "priority": self.priority,
            "enabled": self.enabled,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        return cls(
            name=str(data.get("name") or "<unnamed>"),
            conditions=tuple(
                RuleCondition.from_dict(c) for c in (data.get("conditions") or [])
            ),
            actions=tuple(
                RuleAction.from_dict(a) for a in (data.get("actions") or [])
            ),
            priority=int(data.get("priority", 100)),
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description") or ""),
        )


# ---------------------------------------------------------- triggered
@dataclass(slots=True)
class TriggeredAction:
    """A rule-recommended action plus provenance."""

    rule_name: str
    action: RuleAction
    priority: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "action": self.action.to_dict(),
            "priority": self.priority,
        }


@dataclass(slots=True)
class RuleEvaluation:
    """Outcome of one evaluate() pass."""

    triggered: list[TriggeredAction] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def by_kind(self, kind: ActionKind) -> list[TriggeredAction]:
        return [t for t in self.triggered if t.action.kind is kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": [t.to_dict() for t in self.triggered],
            "matched_rules": list(self.matched_rules),
            "duration_ms": self.duration_ms,
        }

    def render(self) -> str:
        if not self.triggered:
            return "no rules fired"
        lines = []
        for t in self.triggered:
            params = t.action.params_dict
            tail = f" {params}" if params else ""
            lines.append(f"[{t.rule_name}] -> {t.action.kind.value}{tail}")
        return "\n".join(lines)


# ---------------------------------------------------------- engine
class RuleEngine:
    """Evaluate a set of rules against an :class:`Observation`.

    Rules are kept in priority order (lowest priority number first ≈
    "fires earliest"). Operators add rules via:

    * ``RuleEngine.add_rule(rule)`` programmatically;
    * ``RuleEngine.load(path)`` from a JSON file.

    The engine is stateless — each call to :py:meth:`evaluate` is
    independent. That's intentional: rate-limiting and "fire only
    once per run" semantics belong to the caller, who has the run
    context.
    """

    def __init__(self, *, rules: Iterable[Rule] | None = None) -> None:
        self._rules: list[Rule] = list(rules or default_rules())
        self._sort()

    # --------------------------------------------------------------- load
    def add_rule(self, rule: Rule) -> None:
        self._rules.append(rule)
        self._sort()

    def load(self, path: str | Path) -> int:
        """Load rules from a JSON file. Returns the count loaded.

        File format is a list of rule dicts (see :py:meth:`Rule.from_dict`).
        Malformed entries are skipped with a warning; one bad rule never
        kills the rest of the catalog.
        """
        p = Path(path)
        if not p.exists():
            return 0
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):  # noqa: BLE001
            log.warning("rule engine: cannot read %s", p, exc_info=True)
            return 0
        if not isinstance(raw, list):
            log.warning("rule engine: %s must contain a JSON array", p)
            return 0
        loaded = 0
        for entry in raw:
            try:
                self._rules.append(Rule.from_dict(entry))
                loaded += 1
            except (KeyError, ValueError, TypeError):  # noqa: BLE001
                log.warning("rule engine: skipping malformed rule in %s", p)
        self._sort()
        return loaded

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(self._rules)

    # ------------------------------------------------------------ evaluate
    def evaluate(self, observation: Observation) -> RuleEvaluation:
        """Evaluate all enabled rules. Returns triggered actions in priority
        order, lowest priority number first.

        A rule fires when *every* condition is true. Empty condition lists
        fire unconditionally — useful for default-actions like "always
        wait_for_network_idle" but operators rarely want that.
        """
        import time as _t
        started = _t.time()
        ev = RuleEvaluation()
        for rule in self._rules:
            if not rule.enabled:
                continue
            if not all(self._eval_condition(c, observation) for c in rule.conditions):
                continue
            ev.matched_rules.append(rule.name)
            for action in rule.actions:
                ev.triggered.append(TriggeredAction(
                    rule_name=rule.name,
                    action=action,
                    priority=rule.priority,
                ))
        ev.triggered.sort(key=lambda t: t.priority)
        ev.duration_ms = int((_t.time() - started) * 1000)
        return ev

    # ------------------------------------------------------------ private
    def _sort(self) -> None:
        self._rules.sort(key=lambda r: r.priority)

    def _eval_condition(
        self, c: RuleCondition, o: Observation,
    ) -> bool:
        kind = c.kind
        v = c.value

        if kind is ConditionKind.URL_CONTAINS:
            return bool(v) and str(v).lower() in o.url.lower()
        if kind is ConditionKind.URL_NOT_CONTAINS:
            return not (v and str(v).lower() in o.url.lower())
        if kind is ConditionKind.URL_MATCHES:
            try:
                return bool(re.search(str(v or ""), o.url))
            except re.error:
                return False
        if kind is ConditionKind.TITLE_CONTAINS:
            return bool(v) and str(v).lower() in o.title.lower()
        if kind is ConditionKind.BODY_CONTAINS_ANY:
            phrases = _coerce_list(v)
            body = o.body_lower
            return any(p.lower() in body for p in phrases if p)
        if kind is ConditionKind.BODY_CONTAINS_ALL:
            phrases = _coerce_list(v)
            body = o.body_lower
            return all(p.lower() in body for p in phrases if p)
        if kind is ConditionKind.BODY_NOT_CONTAINS:
            phrases = _coerce_list(v)
            body = o.body_lower
            return not any(p.lower() in body for p in phrases if p)
        if kind is ConditionKind.HAS_INTENT:
            return str(v) in o.visible_intents
        if kind is ConditionKind.POPUP_DETECTED:
            return bool(o.popup_kinds)
        if kind is ConditionKind.POPUP_KIND:
            return str(v) in o.popup_kinds
        if kind is ConditionKind.DOWNLOAD_STARTED:
            return bool(o.download_in_progress)
        if kind is ConditionKind.PREVIOUS_FAILED:
            return bool(o.previous_step_failed)
        if kind is ConditionKind.RETRY_COUNT_GTE:
            try:
                return o.retry_count >= int(v)
            except (TypeError, ValueError):
                return False
        if kind is ConditionKind.HAS_ERROR_TEXT:
            phrases = _coerce_list(v) or list(_DEFAULT_ERROR_PHRASES)
            body = o.body_lower
            return any(p.lower() in body for p in phrases)
        return False


# ---------------------------------------------------------- helpers
def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [str(value)]


_DEFAULT_ERROR_PHRASES = (
    "error", "failed", "invalid", "incorrect", "wrong",
    "unauthorized", "forbidden", "denied", "expired",
)


# ---------------------------------------------------------- defaults
DEFAULT_RULES_JSON: list[dict[str, Any]] = [
    {
        "name": "popup_detected_dismiss",
        "description": "Any popup -> ask the popup guard to dismiss it",
        "priority": 10,
        "conditions": [{"kind": "popup_detected"}],
        "actions": [{"kind": "dismiss_popup"}],
    },
    {
        "name": "captcha_human_handoff",
        "description": "Captcha or 'verify you are human' -> mark for human",
        "priority": 5,
        "conditions": [
            {
                "kind": "body_contains_any",
                "value": [
                    "captcha", "are you human", "verify you are human",
                    "i am not a robot", "press and hold",
                    "complete the security check",
                ],
            },
        ],
        "actions": [{"kind": "mark_human", "params": {"reason": "captcha"}}],
    },
    {
        "name": "session_expired_relogin",
        "description": "Session expired -> navigate back to login",
        "priority": 30,
        "conditions": [
            {
                "kind": "body_contains_any",
                "value": [
                    "session expired", "please log in", "sign in to continue",
                    "your session has timed out",
                ],
            },
        ],
        "actions": [{"kind": "navigate", "params": {"target": "login"}}],
    },
    {
        "name": "rate_limit_backoff",
        "description": "Rate limit / 429 / too many requests -> backoff",
        "priority": 20,
        "conditions": [
            {
                "kind": "body_contains_any",
                "value": [
                    "rate limit", "too many requests", "try again later",
                    "slow down",
                ],
            },
        ],
        "actions": [{"kind": "backoff", "params": {"seconds": 30}}],
    },
    {
        "name": "download_started_wait",
        "description": "Download started -> wait for completion",
        "priority": 40,
        "conditions": [{"kind": "download_started"}],
        "actions": [{"kind": "wait_for_download"}],
    },
    {
        "name": "dashboard_success_signal",
        "description": "Reached a dashboard URL -> register success signal",
        "priority": 50,
        "conditions": [
            {
                "kind": "url_matches",
                "value": "/(dashboard|home|account|profile|lobby)(/|$|\\?)",
            },
        ],
        "actions": [
            {"kind": "report_success", "params": {"signal": "dashboard"}},
        ],
    },
    {
        "name": "login_failed_retry",
        "description": "After a failed login attempt with error text -> retry once",
        "priority": 60,
        "conditions": [
            {"kind": "previous_failed"},
            {"kind": "retry_count_gte", "value": 0},
            {
                "kind": "body_contains_any",
                "value": [
                    "incorrect password", "invalid credentials",
                    "wrong password", "login failed",
                ],
            },
            {"kind": "retry_count_gte", "value": 0},
        ],
        "actions": [{"kind": "retry_step"}],
    },
    {
        "name": "verification_required_wait",
        "description": "Verification email/SMS pending -> wait briefly and continue",
        "priority": 70,
        "conditions": [
            {
                "kind": "body_contains_any",
                "value": [
                    "verify your email", "check your email",
                    "verification code", "we sent you a code",
                ],
            },
        ],
        "actions": [{"kind": "wait", "params": {"seconds": 5}}],
    },
]


def default_rules() -> list[Rule]:
    """Return a fresh list of the built-in rules."""
    return [Rule.from_dict(r) for r in DEFAULT_RULES_JSON]
