"""Execution templates: deterministic replay of a successful AI run.

The first time the agent succeeds at, say, ``register_account`` on
``example.com``, the action recorder produces a ledger of every navigate /
fill / click / wait / verify step it took. The :class:`TemplateRecorder`
distills that ledger into an :class:`ExecutionTemplate` — a small JSON
file with the *exact* selectors and *intelligent* wait conditions that
worked. Subsequent runs of the same goal on the same domain replay that
template directly via :class:`TemplateReplayer`, with no LLM calls and no
heuristic re-perception. AI is re-engaged only when replay fails.

File layout::

    data/execution_templates/<domain>/<workflow>_v<n>.json

The store keeps multiple versions per (domain, workflow) so a freshly-
recorded recovery template can supersede a stale one without losing the
old one (operators can ``/templates_exec rollback`` from Telegram).

Confidence comes from observed replay statistics:
``successes / max(1, attempts)``, with hard floor 0 and ceiling 1. Newly
recorded templates start at ``1.0`` and decay only when actual replays
fail. The ``min_replay_confidence`` threshold (default 0.6) is what the
:class:`AdaptiveExecutor` uses to decide whether replay is worth trying
at all.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from automation.agent.site_memory import domain_from_url

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
#  Action grammar — deterministic, no LLM, no fixed sleeps
# -----------------------------------------------------------------------------
class ActionKind(str, Enum):
    """The minimal set of actions a template can replay deterministically.

    ``WAIT`` is intentionally NOT a sleep — it stores a *condition name*
    that maps to one of the AdaptiveWaiter strategies (URL change,
    network idle, element visible, success message, …).
    """
    NAVIGATE = "navigate"
    FILL     = "fill"
    CLICK    = "click"
    SELECT   = "select"
    CHECK    = "check"
    PRESS    = "press"
    WAIT     = "wait"
    VERIFY   = "verify"


_ALLOWED_WAIT_CONDITIONS = {
    "page_load", "dom_stable", "network_idle", "url_change",
    "element_visible", "element_gone", "download_complete",
    "success_message", "no_loading", "navigation_complete",
}


@dataclass(slots=True)
class TemplateAction:
    """One deterministic step in an execution template.

    Attributes:
        kind:          Which primitive to invoke.
        selector:      CSS / XPath selector, when applicable.
        url_template:  For NAVIGATE; ``${var}`` placeholders are resolved
                       against the inputs dict at replay time.
        value_template: For FILL / SELECT / PRESS; same templating.
        wait_for:      Condition name (see ``_ALLOWED_WAIT_CONDITIONS``).
                       If set, the replayer waits for this condition AFTER
                       executing the action. Never a numeric delay.
        timeout_ms:    Per-step timeout, defaults to 30s.
        hints:         For VERIFY; substrings to look for in the page body
                       to confirm success.
        meta:          Free-form context (intent label, source step idx,
                       confidence score from the original AI plan).
    """
    kind: ActionKind
    selector: str | None = None
    url_template: str = ""
    value_template: str | None = None
    wait_for: str | None = None
    timeout_ms: int = 30_000
    hints: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "selector": self.selector,
            "url_template": self.url_template,
            "value_template": self.value_template,
            "wait_for": self.wait_for,
            "timeout_ms": self.timeout_ms,
            "hints": self.hints,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TemplateAction":
        return cls(
            kind=ActionKind(data["kind"]),
            selector=data.get("selector"),
            url_template=data.get("url_template", "") or "",
            value_template=data.get("value_template"),
            wait_for=data.get("wait_for"),
            timeout_ms=int(data.get("timeout_ms", 30_000)),
            hints=list(data.get("hints", []) or []),
            meta=dict(data.get("meta") or {}),
        )

    def referenced_inputs(self) -> set[str]:
        """Return the set of ``${var}`` names this action depends on."""
        names: set[str] = set()
        for src in (self.url_template, self.value_template or ""):
            if not src:
                continue
            for m in re.finditer(r"\$\{([a-zA-Z0-9_.]+)\}", src):
                # Only record top-level names, not dotted paths
                names.add(m.group(1).split(".", 1)[0])
        return names


# -----------------------------------------------------------------------------
#  Template + Stats
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class TemplateStats:
    """Replay-time observed stats. Confidence is derived from these."""
    replays_attempted: int = 0
    replays_succeeded: int = 0
    replays_failed: int = 0
    last_used: float = 0.0
    last_success: float = 0.0
    last_failure: float = 0.0
    avg_duration_ms: float = 0.0
    last_failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TemplateStats":
        return cls(
            replays_attempted=int(data.get("replays_attempted", 0)),
            replays_succeeded=int(data.get("replays_succeeded", 0)),
            replays_failed=int(data.get("replays_failed", 0)),
            last_used=float(data.get("last_used") or 0.0),
            last_success=float(data.get("last_success") or 0.0),
            last_failure=float(data.get("last_failure") or 0.0),
            avg_duration_ms=float(data.get("avg_duration_ms") or 0.0),
            last_failure_reason=str(data.get("last_failure_reason") or ""),
        )

    @property
    def success_rate(self) -> float:
        if self.replays_attempted <= 0:
            return 1.0  # never tried = full prior trust (it was learned from a real success)
        return self.replays_succeeded / self.replays_attempted


@dataclass(slots=True)
class ExecutionTemplate:
    """A versioned, deterministic replay of one workflow on one domain."""

    domain: str
    workflow: str
    version: int
    actions: list[TemplateAction] = field(default_factory=list)
    stats: TemplateStats = field(default_factory=TemplateStats)
    confidence: float = 1.0
    min_replay_confidence: float = 0.6
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    created_from_run_id: str = ""
    created_from_account_id: str = ""
    origin_url: str = ""
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------- I/O
    @property
    def filename(self) -> str:
        return f"{_safe(self.workflow)}_v{int(self.version)}.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "workflow": self.workflow,
            "version": self.version,
            "actions": [a.to_dict() for a in self.actions],
            "stats": self.stats.to_dict(),
            "confidence": self.confidence,
            "min_replay_confidence": self.min_replay_confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_from_run_id": self.created_from_run_id,
            "created_from_account_id": self.created_from_account_id,
            "origin_url": self.origin_url,
            "notes": self.notes,
            "input_keys_required": sorted(self.input_keys_required),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionTemplate":
        return cls(
            domain=data["domain"],
            workflow=data["workflow"],
            version=int(data.get("version", 1)),
            actions=[TemplateAction.from_dict(a) for a in data.get("actions", []) or []],
            stats=TemplateStats.from_dict(data.get("stats") or {}),
            confidence=float(data.get("confidence", 1.0)),
            min_replay_confidence=float(data.get("min_replay_confidence", 0.6)),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            created_from_run_id=str(data.get("created_from_run_id", "")),
            created_from_account_id=str(data.get("created_from_account_id", "")),
            origin_url=str(data.get("origin_url", "")),
            notes=list(data.get("notes", []) or []),
        )

    # ------------------------------------------------------------------- meta
    @property
    def input_keys_required(self) -> set[str]:
        names: set[str] = set()
        for a in self.actions:
            names.update(a.referenced_inputs())
        return names

    def is_eligible(self) -> bool:
        """Eligible to be picked by the AdaptiveExecutor for replay?"""
        if not self.actions:
            return False
        return self.confidence >= self.min_replay_confidence

    # ----------------------------------------------------------- bookkeeping
    def record_replay_outcome(
        self,
        *,
        success: bool,
        duration_ms: int,
        failure_reason: str = "",
    ) -> None:
        """Update stats + recompute confidence after a replay attempt."""
        s = self.stats
        s.replays_attempted += 1
        if success:
            s.replays_succeeded += 1
            s.last_success = time.time()
        else:
            s.replays_failed += 1
            s.last_failure = time.time()
            s.last_failure_reason = (failure_reason or "")[:300]
        s.last_used = time.time()

        # Exponential moving average for duration so old runs don't dominate
        if success:
            prev = float(s.avg_duration_ms or duration_ms)
            s.avg_duration_ms = round(0.7 * prev + 0.3 * duration_ms, 2)

        # Confidence: blend prior 1.0 with observed success rate
        # so a single failure doesn't immediately disqualify a 50-replay template.
        prior_weight = 3.0  # virtual successful replays
        succ = s.replays_succeeded + prior_weight
        att = s.replays_attempted + prior_weight
        self.confidence = round(min(1.0, succ / max(1.0, att)), 3)
        self.updated_at = time.time()


# -----------------------------------------------------------------------------
#  ExecutionTemplateStore — file-backed CRUD with versioning
# -----------------------------------------------------------------------------
class ExecutionTemplateStore:
    """Per-domain, per-workflow, multi-versioned template store.

    Layout::

        <root>/<domain>/<workflow>_v1.json
        <root>/<domain>/<workflow>_v2.json
        <root>/example.com/register_account_v3.json

    Domains are normalised the same way :func:`domain_from_url` normalises
    them in :mod:`automation.agent.site_memory`, so an
    ``ExecutionTemplate`` and its sibling ``SiteRecord`` share a key space.
    """

    def __init__(self, root: str | Path = "data/execution_templates") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ----------------------------------------------------------- key helpers
    @staticmethod
    def normalize_domain(url_or_domain: str) -> str:
        if not url_or_domain:
            return ""
        if "://" in url_or_domain:
            return domain_from_url(url_or_domain)
        # raw hostname
        host = url_or_domain.split("/", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        return _safe_domain(host)

    def _domain_dir(self, domain: str) -> Path:
        d = self.normalize_domain(domain)
        path = self.root / (d or "_unknown")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _path(self, template: ExecutionTemplate) -> Path:
        return self._domain_dir(template.domain) / template.filename

    # ----------------------------------------------------------------- CRUD
    def list_for_domain(self, domain: str) -> list[ExecutionTemplate]:
        """Return all templates for a domain, sorted by (workflow, version)."""
        out: list[ExecutionTemplate] = []
        ddir = self.root / (self.normalize_domain(domain) or "_unknown")
        if not ddir.exists():
            return out
        for f in sorted(ddir.iterdir()):
            if f.suffix.lower() != ".json":
                continue
            try:
                out.append(ExecutionTemplate.from_dict(
                    json.loads(f.read_text()),
                ))
            except (json.JSONDecodeError, KeyError):
                log.warning("execution template %s is corrupt; skipping", f)
        out.sort(key=lambda t: (t.workflow, t.version))
        return out

    def all_versions(
        self, domain: str, workflow: str,
    ) -> list[ExecutionTemplate]:
        """All versions for (domain, workflow), oldest first."""
        return [
            t for t in self.list_for_domain(domain)
            if t.workflow == workflow
        ]

    def find_best(
        self, domain: str, workflow: str,
        *,
        require_eligible: bool = True,
    ) -> ExecutionTemplate | None:
        """Pick the template most likely to succeed.

        Sort key = (eligible, confidence, version). Returns None when no
        candidate clears its own ``min_replay_confidence`` (unless
        ``require_eligible=False`` is passed for diagnostics).
        """
        candidates = self.all_versions(domain, workflow)
        if not candidates:
            return None
        if require_eligible:
            candidates = [c for c in candidates if c.is_eligible()]
            if not candidates:
                return None
        # Highest confidence first; tie-break on highest version (newer wins).
        candidates.sort(
            key=lambda t: (round(t.confidence, 3), t.version),
            reverse=True,
        )
        return candidates[0]

    def next_version(self, domain: str, workflow: str) -> int:
        existing = self.all_versions(domain, workflow)
        if not existing:
            return 1
        return max(t.version for t in existing) + 1

    def save(self, template: ExecutionTemplate) -> ExecutionTemplate:
        """Persist a template. Overwrites the file for the same version."""
        with self._lock:
            template.updated_at = time.time()
            path = self._path(template)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(template.to_dict(), indent=2, default=str),
            )
        return template

    def delete(self, domain: str, workflow: str, version: int) -> bool:
        with self._lock:
            ddir = self.root / (self.normalize_domain(domain) or "_unknown")
            f = ddir / f"{_safe(workflow)}_v{int(version)}.json"
            if not f.exists():
                return False
            f.unlink()
            return True

    def summary(self, *, limit: int = 200) -> dict[str, Any]:
        """Aggregate view used by /templates_exec on Telegram."""
        out: dict[str, Any] = {"domains": []}
        if not self.root.exists():
            return out
        for ddir in sorted(self.root.iterdir()):
            if not ddir.is_dir():
                continue
            templates = self.list_for_domain(ddir.name)
            if not templates:
                continue
            by_wf: dict[str, list[ExecutionTemplate]] = {}
            for t in templates:
                by_wf.setdefault(t.workflow, []).append(t)
            workflows = []
            for wf, versions in by_wf.items():
                versions.sort(key=lambda x: x.version, reverse=True)
                latest = versions[0]
                workflows.append({
                    "workflow": wf,
                    "versions": len(versions),
                    "latest_version": latest.version,
                    "confidence": latest.confidence,
                    "replays_attempted": latest.stats.replays_attempted,
                    "replays_succeeded": latest.stats.replays_succeeded,
                    "replays_failed": latest.stats.replays_failed,
                    "avg_duration_ms": int(latest.stats.avg_duration_ms),
                    "input_keys_required": sorted(latest.input_keys_required),
                })
            out["domains"].append({
                "domain": ddir.name,
                "workflows": sorted(workflows, key=lambda x: x["workflow"]),
            })
            if len(out["domains"]) >= limit:
                break
        return out


# -----------------------------------------------------------------------------
#  helpers
# -----------------------------------------------------------------------------
_DOMAIN_RE = re.compile(r"[^a-z0-9._-]")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:64]


def _safe_domain(host: str) -> str:
    return _DOMAIN_RE.sub("_", (host or "").lower())[:128]


def interpolate(
    template_text: str | None, inputs: dict[str, Any],
) -> str:
    """Replace ``${var}`` and ``${nested.key}`` with values from ``inputs``.

    Missing keys interpolate to the empty string — same semantics as the
    legacy workflow engine, so user inputs that don't define every field
    don't blow up the replay (they may instead trigger a verification
    failure, which is the right escalation path to AI).
    """
    if not template_text:
        return ""
    text = template_text
    while "${" in text:
        start = text.index("${")
        end = text.find("}", start)
        if end == -1:
            break
        expr = text[start + 2:end]
        text = text[:start] + str(_resolve(expr, inputs)) + text[end + 1:]
    return text


def _resolve(expr: str, inputs: dict[str, Any]) -> Any:
    node: Any = inputs
    for part in expr.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return ""
    return node


__all__ = [
    "ActionKind",
    "TemplateAction",
    "TemplateStats",
    "ExecutionTemplate",
    "ExecutionTemplateStore",
    "interpolate",
]
