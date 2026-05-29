"""Deterministic semantic element detection.

The legacy planner asked the AI brain whenever it needed to find a
button or field. That works, but it makes every step a paid model
call and a network round-trip. For the vast majority of real pages,
the *meaning* of a button can be inferred without AI: the text
"Sign Up", "Register", "Create Account", and "Join Now" all mean the
same thing, and a heuristic that knows the synonym set picks the
right element in microseconds.

This module is the deterministic-first detector. It complements
:class:`automation.ai.intents.IntentMatcher` (which labels individual
elements) by:

  * grouping primitive intents into *high-level groups* the agent
    actually cares about (``claim_reward`` ≈ "Claim Gift" ≈
    "Get Bonus" ≈ "Redeem Reward"),
  * adding keyword synonyms that the IntentMatcher does not know about
    out of the box (a future ``intents.py`` expansion can subsume
    these — until then the heuristics catch them),
  * scoring candidates by *multiple* signals (intent match, raw text
    match, role agreement, visibility, document order, container
    proximity), and
  * emitting ranked alternatives so the recovery stack has a fallback
    list without re-querying the page.

The output is a list of :class:`ActionStep` objects compatible with
the existing :class:`ActionExecutor`, so the rest of the framework
does not need to change.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from automation.ai.perception import DetectedElement, PageSnapshot
from automation.ai.planner import ActionStep, ActionType

log = logging.getLogger(__name__)


# ----------------------------------------------------------- intent groups
# A group maps a high-level concept to a tuple of primitive ``IntentMatcher``
# intents *plus* a list of keyword synonyms used when the matcher missed
# them. Keyword matching is case-insensitive, whitespace-tolerant and
# allows substring hits ("Claim Gift" matches "claim").
#
# When extending: prefer adding to the matcher first (richer signals),
# fall back to keywords here when the term is too domain-specific to
# generalize (e.g. "place bet").
@dataclass(slots=True, frozen=True)
class IntentGroup:
    name: str
    base_intents: tuple[str, ...]
    keywords: tuple[str, ...]
    preferred_roles: tuple[str, ...] = ("button", "link")
    description: str = ""


INTENT_GROUPS: dict[str, IntentGroup] = {
    "register": IntentGroup(
        name="register",
        base_intents=("register",),
        keywords=(
            "register", "sign up", "signup", "create account",
            "create an account", "join", "join now", "get started",
            "new account", "new user",
        ),
        description="Create a new account",
    ),
    "login": IntentGroup(
        name="login",
        base_intents=("login",),
        keywords=(
            "login", "log in", "sign in", "signin", "access account",
            "enter account",
        ),
        description="Authenticate to existing account",
    ),
    "logout": IntentGroup(
        name="logout",
        base_intents=("logout",),
        keywords=("logout", "log out", "sign out", "signout", "exit"),
        description="End the current session",
    ),
    "submit": IntentGroup(
        name="submit",
        base_intents=("submit",),
        keywords=(
            "submit", "send", "save", "confirm", "continue",
            "next", "proceed", "ok", "done", "apply", "go",
        ),
        description="Submit a form / confirm an action",
    ),
    "claim_reward": IntentGroup(
        name="claim_reward",
        base_intents=(),
        keywords=(
            "claim gift", "claim reward", "claim bonus", "claim now",
            "claim", "get gift", "get reward", "get bonus",
            "redeem reward", "redeem gift", "redeem now", "redeem",
            "collect reward", "collect bonus", "collect gift",
            "open gift", "spin", "spin the wheel", "reveal",
        ),
        description="Claim a reward / gift / bonus",
    ),
    "open_rewards_page": IntentGroup(
        name="open_rewards_page",
        base_intents=(),
        keywords=(
            "rewards", "my rewards", "bonuses", "bonus", "promotions",
            "offers", "gifts", "loyalty",
        ),
        description="Navigate to the rewards / bonuses page",
    ),
    "open_betting_page": IntentGroup(
        name="open_betting_page",
        base_intents=(),
        keywords=(
            "bet", "bets", "betting", "place bet", "sportsbook",
            "sports", "casino", "live betting", "lobby",
        ),
        description="Navigate to the betting / casino lobby",
    ),
    "place_bet": IntentGroup(
        name="place_bet",
        base_intents=(),
        keywords=(
            "place bet", "place a bet", "bet now", "stake",
            "confirm bet", "submit bet", "play",
        ),
        description="Place / confirm a wager",
    ),
    "deposit": IntentGroup(
        name="deposit",
        base_intents=(),
        keywords=(
            "deposit", "add funds", "top up", "top-up",
            "make deposit", "add money", "fund account",
        ),
        description="Open the deposit flow",
    ),
    "withdraw": IntentGroup(
        name="withdraw",
        base_intents=(),
        keywords=(
            "withdraw", "withdrawal", "cash out", "cashout",
            "payout", "request payout",
        ),
        description="Open the withdrawal flow",
    ),
    "dashboard": IntentGroup(
        name="dashboard",
        base_intents=("dashboard",),
        keywords=(
            "dashboard", "home", "overview", "main", "lobby",
        ),
        description="Navigate to the user dashboard / home",
    ),
    "profile": IntentGroup(
        name="profile",
        base_intents=("profile",),
        keywords=(
            "profile", "account", "my account", "my profile",
            "settings", "preferences",
        ),
        description="Open profile / account settings",
    ),
    "verify_email": IntentGroup(
        name="verify_email",
        base_intents=(),
        keywords=(
            "verify email", "confirm email", "verify your email",
            "click here to verify", "activate account",
        ),
        description="Email verification action",
    ),
    "search": IntentGroup(
        name="search",
        base_intents=("search",),
        keywords=("search", "find", "lookup"),
        preferred_roles=("textbox", "searchbox", "button"),
        description="Search input or trigger",
    ),
    # Field intents (different role bias — passed through unchanged from
    # the matcher; included here so the heuristics layer is self-sufficient).
    "username_field": IntentGroup(
        name="username_field",
        base_intents=("username_field", "email_field"),
        keywords=(
            "username", "user name", "email", "phone", "mobile",
            "login id", "user id",
        ),
        preferred_roles=("textbox",),
        description="Identifier input (username / email / phone)",
    ),
    "email_field": IntentGroup(
        name="email_field",
        base_intents=("email_field",),
        keywords=("email", "e-mail", "email address"),
        preferred_roles=("textbox",),
        description="Email input",
    ),
    "phone_field": IntentGroup(
        name="phone_field",
        base_intents=(),
        keywords=("phone", "mobile", "number", "phone number", "mobile number"),
        preferred_roles=("textbox",),
        description="Phone / mobile number input",
    ),
    "password_field": IntentGroup(
        name="password_field",
        base_intents=("password_field",),
        keywords=("password", "passcode", "secret"),
        preferred_roles=("textbox",),
        description="Password input",
    ),
    "confirm_password_field": IntentGroup(
        name="confirm_password_field",
        base_intents=("confirm_password_field",),
        keywords=(
            "confirm password", "repeat password", "re-enter password",
            "verify password",
        ),
        preferred_roles=("textbox",),
        description="Password confirmation input",
    ),
    "accept_terms": IntentGroup(
        name="accept_terms",
        base_intents=("accept_terms",),
        keywords=(
            "agree", "i agree", "accept terms", "terms and conditions",
            "privacy policy", "consent",
        ),
        preferred_roles=("checkbox",),
        description="Terms / consent checkbox",
    ),
}


# ----------------------------------------------------------------- types
@dataclass(slots=True)
class Candidate:
    """A scored element that *could* satisfy a target intent group.

    We carry the raw element alongside the precomputed selector and
    confidence so the deterministic engine can both act on it and
    surface alternatives in the reasoning panel.
    """

    element: DetectedElement
    selector: str
    score: float
    rationale: str
    matched_on: str  # "intent", "text", "aria", "placeholder", "role"

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "score": self.score,
            "rationale": self.rationale,
            "matched_on": self.matched_on,
            "text": self.element.text[:60],
            "tag": self.element.tag,
            "role": self.element.role,
        }


@dataclass(slots=True)
class HeuristicPlan:
    """Result of building a plan for a goal without AI."""

    goal: str
    steps: list[ActionStep] = field(default_factory=list)
    confidence: float = 0.0
    alternatives: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """A plan is usable when it produced at least one non-NOOP step."""
        return any(s.action is not ActionType.NOOP for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "confidence": self.confidence,
            "notes": self.notes,
            "steps": [s.to_dict() for s in self.steps],
            "alternatives": [a.to_dict() for a in self.alternatives],
        }


# ----------------------------------------------------------------- engine
class Heuristics:
    """Deterministic detector + plan builder — no AI required.

    Every public method is a pure function over a :class:`PageSnapshot`
    plus optional inputs. The class itself is stateless; you can keep
    a single instance for the lifetime of the agent.
    """

    def __init__(
        self,
        *,
        groups: dict[str, IntentGroup] | None = None,
        min_button_score: float = 0.45,
        min_field_score: float = 0.40,
    ) -> None:
        self.groups: dict[str, IntentGroup] = dict(groups or INTENT_GROUPS)
        self.min_button_score = float(min_button_score)
        self.min_field_score = float(min_field_score)

    # --------------------------------------------------------------- find
    # Role aliases — the perception layer reports the raw tag for elements
    # without explicit ``role=`` (so an ``<input>`` is "input"), but
    # IntentMatcher / WAI-ARIA think in canonical role names. Filter
    # by the equivalence class instead of the literal string.
    _ROLE_ALIASES: dict[str, frozenset[str]] = {
        "textbox": frozenset({"textbox", "searchbox", "input", "textarea"}),
        "button": frozenset({"button", "link"}),
        "checkbox": frozenset({"checkbox", "switch", "input"}),
    }

    def candidates(
        self,
        snapshot: PageSnapshot,
        group_name: str,
        *,
        role_filter: str | None = None,
        min_score: float | None = None,
        max_results: int = 5,
    ) -> list[Candidate]:
        """Return ranked candidate elements for an intent group.

        ``role_filter`` lets callers narrow the search (e.g. only
        ``textbox`` for fields). ``min_score`` defaults to
        :py:attr:`min_button_score` for clickable groups and
        :py:attr:`min_field_score` for input groups.
        """
        group = self.groups.get(group_name)
        if group is None:
            return []
        threshold = (
            float(min_score)
            if min_score is not None
            else (
                self.min_field_score
                if "field" in group_name
                else self.min_button_score
            )
        )
        allowed_roles = self._ROLE_ALIASES.get(role_filter, None) if role_filter else None
        scored: list[Candidate] = []
        for el in snapshot.elements:
            if not el.visible:
                continue
            if allowed_roles is not None and el.role not in allowed_roles \
                    and el.tag not in allowed_roles:
                continue
            score, matched_on = self._score(el, group)
            if score < threshold:
                continue
            scored.append(
                Candidate(
                    element=el,
                    selector=el.selector,
                    score=round(score, 3),
                    rationale=self._explain(el, group, score, matched_on),
                    matched_on=matched_on,
                )
            )
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[: max(0, max_results)]

    def best(
        self,
        snapshot: PageSnapshot,
        group_name: str,
        *,
        role_filter: str | None = None,
        min_score: float | None = None,
    ) -> Candidate | None:
        c = self.candidates(
            snapshot, group_name,
            role_filter=role_filter, min_score=min_score, max_results=1,
        )
        return c[0] if c else None

    # ----------------------------------------------------------- planners
    def build_click_plan(
        self,
        snapshot: PageSnapshot,
        group_name: str,
    ) -> HeuristicPlan:
        """Build a click plan for the best match of ``group_name``."""
        candidates = self.candidates(snapshot, group_name, max_results=5)
        plan = HeuristicPlan(goal=group_name)
        if not candidates:
            plan.notes.append(f"no candidate found for {group_name!r}")
            plan.steps.append(
                ActionStep(
                    action=ActionType.NOOP,
                    intent=group_name,
                    rationale=f"heuristics: no element for {group_name!r}",
                )
            )
            return plan
        best = candidates[0]
        plan.steps.append(
            ActionStep(
                action=ActionType.CLICK,
                intent=group_name,
                selector=best.selector,
                confidence=best.score,
                rationale=best.rationale,
            )
        )
        plan.confidence = best.score
        plan.alternatives = candidates[1:]
        return plan

    def build_form_plan(
        self,
        snapshot: PageSnapshot,
        *,
        fields: dict[str, str],
        submit_group: str = "submit",
    ) -> HeuristicPlan:
        """Build a fill-the-form-and-submit plan deterministically.

        ``fields`` is ``{group_name: value}`` — e.g.
        ``{"username_field": "u", "password_field": "p"}``. Missing
        groups are skipped silently with a note. The submit step uses
        ``submit_group``.
        """
        plan = HeuristicPlan(goal=f"fill_form_then_{submit_group}")
        confidences: list[float] = []

        for group_name, value in fields.items():
            best = self.best(
                snapshot, group_name, role_filter="textbox",
            )
            if best is None:
                # Some fields are required (password); some are optional
                # (confirm_password_field). Optional fields use a "?" suffix
                # in goal templates — heuristics treat absence as a note,
                # never as a hard error.
                plan.notes.append(f"field not found: {group_name}")
                plan.steps.append(
                    ActionStep(
                        action=ActionType.NOOP,
                        intent=group_name,
                        optional=group_name.endswith("?")
                        or group_name == "confirm_password_field",
                        rationale=f"heuristics: no field for {group_name!r}",
                    )
                )
                continue
            confidences.append(best.score)
            plan.steps.append(
                ActionStep(
                    action=ActionType.FILL,
                    intent=group_name,
                    selector=best.selector,
                    value=value,
                    confidence=best.score,
                    rationale=best.rationale,
                )
            )

        # Submit
        submit = self.best(snapshot, submit_group)
        if submit is None:
            plan.notes.append(f"no submit button for {submit_group!r}")
            plan.steps.append(
                ActionStep(
                    action=ActionType.NOOP,
                    intent=submit_group,
                    rationale="heuristics: no submit button",
                )
            )
        else:
            confidences.append(submit.score)
            plan.steps.append(
                ActionStep(
                    action=ActionType.CLICK,
                    intent=submit_group,
                    selector=submit.selector,
                    confidence=submit.score,
                    rationale=submit.rationale,
                )
            )
            plan.alternatives.append(submit)

        plan.confidence = (
            sum(confidences) / len(confidences) if confidences else 0.0
        )
        return plan

    # ----------------------------------------------------------- scoring
    def _score(
        self, el: DetectedElement, group: IntentGroup,
    ) -> tuple[float, str]:
        """Return (score, matched_on) for an element vs an intent group.

        Combines four signals:
          1. Best base-intent match score from the perception layer.
          2. Direct keyword substring match against text/aria/placeholder/name.
          3. Role agreement bonus.
          4. Visibility / size heuristic.
        """
        score = 0.0
        matched_on = ""

        # 1. Base-intent match (already scored by IntentMatcher)
        for m in el.intents:
            if m.intent in group.base_intents and m.score > score:
                score = m.score
                matched_on = "intent"

        # 2. Keyword fallback against multiple text sources
        text_sources = (
            ("text", _norm(el.text)),
            ("aria", _norm(el.aria)),
            ("placeholder", _norm(el.placeholder)),
            ("name", _norm(el.name)),
            ("href", _norm(el.href)),
        )
        for src_name, hay in text_sources:
            if not hay:
                continue
            for kw in group.keywords:
                kw_norm = _norm(kw)
                if not kw_norm:
                    continue
                # Whole-word containment is the cheapest reliable signal.
                if _contains_word_or_phrase(hay, kw_norm):
                    # Confidence scales with how much of the haystack the
                    # keyword covers (a button labeled exactly "Claim" is
                    # more confident than one labeled
                    # "Tap to redeem your seasonal claim").
                    ratio = len(kw_norm) / max(len(hay), 1)
                    s = 0.55 + 0.35 * min(ratio, 1.0)
                    if s > score:
                        score = s
                        matched_on = src_name

        # 3. Role agreement (small additive bonus)
        if group.preferred_roles and el.role in group.preferred_roles and score > 0:
            score = min(1.0, score + 0.08)

        # 4. Visibility / sane size (penalize hidden-but-rendered elements)
        if not el.visible:
            score *= 0.0  # never act on invisible elements
        elif el.bbox[2] < 4 or el.bbox[3] < 4:
            score *= 0.7  # tiny elements are usually icons/decoration

        return score, matched_on or "role"

    def _explain(
        self,
        el: DetectedElement,
        group: IntentGroup,
        score: float,
        matched_on: str,
    ) -> str:
        bits = [f"group={group.name}", f"matched_on={matched_on}",
                f"score={score:.2f}"]
        if el.text:
            bits.append(f"text={el.text[:40]!r}")
        elif el.aria:
            bits.append(f"aria={el.aria[:40]!r}")
        elif el.placeholder:
            bits.append(f"placeholder={el.placeholder[:40]!r}")
        return ", ".join(bits)


# ----------------------------------------------------------- text helpers
_WS_RE = re.compile(r"\s+")


def _norm(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", str(text).strip().lower())


def _contains_word_or_phrase(haystack: str, needle: str) -> bool:
    """Word-aware substring test.

    Plain ``in`` is too eager — "join" would match "joint", and
    "bet" would match "better". A whole-word boundary check fixes
    that for single-word needles and is a no-op for multi-word
    phrases (where the phrase boundaries already disambiguate).
    """
    if " " in needle:
        return needle in haystack
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None
