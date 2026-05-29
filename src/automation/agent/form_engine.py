"""Dedicated Form Engine: discover → classify → map → fill → verify.

The framework already had two strong primitives:

* :mod:`automation.agent.heuristics` — semantic intent group detection
  over a :class:`PageSnapshot`, e.g. "this textbox is the password
  field". Built for plan-time reasoning.
* :mod:`automation.agent.form_filler` — five-strategy filling with
  read-back verification, plus pre-submit validation. Built for
  step-time execution.

What was missing was the *orchestrator* between them. The brain or
the loop would ask the heuristics "where is the email field?" once,
then ask the form filler to type into it once, but the two were
never composed into a single "fill this form, completely, and tell
me whether every value persisted" call. The result: the framework
worked great when there were three obvious fields with obvious
labels; it struggled when a registration page also asked for an
invitation code, an OTP, or a phone number with a country selector.

This module is that orchestrator. One call discovers every
fillable input on the page, classifies each by intent group, maps
the operator-supplied data dictionary onto the classified fields,
fills them with the multi-strategy ladder, reads each value back to
verify it persisted, and returns a structured report.

Supported classifications (each backed by an ``IntentGroup`` plus a
small set of regex/keyword fallbacks for the long tail):

  * ``email`` / ``username`` / ``phone``
  * ``password`` / ``confirm_password``
  * ``otp`` — one-time codes, six-digit verification, 2FA prompts
  * ``invitation_code`` / ``referral_code`` / ``promo_code``
  * ``search``
  * ``custom`` — anything that has a ``name=``/``id=``/``placeholder=``
    matching a key the caller supplied via ``custom_field_keys``.

The classifier is intentionally conservative: when no group matches
above ``min_field_score`` the field is left unclassified and only
filled if the caller passes a key whose normalized form matches
the field's name/id/placeholder. That keeps the engine from
hallucinating mappings on noisy pages.

Threading
=========

``FormEngine`` is stateless; the per-call state lives in the
returned :class:`FormFillReport`. One instance can be shared
across runs.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from automation.agent.form_filler import FillResult, FormFiller
from automation.agent.heuristics import Heuristics
from automation.ai.perception import DetectedElement, PageSnapshot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- types
#: Canonical names returned by :meth:`FormEngine.classify`. The set is
#: deliberately small — these are the buckets the framework actually
#: knows how to *fill* without operator help; everything else falls
#: into ``custom`` or ``unknown``.
KNOWN_FIELDS: tuple[str, ...] = (
    "email",
    "username",
    "phone",
    "password",
    "confirm_password",
    "otp",
    "invitation_code",
    "search",
)


@dataclass(slots=True, frozen=True)
class _ClassifierRule:
    """Internal rule for the long-tail classifier.

    Each rule fires when *any* of its patterns match the haystack
    (the concatenation of the field's text labels: name, id, aria,
    placeholder, label-for, autocomplete). Rules are tried in
    priority order; the first match wins.
    """

    name: str
    patterns: tuple[re.Pattern[str], ...]
    require_password_type: bool = False
    forbid_password_type: bool = False
    description: str = ""


# Compiled once at import time. The patterns favour whole-word matches
# so "code" doesn't match "codepoint" but does match "verification code".
def _rx(*words: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(rf"(?<![a-z]){w}(?![a-z])", re.IGNORECASE) for w in words)


_CLASSIFIER_RULES: tuple[_ClassifierRule, ...] = (
    # OTP / verification codes — must NOT be a password input (those
    # are sometimes also 6 digits, but they're handled by password rule
    # below). The patterns include both "OTP" and "verification code"
    # since different sites use either.
    _ClassifierRule(
        name="otp",
        patterns=_rx(
            "otp", "one[\\s\\-]?time", "verif(?:y|ication)[\\s\\-]?code",
            "auth[\\s\\-]?code", "2fa", "two[\\s\\-]?factor",
            "security[\\s\\-]?code", "sms[\\s\\-]?code",
        ),
        forbid_password_type=True,
        description="One-time code / OTP / 2FA",
    ),
    _ClassifierRule(
        name="invitation_code",
        patterns=_rx(
            "invitation", "invite[\\s\\-]?code", "invite",
            "referral", "referrer", "referral[\\s\\-]?code",
            "promo[\\s\\-]?code", "promo", "coupon", "voucher",
            "promotion[\\s\\-]?code", "affiliate[\\s\\-]?code",
        ),
        forbid_password_type=True,
        description="Invitation / referral / promo code",
    ),
    # Confirm-password must be checked *before* password so a
    # "confirm-password" field doesn't get classified as "password"
    # by the more general pattern.
    _ClassifierRule(
        name="confirm_password",
        patterns=_rx(
            "confirm[\\s\\-]?password", "repeat[\\s\\-]?password",
            "verify[\\s\\-]?password", "password[\\s\\-]?again",
            "retype[\\s\\-]?password",
        ),
        require_password_type=True,
        description="Password confirmation",
    ),
    _ClassifierRule(
        name="password",
        patterns=_rx("password", "passcode", "passphrase"),
        require_password_type=True,
        description="Password",
    ),
    _ClassifierRule(
        name="email",
        patterns=_rx("email", "e\\-?mail"),
        forbid_password_type=True,
        description="Email address",
    ),
    _ClassifierRule(
        name="phone",
        patterns=_rx(
            "phone", "mobile", "tel", "cell", "msisdn",
            "phone[\\s\\-]?number", "mobile[\\s\\-]?number",
        ),
        forbid_password_type=True,
        description="Phone / mobile number",
    ),
    _ClassifierRule(
        name="username",
        patterns=_rx(
            "user[\\s\\-]?name", "userid", "user[\\s\\-]?id",
            "login[\\s\\-]?id", "member[\\s\\-]?id", "handle",
            "nickname", "screen[\\s\\-]?name",
        ),
        forbid_password_type=True,
        description="Username / login id",
    ),
    _ClassifierRule(
        name="search",
        patterns=_rx("search", "query", "find", "lookup"),
        forbid_password_type=True,
        description="Search input",
    ),
)


@dataclass(slots=True)
class ClassifiedField:
    """One discovered + classified input.

    ``name`` is the canonical bucket from :data:`KNOWN_FIELDS` plus
    ``"custom"`` (matched by caller-supplied key) and ``"unknown"``
    (no match, will be skipped during fill). ``score`` is the
    Heuristics confidence when the classification came from there;
    for the long-tail classifier it's a flat value reflecting how
    specific the rule was.
    """

    element: DetectedElement
    selector: str
    name: str
    score: float
    matched_on: str  # "intent" | "rule:<rule_name>" | "custom:<key>" | "unknown"
    custom_key: str | None = None  # set when name == "custom"
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "name": self.name,
            "score": round(self.score, 3),
            "matched_on": self.matched_on,
            "custom_key": self.custom_key,
            "rationale": self.rationale,
            "tag": self.element.tag,
            "type": self.element.type,
            "placeholder": self.element.placeholder[:60],
            "aria": self.element.aria[:60],
            "name_attr": self.element.name[:60],
            "id_attr": self.element.id[:60],
        }


@dataclass(slots=True)
class FilledField:
    """One field that the engine attempted to fill.

    Always present — even when the value mapping was missing — so
    operators can see the full picture in the report. ``mapped_value``
    is empty when the caller had no value for the field's classified
    name (and the engine therefore skipped it).
    """

    field: ClassifiedField
    mapped_value: str
    fill: FillResult | None = None
    skipped_reason: str | None = None

    @property
    def success(self) -> bool:
        return self.fill is not None and self.fill.success

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field.to_dict(),
            "mapped_value_present": bool(self.mapped_value),
            "fill": self.fill.to_dict() if self.fill else None,
            "skipped_reason": self.skipped_reason,
            "success": self.success,
        }


@dataclass(slots=True)
class FormFillReport:
    """End-to-end outcome of :py:meth:`FormEngine.fill_form`."""

    discovered: list[ClassifiedField] = field(default_factory=list)
    filled: list[FilledField] = field(default_factory=list)
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def required_failures(self) -> list[FilledField]:
        """Fields the caller asked for but the engine couldn't fill."""
        return [
            f for f in self.filled
            if f.mapped_value and not f.success and f.skipped_reason is None
        ]

    @property
    def success(self) -> bool:
        """All fields the caller had values for were filled successfully."""
        return not self.required_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "duration_ms": self.duration_ms,
            "discovered": [c.to_dict() for c in self.discovered],
            "filled": [f.to_dict() for f in self.filled],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------- engine
