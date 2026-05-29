"""Learn-once / replay-many goal templates.

The user's complaint about the previous architecture was that every
single account paid the full AI tax. The fix is templates: when
account #1 successfully completes a goal on a host, the engine
records the action sequence as a template; when account #2+ shows
up, the engine checks for a matching template and *replays it
deterministically*, only falling back to the AI brain if a step
fails verification.

This module implements the storage half of that pipeline. The
deterministic engine (``deterministic_engine.py``) is the consumer
that decides when to record, when to replay, and when to give up.

What's recorded
===============

Each template is a sequence of :class:`TemplateStep` records — the
same action shape the executor already understands, plus a small
amount of *verification metadata* (expected URL fragment, expected
body text, observed step duration). Replaying a step means executing
the action, then checking that the verification metadata still
holds. If it doesn't, the engine treats the template as stale and
falls back.

Multiple versions per goal
==========================

Sites change. We keep a small list of templates per ``(host, goal)``
pair, with the most successful one tried first. When account #2
successfully replays template ``v1``, its success counter goes up;
when account #5's replay fails, we record a failure and may decide
to record a new ``v2`` when the next AI-driven run succeeds.

Layout on disk
==============

::

    data/learning/templates/
        example.com.json          # all templates for example.com
        rewards.gov.json
        ...

Each file is a JSON object keyed by goal:

.. code-block:: json

    {
      "host": "example.com",
      "goals": {
        "register": [
          {"version": 1, "steps": [...], "success_count": 12, ...},
          {"version": 2, "steps": [...], "success_count": 3, ...}
        ],
        "login": [...]
      }
    }

The format is reviewer-friendly so operators can sanity-check what
the agent learned and prune problematic templates by hand.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from automation.ai.planner import ActionStep, ActionType

log = logging.getLogger(__name__)


# ---------------------------------------------------------- step
@dataclass(slots=True)
class TemplateStep:
    """One recorded action + its post-action verification metadata.

    Stored as plain data so a template file is reviewable and
    diffable. Conversion to/from :class:`ActionStep` happens at
    replay time.
    """

    action: str  # matches automation.ai.planner.ActionType.value
    selector: str | None = None
    value: str | None = None  # secrets are *not* recorded; see commit logic
    intent: str | None = None
    timeout_ms: int = 10_000
    optional: bool = False

    # Post-action verification (best-effort signals captured on success)
    expected_url_fragment: str = ""
    expected_text_fragment: str = ""
    duration_ms_observed: int = 0

    # Free-form rationale kept from the original execution so the
    # reasoning panel can explain replays without re-running heuristics.
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "selector": self.selector,
            "value": self.value,
            "intent": self.intent,
            "timeout_ms": self.timeout_ms,
            "optional": self.optional,
            "expected_url_fragment": self.expected_url_fragment,
            "expected_text_fragment": self.expected_text_fragment,
            "duration_ms_observed": self.duration_ms_observed,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TemplateStep":
        return cls(
            action=str(data.get("action", "noop")),
            selector=data.get("selector"),
            value=data.get("value"),
            intent=data.get("intent"),
            timeout_ms=int(data.get("timeout_ms", 10_000)),
            optional=bool(data.get("optional", False)),
            expected_url_fragment=str(data.get("expected_url_fragment", "")),
            expected_text_fragment=str(data.get("expected_text_fragment", "")),
            duration_ms_observed=int(data.get("duration_ms_observed", 0)),
            rationale=str(data.get("rationale", "")),
        )

    def to_action_step(self, *, value_override: str | None = None) -> ActionStep:
        """Turn this template step into the executor's :class:`ActionStep`.

        ``value_override`` lets the caller plug per-account credentials
        in for fields whose original value was redacted at record time.
        """
        try:
            action_enum = ActionType(self.action)
        except ValueError:
            action_enum = ActionType.NOOP
        return ActionStep(
            action=action_enum,
            intent=self.intent,
            selector=self.selector,
            value=value_override if value_override is not None else self.value,
            confidence=0.9,  # replay is high-confidence by definition
            rationale=self.rationale or "template replay",
            timeout_ms=self.timeout_ms,
            optional=self.optional,
            metadata={"replay": True},
        )


# ---------------------------------------------------------- template
@dataclass(slots=True)
class SiteTemplate:
    """A complete recorded path for one ``(host, goal)`` pair."""

    host: str
    goal: str
    version: int
    steps: list[TemplateStep] = field(default_factory=list)
    initial_url_pattern: str = ""  # regex, applied to URL when matching
    initial_page_signature: str = ""  # exact match preferred
    expected_final_url_pattern: str = ""
    expected_success_signals: list[dict[str, Any]] = field(default_factory=list)

    success_count: int = 0
    fail_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0

    @property
    def confidence(self) -> float:
        """Smoothed success rate; new templates get a small bonus."""
        total = self.success_count + self.fail_count
        if total <= 0:
            # Never replayed. Still preferable to "no template at all"
            # but we want freshly-recorded templates ranked behind
            # battle-tested ones — give them a moderate base confidence.
            return 0.55
        return self.success_count / (total + 1.0)

    @property
    def stale(self) -> bool:
        """Templates with more failures than successes are considered stale."""
        total = self.success_count + self.fail_count
        if total < 3:
            return False
        return self.fail_count > self.success_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "goal": self.goal,
            "version": self.version,
            "steps": [s.to_dict() for s in self.steps],
            "initial_url_pattern": self.initial_url_pattern,
            "initial_page_signature": self.initial_page_signature,
            "expected_final_url_pattern": self.expected_final_url_pattern,
            "expected_success_signals": list(self.expected_success_signals),
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "confidence": round(self.confidence, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SiteTemplate":
        return cls(
            host=str(data.get("host", "")),
            goal=str(data.get("goal", "")),
            version=int(data.get("version", 1)),
            steps=[TemplateStep.from_dict(s) for s in data.get("steps") or []],
            initial_url_pattern=str(data.get("initial_url_pattern", "")),
            initial_page_signature=str(data.get("initial_page_signature", "")),
            expected_final_url_pattern=str(data.get("expected_final_url_pattern", "")),
            expected_success_signals=list(data.get("expected_success_signals") or []),
            success_count=int(data.get("success_count", 0)),
            fail_count=int(data.get("fail_count", 0)),
            created_at=float(data.get("created_at", time.time())),
            last_used=float(data.get("last_used", 0.0)),
        )

    def matches(self, *, url: str, signature: str = "") -> float:
        """Return a 0..1 score for how well this template fits the page.

        Score blends URL pattern match (regex) and page signature
        equality. Either signal alone is enough to consider replay; both
        is best.
        """
        url_score = 0.0
        if self.initial_url_pattern:
            try:
                if re.search(self.initial_url_pattern, url):
                    url_score = 1.0
            except re.error:
                url_score = 0.0
        sig_score = 1.0 if (
            signature
            and self.initial_page_signature
            and signature == self.initial_page_signature
        ) else 0.0
        if url_score and sig_score:
            return 1.0
        if url_score:
            return 0.85
        if sig_score:
            return 0.7
        return 0.0


# ---------------------------------------------------------- recorder
@dataclass(slots=True)
class TemplateRecording:
    """An in-progress recording. Owned by the deterministic engine.

    The engine calls :py:meth:`add_step` for every successful step and
    :py:meth:`abort` if anything fails. On goal success the engine
    hands the recording to :py:meth:`TemplateStore.commit` which turns
    it into a :class:`SiteTemplate`.
    """

    host: str
    goal: str
    initial_url: str = ""
    initial_signature: str = ""
    steps: list[TemplateStep] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    aborted: bool = False

    # Fields filled in just before commit:
    final_url_pattern: str = ""
    success_signals: list[dict[str, Any]] = field(default_factory=list)

    def add_step(self, step: TemplateStep) -> None:
        if self.aborted:
            return
        self.steps.append(step)

    def abort(self, reason: str = "") -> None:
        self.aborted = True
        if reason:
            log.debug(
                "template recording aborted host=%s goal=%s reason=%s",
                self.host, self.goal, reason,
            )


# ---------------------------------------------------------- store
class TemplateStore:
    """File-backed catalog of templates, one JSON per host."""

    def __init__(
        self,
        root: str | Path = "data/learning/templates",
        *,
        max_versions_per_goal: int = 4,
        autoflush: bool = False,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_versions_per_goal = int(max_versions_per_goal)
        self.autoflush = bool(autoflush)
        self._cache: dict[str, dict[str, list[SiteTemplate]]] = {}
        self._dirty: set[str] = set()

    # --------------------------------------------------------- record / commit
    def start_recording(
        self,
        *,
        host: str,
        goal: str,
        initial_url: str = "",
        initial_signature: str = "",
    ) -> TemplateRecording:
        """Begin a new recording — caller stores the handle and feeds it."""
        return TemplateRecording(
            host=_canonical_host(host),
            goal=str(goal),
            initial_url=initial_url,
            initial_signature=initial_signature,
        )

    def commit(self, recording: TemplateRecording) -> SiteTemplate | None:
        """Turn a recording into a stored template.

        Returns ``None`` if the recording was aborted, contained no
        usable steps, or has already been superseded by an identical
        template (we don't store duplicates).
        """
        if recording.aborted:
            return None
        usable_steps = [s for s in recording.steps if s.action != "noop"]
        if not usable_steps:
            return None
        host = _canonical_host(recording.host)
        goal_bucket = self._bucket(host, recording.goal)
        # Promote a duplicate (same step shape) to "success" instead of
        # creating a copy. This keeps the catalog tight on repeat runs.
        sig = _step_signature(usable_steps)
        for existing in goal_bucket:
            if _step_signature(existing.steps) == sig:
                existing.success_count += 1
                existing.last_used = time.time()
                self._dirty.add(host)
                if self.autoflush:
                    self.flush(host)
                return existing
        # New template; assign next version number.
        next_version = (
            max((t.version for t in goal_bucket), default=0) + 1
        )
        template = SiteTemplate(
            host=host,
            goal=recording.goal,
            version=next_version,
            steps=usable_steps,
            initial_url_pattern=_url_to_pattern(recording.initial_url),
            initial_page_signature=recording.initial_signature,
            expected_final_url_pattern=recording.final_url_pattern,
            expected_success_signals=list(recording.success_signals),
            last_used=time.time(),
        )
        goal_bucket.append(template)
        # Cap versions: keep the most-confident ones, drop the rest.
        goal_bucket.sort(
            key=lambda t: (t.confidence, t.success_count, t.last_used),
            reverse=True,
        )
        del goal_bucket[self.max_versions_per_goal:]
        self._dirty.add(host)
        if self.autoflush:
            self.flush(host)
        return template

    # ------------------------------------------------------------- lookup
    def find_template(
        self,
        *,
        host: str,
        goal: str,
        url: str = "",
        signature: str = "",
        min_match: float = 0.5,
    ) -> SiteTemplate | None:
        """Return the best non-stale template for the (host, goal, page)."""
        host = _canonical_host(host)
        bucket = self._bucket(host, goal)
        best: SiteTemplate | None = None
        best_score = 0.0
        for tpl in bucket:
            if tpl.stale:
                continue
            score = tpl.matches(url=url, signature=signature)
            if score < min_match:
                continue
            # Combine match score with template confidence so a mediocre
            # match against a battle-tested template can still beat a
            # perfect match against a flaky one.
            combined = score * 0.6 + tpl.confidence * 0.4
            if combined > best_score:
                best = tpl
                best_score = combined
        return best

    def list_templates(
        self, *, host: str, goal: str | None = None,
    ) -> list[SiteTemplate]:
        """Return all templates for a host (optionally filtered by goal)."""
        host = _canonical_host(host)
        host_dict = self._load_host(host)
        if goal is not None:
            return list(host_dict.get(goal, []))
        return [t for tpls in host_dict.values() for t in tpls]

    # ------------------------------------------------------------- counters
    def record_replay_result(
        self,
        template: SiteTemplate,
        *,
        success: bool,
    ) -> None:
        """Update success/fail counters after a replay attempt."""
        if success:
            template.success_count += 1
        else:
            template.fail_count += 1
        template.last_used = time.time()
        self._dirty.add(template.host)
        if self.autoflush:
            self.flush(template.host)

    # ------------------------------------------------------------- I/O
    def flush(self, host: str) -> bool:
        host = _canonical_host(host)
        if host not in self._dirty:
            return False
        host_dict = self._cache.get(host)
        if not host_dict:
            self._dirty.discard(host)
            return False
        payload = {
            "host": host,
            "goals": {
                goal: [tpl.to_dict() for tpl in tpls]
                for goal, tpls in sorted(host_dict.items())
                if tpls
            },
        }
        try:
            self._path_for(host).write_text(
                json.dumps(payload, indent=2, default=str),
            )
            self._dirty.discard(host)
            return True
        except OSError:  # noqa: BLE001
            log.warning("template store: write failed for %s", host, exc_info=True)
            return False

    def flush_all(self) -> int:
        return sum(1 for h in list(self._dirty) if self.flush(h))

    # ------------------------------------------------------------- private
    def _bucket(self, host: str, goal: str) -> list[SiteTemplate]:
        return self._load_host(host).setdefault(goal, [])

    def _load_host(self, host: str) -> dict[str, list[SiteTemplate]]:
        host = _canonical_host(host)
        if host in self._cache:
            return self._cache[host]
        path = self._path_for(host)
        loaded: dict[str, list[SiteTemplate]] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                for goal, tpls_raw in (raw.get("goals") or {}).items():
                    loaded[goal] = [
                        SiteTemplate.from_dict({**t, "host": host, "goal": goal})
                        for t in tpls_raw or []
                    ]
            except (OSError, json.JSONDecodeError):  # noqa: BLE001
                log.warning("template store: bad file %s", path, exc_info=True)
        self._cache[host] = loaded
        return loaded

    def _path_for(self, host: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", host)[:128] or "unknown"
        return self.root / f"{safe}.json"


# ---------------------------------------------------------- helpers
def _canonical_host(host: str) -> str:
    """Lowercase + strip-port; URL → host."""
    raw = (host or "").strip().lower()
    if not raw:
        return "unknown"
    if "://" in raw:
        try:
            return urlsplit(raw).hostname or "unknown"
        except ValueError:
            return "unknown"
    # Strip port if present.
    return raw.split(":", 1)[0]


def _url_to_pattern(url: str) -> str:
    """Build a tolerant regex pattern from a concrete URL.

    The goal is to match the *path* on the same host while ignoring
    query/fragment and numeric IDs ("/users/42/profile" →
    ``/users/\\d+/profile``). This is far more useful for replay
    matching than a literal URL comparison.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    path = parts.path or "/"
    # Build the pattern in two passes.
    #   1. ``re.escape`` everything so static path text is matched literally.
    #   2. Replace every escaped numeric path segment (``/\d+`` after
    #      ``re.escape`` reads as ``\d+`` because ``re.escape("\d")``
    #      is ``\\d``…) with the unescaped ``/\d+`` so IDs match.
    escaped = re.escape(path)
    # ``re.escape`` turns every digit run "42" into "42" (digits aren't
    # escaped) — so look for the original numeric segments and substitute.
    pattern = re.sub(r"/\d+(?=/|$)", r"/\\d+", escaped)
    return f"^.*{pattern}(\\?|$|/)"


def _step_signature(steps: list[TemplateStep]) -> str:
    """Compact fingerprint used to detect duplicate templates on commit."""
    return "|".join(
        f"{s.action}:{s.intent or ''}:{s.selector or ''}" for s in steps
    )
