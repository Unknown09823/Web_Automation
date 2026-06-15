
* **Pages** — URL paths the agent has seen, with the symbolic name
  it learned for each (``/promo/rewards`` → ``"rewards"``).
* **Buttons** — selector candidates per intent group, ranked by
  success / fail counters. The deterministic engine consults this
  list before falling back to heuristic detection.
* **Navigation paths** — how to get from one page to another (e.g.
  from ``"dashboard"`` to ``"rewards"``: click selector ``.menu-rewards``).
  This lets the agent cross-navigate without re-running heuristics.
* **Success signals** — URL fragments, body phrases, or visible
  elements that historically meant a goal completed. The verifier
  uses these as additional weighted signals.

Persistence model

Writes are debounced via :py:meth:`flush` — a busy run can mutate
state hundreds of times, but we only hit the disk on flush. Reads
are always served from the in-memory cache so the hot path never
blocks on I/O. Operators can call :py:meth:`flush_all` or pass
``autoflush=True`` for stricter guarantees.

The format is intentionally permissive: unknown top-level keys are
preserved on round-trip so users can attach private annotations.
"""Per-site self-learning memory.

Each site gets a JSON file under ``data/learning/sites/<domain>.json``
containing successful patterns observed in past runs. The agent reads the
record at the start of a run for that domain (so it can prefer known-good
selectors), and updates it after every successful goal::

    {
        "domain": "example.com",
        "first_seen": 1716981234.5,
        "last_used": 1716981999.0,
        "successes": 12,
        "failures": 1,
        "logins":     [{"selector": "#email", "intent": "username"}, ...],
        "submits":    [{"selector": "button[type=submit]", "intent": "submit"}],
        "dashboard_urls": ["https://example.com/dashboard"],
        "workflows": {
            "register": {"runs": 8, "ok": 8, "avg_seconds": 23.1},
            "login":    {"runs": 4, "ok": 4, "avg_seconds": 9.6}
        }
    }

The store deliberately uses one JSON file per domain (rather than a SQLite
DB like ``ai.memory``) so the user can ``cat`` and ``grep`` it from
Telegram-shell or VS Code without a query engine. The selector-level memory
in ``ai.memory`` (SQLite) remains for fine-grained, dense lookups; this
store is the *narrative* memory the user wants surfaced via ``/memory``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


_DOMAIN_RE = re.compile(r"[^a-z0-9._-]")


def domain_from_url(url: str) -> str:
    """Normalize a URL or hostname into a filesystem-safe domain key."""
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = urlsplit(url).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    host = _DOMAIN_RE.sub("_", host)
    return host[:128]


@dataclass(slots=True)
class WorkflowStats:
    """Stats for one workflow type on one site."""
    runs: int = 0
    ok: int = 0
    failures: int = 0
    last_duration_seconds: float = 0.0
    avg_seconds: float = 0.0


@dataclass(slots=True)
class SiteRecord:
    """The observed pattern set for one site."""

    domain: str
    first_seen: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    successes: int = 0
    failures: int = 0
    logins: list[dict[str, str]] = field(default_factory=list)
    submits: list[dict[str, str]] = field(default_factory=list)
    dashboard_urls: list[str] = field(default_factory=list)
    landing_urls: list[str] = field(default_factory=list)
    workflows: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SiteRecord":
        return cls(
            domain=data.get("domain", ""),
            first_seen=float(data.get("first_seen", time.time())),
            last_used=float(data.get("last_used", time.time())),
            successes=int(data.get("successes", 0)),
            failures=int(data.get("failures", 0)),
            logins=list(data.get("logins") or []),
            submits=list(data.get("submits") or []),
            dashboard_urls=list(data.get("dashboard_urls") or []),
            landing_urls=list(data.get("landing_urls") or []),
            workflows=dict(data.get("workflows") or {}),
            notes=list(data.get("notes") or []),
        )

    def add_selector(self, kind: str, selector: str, intent: str = "") -> None:
        """Append a known-good selector; dedup, cap to 25."""
        if not selector:
            return
        target = self.logins if kind == "login" else self.submits
        if any(s.get("selector") == selector for s in target):
            return
        target.append({"selector": selector, "intent": intent})
        if len(target) > 25:
            del target[: len(target) - 25]

    def add_dashboard_url(self, url: str) -> None:
        if not url:
            return
        if url in self.dashboard_urls:
            return
        self.dashboard_urls.append(url)
        if len(self.dashboard_urls) > 10:
            self.dashboard_urls = self.dashboard_urls[-10:]

    def record_workflow(
        self,
        workflow: str,
        *,
        success: bool,
        duration_seconds: float,
    ) -> None:
        stats = self.workflows.setdefault(
            workflow,
            {"runs": 0, "ok": 0, "failures": 0,
             "last_duration_seconds": 0.0, "avg_seconds": 0.0},
        )
        stats["runs"] = int(stats.get("runs", 0)) + 1
        if success:
            stats["ok"] = int(stats.get("ok", 0)) + 1
        else:
            stats["failures"] = int(stats.get("failures", 0)) + 1
        stats["last_duration_seconds"] = float(duration_seconds)
        # exponential moving average so old runs don't dominate
        prev = float(stats.get("avg_seconds") or duration_seconds)
        stats["avg_seconds"] = round(0.7 * prev + 0.3 * duration_seconds, 2)

    @property
    def confidence(self) -> float:
        """Rough confidence in this site's stored patterns: 0.0–1.0."""
        total = self.successes + self.failures
        if total == 0:
            return 0.0
        return round(self.successes / total, 3)