class FormEngine:
    """Discover + classify + map + fill + verify, in one call.

    The engine composes the existing :class:`Heuristics` (for
    high-confidence intent matches) with a small long-tail rule
    classifier (for OTP / invitation / confirm_password / search
    that the heuristics groups don't always catch by themselves).
    Filling is delegated to :class:`FormFiller` so the multi-
    strategy ladder remains the single source of truth for typing.

    Parameters
    ----------
    heuristics:
        Re-use the agent's instance to keep group definitions
        consistent. A new one is built when ``None``.
    form_filler:
        Likewise; reuse for backpressure / settling settings.
    min_field_score:
        Minimum confidence for a Heuristics intent-group match to
        win classification. Below this we fall back to the rule
        classifier and finally to "unknown".
    """

    def __init__(
        self,
        *,
        heuristics: Heuristics | None = None,
        form_filler: FormFiller | None = None,
        min_field_score: float = 0.45,
    ) -> None:
        self.heuristics = heuristics or Heuristics()
        self.form_filler = form_filler or FormFiller()
        self.min_field_score = float(min_field_score)

    # --------------------------------------------------------- discovery
    def discover(self, snapshot: PageSnapshot) -> list[DetectedElement]:
        """Return every visible, fillable input on the page.

        "Fillable" means: textbox-like role/tag (input/textarea/role
        textbox/searchbox), visible, and not disabled. Hidden inputs
        and submit buttons are deliberately excluded.
        """
        out: list[DetectedElement] = []
        for el in snapshot.elements:
            if not el.visible:
                continue
            if not _is_fillable(el):
                continue
            out.append(el)
        return out

    # --------------------------------------------------------- classification
    def classify(
        self,
        snapshot: PageSnapshot,
        *,
        custom_field_keys: Iterable[str] = (),
    ) -> list[ClassifiedField]:
        """Discover every input and tag it with a canonical name.

        ``custom_field_keys`` is the set of keys from the caller's
        ``inputs`` dict that aren't standard. Each becomes a possible
        classification target — for example, passing ``"otp"`` lets
        a field literally named ``otp`` be filled even if the rule
        classifier missed it.
        """
        elements = self.discover(snapshot)
        custom_keys = tuple(_normalize_key(k) for k in custom_field_keys if k)
        results: list[ClassifiedField] = []

        for el in elements:
            classified = self._classify_one(el, custom_keys=custom_keys)
            results.append(classified)

        # If the page has only one password-type input we treat it
        # as ``password`` (not ``confirm_password``) regardless of
        # label, since most one-password forms label the field with
        # whatever wording they please.
        password_count = sum(
            1 for c in results
            if c.element.type == "password" and c.name in ("password", "confirm_password")
        )
        if password_count == 1:
            for c in results:
                if c.element.type == "password" and c.name == "confirm_password":
                    c.name = "password"
                    c.matched_on = c.matched_on + "+single-password-fallback"
                    break

        return results

    # --------------------------------------------------------- public: fill
    async def fill_form(
        self,
        page: Any,
        snapshot: PageSnapshot,
        inputs: dict[str, str],
        *,
        timeout_ms: int = 30_000,
    ) -> FormFillReport:
        """Run the full discover → classify → map → fill → verify pipeline.

        Returns a :class:`FormFillReport` whose ``success`` is true
        iff every input the caller had a value for was filled and
        the read-back matched.

        Notes
        -----
        * Fields the engine classifies as ``password`` are filled
          first within their group (so confirm_password reads the
          same value reliably even if the SPA mirrors them).
        * The pre-submit validation step is *not* run here — the
          caller's loop does that just before clicking the submit
          button so any field that re-rendered between fill and
          submit is caught.
        """
        started = time.time()
        report = FormFillReport()
        normalized_inputs = {_normalize_key(k): v for k, v in (inputs or {}).items()}
        report.discovered = self.classify(
            snapshot, custom_field_keys=normalized_inputs.keys(),
        )

        # Fill order: password before confirm_password before everything else,
        # so the SPA has the canonical password before the confirmation
        # listener fires. Within categories we preserve discovery order.
        order = {
            "password": 0,
            "confirm_password": 1,
            "email": 2,
            "username": 2,
            "phone": 2,
            "otp": 3,
            "invitation_code": 3,
            "search": 4,
            "custom": 5,
            "unknown": 6,
        }
        ordered = sorted(
            report.discovered,
            key=lambda c: (order.get(c.name, 9), report.discovered.index(c)),
        )

        for cf in ordered:
            value = self._map_value(cf, normalized_inputs)
            if not value:
                report.filled.append(FilledField(
                    field=cf, mapped_value="",
                    skipped_reason=f"no value for {cf.name}",
                ))
                continue
            try:
                fr = await self.form_filler.fill_field(
                    page, cf.selector, value, timeout_ms=timeout_ms,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                report.filled.append(FilledField(
                    field=cf, mapped_value=value,
                    fill=None,
                    skipped_reason=f"fill raised: {exc!r}",
                ))
                report.notes.append(f"fill raised on {cf.selector}: {exc!r}")
                continue
            report.filled.append(FilledField(
                field=cf, mapped_value=value, fill=fr,
            ))
            if not fr.success:
                report.notes.append(
                    f"{cf.name}: failed after strategies "
                    f"{[s.value for s in fr.strategies_tried]}"
                )

        report.duration_ms = int((time.time() - started) * 1000)
        return report

    # --------------------------------------------------------- internals
    def _classify_one(
        self,
        el: DetectedElement,
        *,
        custom_keys: tuple[str, ...],
    ) -> ClassifiedField:
        """Classify a single element. Heuristics first, then rules, then custom."""
        # ---- 1. Heuristics intent groups ------------------------------
        # The Heuristics class already ranks candidates by group; we
        # invert that here, asking "which group does this element best
        # belong to?" by checking each canonical group against this
        # element's intents and labels.
        best_group_score = 0.0
        best_group_name: str | None = None
        best_match_source = ""
        # Intent-group → canonical name. Keep this in sync with the
        # heuristics ``INTENT_GROUPS`` keys.
        group_to_name = {
            "email_field": "email",
            "username_field": "username",
            "phone_field": "phone",
            "password_field": "password",
            "confirm_password_field": "confirm_password",
            "search": "search",
        }
        for intent_match in el.intents:
            for group_name, canonical in group_to_name.items():
                # The IntentMatcher labels with `password_field`,
                # `email_field`, etc. — those names line up with the
                # heuristics group names by construction.
                if intent_match.intent != group_name:
                    continue
                if intent_match.score > best_group_score:
                    best_group_score = float(intent_match.score)
                    best_group_name = canonical
                    best_match_source = f"intent:{intent_match.matched_on}"

        if best_group_name and best_group_score >= self.min_field_score:
            # Special-case: a non-password-type input claiming to be
            # ``password`` is suspect (probably a username field with
            # the word "password" near it). Fall through to the rule
            # classifier to see if a more specific bucket fits.
            if best_group_name in ("password", "confirm_password") and el.type != "password":
                pass  # let the rule layer try
            else:
                return ClassifiedField(
                    element=el, selector=el.selector,
                    name=best_group_name, score=best_group_score,
                    matched_on=best_match_source,
                    rationale=f"heuristic group {best_group_name} ({best_match_source})",
                )

        # ---- 2. Long-tail rule classifier -----------------------------
        haystack = _build_haystack(el)
        for rule in _CLASSIFIER_RULES:
            if rule.require_password_type and el.type != "password":
                continue
            if rule.forbid_password_type and el.type == "password":
                continue
            for pat in rule.patterns:
                if pat.search(haystack):
                    return ClassifiedField(
                        element=el, selector=el.selector,
                        name=rule.name,
                        # Rules are deterministic — we can't measure
                        # confidence the way Heuristics can, so we
                        # report a steady high value to signal
                        # "matched a specific pattern".
                        score=0.85,
                        matched_on=f"rule:{rule.name}",
                        rationale=rule.description or rule.name,
                    )

        # Type-only fallback for password inputs that neither
        # heuristics nor rules placed: a bare ``type="password"`` is
        # almost certainly a password regardless of label noise.
        if el.type == "password":
            return ClassifiedField(
                element=el, selector=el.selector,
                name="password", score=0.6,
                matched_on="type:password",
                rationale="password fallback by input type",
            )

        # ---- 3. Custom keys supplied by the caller --------------------
        for key in custom_keys:
            if not key:
                continue
            if _haystack_matches_key(haystack, key):
                return ClassifiedField(
                    element=el, selector=el.selector,
                    name="custom", score=0.55,
                    matched_on=f"custom:{key}",
                    custom_key=key,
                    rationale=f"matched caller key {key!r}",
                )

        # ---- 4. Heuristic match below threshold but better than nothing
        if best_group_name:
            return ClassifiedField(
                element=el, selector=el.selector,
                name=best_group_name, score=best_group_score,
                matched_on=f"intent:weak({best_match_source})",
                rationale=(
                    f"weak heuristic match for {best_group_name} "
                    f"({best_group_score:.2f})"
                ),
            )

        return ClassifiedField(
            element=el, selector=el.selector,
            name="unknown", score=0.0,
            matched_on="unknown",
            rationale="no classifier matched",
        )

    def _map_value(
        self,
        field: ClassifiedField,
        inputs: dict[str, str],
    ) -> str:
        """Pick the right value from the caller's inputs for this field.

        The mapping is intentionally generous so the same dict can
        drive register / login / change_password / OTP-confirm flows
        without rewriting it per goal.
        """
        if field.name == "custom" and field.custom_key:
            return inputs.get(field.custom_key, "") or ""

        if field.name == "email":
            return inputs.get("email") or inputs.get("username") or ""
        if field.name == "username":
            return (
                inputs.get("username")
                or inputs.get("email")
                or inputs.get("number")
                or inputs.get("phone")
                or ""
            )
        if field.name == "phone":
            return inputs.get("phone") or inputs.get("number") or ""
        if field.name == "password":
            return inputs.get("password", "") or ""
        if field.name == "confirm_password":
            return (
                inputs.get("confirm_password")
                or inputs.get("password")
                or ""
            )
        if field.name == "otp":
            return (
                inputs.get("otp")
                or inputs.get("code")
                or inputs.get("verification_code")
                or ""
            )
        if field.name == "invitation_code":
            return (
                inputs.get("invitation_code")
                or inputs.get("invite_code")
                or inputs.get("invite")
                or inputs.get("referral_code")
                or inputs.get("promo_code")
                or inputs.get("promo")
                or ""
            )
        if field.name == "search":
            return inputs.get("query") or inputs.get("search") or ""
        return inputs.get(field.name, "") or ""


# ---------------------------------------------------------------- helpers
def _is_fillable(el: DetectedElement) -> bool:
    """True when the element is something we can put a string into."""
    role = (el.role or "").lower()
    tag = (el.tag or "").lower()
    type_ = (el.type or "").lower()

    # Reject explicit non-fillable input types.
    if type_ in {
        "submit", "button", "reset", "image", "file", "hidden",
        "checkbox", "radio", "color", "range",
    }:
        return False
    if tag == "input":
        return True
    if tag == "textarea":
        return True
    if role in {"textbox", "searchbox"}:
        return True
    return False


def _build_haystack(el: DetectedElement) -> str:
    """Concatenate every text label associated with an element.

    Order is intentionally consistent so regex anchors like ``^``
    have predictable meaning if a future rule wants to use them.
    """
    parts = [
        el.name or "",
        el.id or "",
        el.placeholder or "",
        el.aria or "",
        el.text or "",
        el.type or "",
    ]
    return " | ".join(p for p in parts if p).lower()


def _normalize_key(key: str) -> str:
    """Lowercase + collapse whitespace/dashes/underscores to single ``_``."""
    if not key:
        return ""
    s = re.sub(r"[\s\-]+", "_", key.strip().lower())
    return re.sub(r"_+", "_", s)


def _haystack_matches_key(haystack: str, key: str) -> bool:
    """Whole-token containment check between a haystack and a normalized key.

    "promo_code" matches "your promo code here" but not "promotional".
    Multi-word keys are treated as a phrase; single-word keys use
    word-boundary regex.
    """
    if " " in key or "_" in key:
        # Treat the key as a phrase. Replace ``_`` with whitespace so
        # it matches "promo code" and "promo_code" alike.
        phrase = re.sub(r"[\s_]+", r"\\s*[\\s_-]?\\s*", re.escape(key))
        return re.search(rf"\b{phrase}\b", haystack) is not None
    return re.search(rf"\b{re.escape(key)}\b", haystack) is not None