class SiteMemory:
    """File-backed store of :class:`SiteRecord` keyed by domain."""

    def __init__(self, root: str | Path = "data/learning/sites") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, domain: str) -> Path:
        d = domain_from_url(domain) if "://" in domain else domain
        d = _DOMAIN_RE.sub("_", d.lower())
        return self.root / f"{d or '_unknown'}.json"

    def get(self, url_or_domain: str) -> SiteRecord:
        domain = domain_from_url(url_or_domain) if url_or_domain else ""
        path = self._path(domain or "_unknown")
        if not path.exists():
            return SiteRecord(domain=domain)
        try:
            return SiteRecord.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError):
            log.warning("site memory %s is corrupt; resetting", path)
            return SiteRecord(domain=domain)

    def save(self, record: SiteRecord) -> None:
        record.last_used = time.time()
        path = self._path(record.domain or "_unknown")
        with self._lock:
            path.write_text(json.dumps(record.to_dict(), indent=2, default=str))

    def list(self, *, limit: int = 100) -> list[SiteRecord]:
        out: list[SiteRecord] = []
        if not self.root.exists():
            return out
        files = sorted(
            self.root.iterdir(),
            key=lambda f: f.stat().st_mtime if f.exists() else 0,
            reverse=True,
        )
        for f in files[:limit]:
            if f.suffix.lower() != ".json":
                continue
            try:
                out.append(SiteRecord.from_dict(json.loads(f.read_text())))
            except (json.JSONDecodeError, KeyError):
                continue
        return out

    def remember_success(
        self,
        url: str,
        *,
        workflow: str,
        duration_seconds: float = 0.0,
        login_selector: str | None = None,
        submit_selector: str | None = None,
        dashboard_url: str | None = None,
        landing_url: str | None = None,
    ) -> SiteRecord:
        with self._lock:
            rec = self.get(url)
            rec.successes += 1
            if login_selector:
                rec.add_selector("login", login_selector, intent="login")
            if submit_selector:
                rec.add_selector("submit", submit_selector, intent="submit")
            if dashboard_url:
                rec.add_dashboard_url(dashboard_url)
            if landing_url and landing_url not in rec.landing_urls:
                rec.landing_urls.append(landing_url)
                if len(rec.landing_urls) > 10:
                    rec.landing_urls = rec.landing_urls[-10:]
            rec.record_workflow(
                workflow, success=True, duration_seconds=duration_seconds,
            )
            self.save(rec)
            return rec

    def remember_failure(
        self,
        url: str,
        *,
        workflow: str,
        duration_seconds: float = 0.0,
        note: str = "",
    ) -> SiteRecord:
        with self._lock:
            rec = self.get(url)
            rec.failures += 1
            if note:
                rec.notes.append(note)
                if len(rec.notes) > 20:
                    rec.notes = rec.notes[-20:]
            rec.record_workflow(
                workflow, success=False, duration_seconds=duration_seconds,
            )
            self.save(rec)
            return rec

    def summary(self) -> dict[str, Any]:
        records = self.list()
        return {
            "sites": len(records),
            "total_successes": sum(r.successes for r in records),
            "total_failures": sum(r.failures for r in records),
            "by_domain": [
                {
                    "domain": r.domain,
                    "successes": r.successes,
                    "failures": r.failures,
                    "confidence": r.confidence,
                    "last_used": r.last_used,
                    "workflows": list(r.workflows.keys()),
                }
                for r in records[:50]
            ],
        }


__all__ = ["SiteMemory", "SiteRecord", "WorkflowStats", "domain_from_url"]
