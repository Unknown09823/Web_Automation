"""Per-host knowledge bank.

What this module *is*: a long-lived, human-readable cache of the
things the agent has learned about each website.

What it *isn't*: a snapshot of the DOM (perception does that), a
goal-level template (templates do that), or a collection of opaque
ML weights (memory.py does that). Site memory sits between them —
one JSON file per host that's small enough to read, ship across
machines, and review as a pull request.

Each ``SiteMemory`` instance is responsible for a single ``data/learning/sites/``
directory; one file per host gives operators a clean way to inspect
or wipe state for a single domain without touching the others.

Stored facts per host
=====================

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
=================

Writes are debounced via :py:meth:`flush` — a busy run can mutate
state hundreds of times, but we only hit the disk on flush. Reads
are always served from the in-memory cache so the hot path never
blocks on I/O. Operators can call :py:meth:`flush_all` or pass
``autoflush=True`` for stricter guarantees.

The format is intentionally permissive: unknown top-level keys are
preserved on round-trip so users can attach private annotations.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


# ----------------------------------------------------------- data model
@dataclass(slots=True)
class KnownPage:
    """A URL path + the name we learned for it."""

    path: str
    name: str = ""
    seen_count: int = 0
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnownPage":
        return cls(
            path=str(data.get("path", "")),
            name=str(data.get("name", "")),
            seen_count=int(data.get("seen_count", 0)),
            last_seen=float(data.get("last_seen", 0.0)),
        )


@dataclass(slots=True)
class KnownButton:
    """A selector that worked for a particular intent group on this host.

    ``confidence`` is recomputed every time the counters change, so
    callers can sort by it without recomputing themselves.
    """

    intent: str
    selector: str
    success_count: int = 0
    fail_count: int = 0
    url_path: str = ""
    last_used: float = 0.0

    @property
    def confidence(self) -> float:
        total = self.success_count + self.fail_count
        if total <= 0:
            return 0.0
        # Mild Laplace smoothing (+1) so a single success doesn't pin
        # confidence at 1.0 — that would mask flakiness.
        return self.success_count / (total + 1.0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["confidence"] = round(self.confidence, 3)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnownButton":
        return cls(
            intent=str(data.get("intent", "")),
            selector=str(data.get("selector", "")),
            success_count=int(data.get("success_count", 0)),
            fail_count=int(data.get("fail_count", 0)),
            url_path=str(data.get("url_path", "")),
            last_used=float(data.get("last_used", 0.0)),
        )


@dataclass(slots=True)
class NavStep:
    """One hop in a learned navigation path.

    ``intent`` is the heuristic intent group used (e.g.
    ``"open_rewards_page"``). ``selector`` is the concrete element
    that produced the navigation when it worked.
    """

    from_page: str
    to_page: str
    intent: str = ""
    selector: str = ""
    success_count: int = 0
    fail_count: int = 0
    last_used: float = 0.0

    @property
    def confidence(self) -> float:
        total = self.success_count + self.fail_count
        return 0.0 if total <= 0 else self.success_count / (total + 1.0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["confidence"] = round(self.confidence, 3)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NavStep":
        return cls(
            from_page=str(data.get("from_page", "")),
            to_page=str(data.get("to_page", "")),
            intent=str(data.get("intent", "")),
            selector=str(data.get("selector", "")),
            success_count=int(data.get("success_count", 0)),
            fail_count=int(data.get("fail_count", 0)),
            last_used=float(data.get("last_used", 0.0)),
        )


@dataclass(slots=True)
class SuccessSignal:
    """One signal that historically meant a goal succeeded.

    ``kind`` is one of: ``"url_contains"``, ``"url_matches"``,
    ``"body_contains"``, ``"title_contains"``, ``"intent_visible"``.
    The verifier knows how to evaluate each.
    """

    kind: str
    value: str
    weight: float = 1.0
    success_count: int = 0
    fail_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuccessSignal":
        return cls(
            kind=str(data.get("kind", "")),
            value=str(data.get("value", "")),
            weight=float(data.get("weight", 1.0)),
            success_count=int(data.get("success_count", 0)),
            fail_count=int(data.get("fail_count", 0)),
        )


@dataclass(slots=True)
class HostMemory:
    """Everything we remember about one host."""

    host: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    visit_count: int = 0
    pages: dict[str, KnownPage] = field(default_factory=dict)
    # Buttons indexed by intent group → ranked list (highest confidence first)
    buttons: dict[str, list[KnownButton]] = field(default_factory=dict)
    nav_paths: dict[str, list[NavStep]] = field(default_factory=dict)
    success_signals: dict[str, list[SuccessSignal]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "visit_count": self.visit_count,
            "pages": {k: v.to_dict() for k, v in self.pages.items()},
            "buttons": {
                intent: [b.to_dict() for b in lst]
                for intent, lst in self.buttons.items()
            },
            "nav_paths": {
                target: [n.to_dict() for n in lst]
                for target, lst in self.nav_paths.items()
            },
            "success_signals": {
                goal: [s.to_dict() for s in lst]
                for goal, lst in self.success_signals.items()
            },
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HostMemory":
        host = cls(host=str(data.get("host", "")))
        host.first_seen = float(data.get("first_seen", time.time()))
        host.last_seen = float(data.get("last_seen", time.time()))
        host.visit_count = int(data.get("visit_count", 0))
        for path, page_raw in (data.get("pages") or {}).items():
            host.pages[path] = KnownPage.from_dict(page_raw)
        for intent, raws in (data.get("buttons") or {}).items():
            host.buttons[intent] = [KnownButton.from_dict(b) for b in raws or []]
        for target, raws in (data.get("nav_paths") or {}).items():
            host.nav_paths[target] = [NavStep.from_dict(n) for n in raws or []]
        for goal, raws in (data.get("success_signals") or {}).items():
            host.success_signals[goal] = [
                SuccessSignal.from_dict(s) for s in raws or []
            ]
        host.extra = dict(data.get("extra") or {})
        return host


# ----------------------------------------------------------- engine
class SiteMemory:
    """File-backed, per-host knowledge bank.

    Threading: not thread-safe; pair one instance per asyncio event
    loop. The agent already runs each account in its own coroutine and
    serializes its memory updates.
    """

    def __init__(
        self,
        root: str | Path = "data/learning/sites",
        *,
        autoflush: bool = False,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.autoflush = bool(autoflush)
        self._cache: dict[str, HostMemory] = {}
        self._dirty: set[str] = set()

    # ---------------------------------------------------------- host I/O
    def get_host(self, url_or_host: str) -> HostMemory:
        """Return the host's memory, loading from disk on first access."""
        host = _host_of(url_or_host)
        if host in self._cache:
            return self._cache[host]
        path = self._path_for(host)
        if path.exists():
            try:
                self._cache[host] = HostMemory.from_dict(
                    json.loads(path.read_text())
                )
            except (OSError, json.JSONDecodeError):  # noqa: BLE001
                log.warning("site memory: cannot read %s", path, exc_info=True)
                self._cache[host] = HostMemory(host=host)
        else:
            self._cache[host] = HostMemory(host=host)
        return self._cache[host]

    def hosts(self) -> list[str]:
        """List every host that has a file on disk."""
        return sorted(p.stem for p in self.root.glob("*.json"))

    def flush(self, host: str) -> bool:
        """Write a single host's memory to disk if it's dirty."""
        if host not in self._dirty:
            return False
        mem = self._cache.get(host)
        if mem is None:
            self._dirty.discard(host)
            return False
        try:
            self._path_for(host).write_text(
                json.dumps(mem.to_dict(), indent=2, default=str),
            )
            self._dirty.discard(host)
            return True
        except OSError:  # noqa: BLE001
            log.warning("site memory: write failed for %s", host, exc_info=True)
            return False

    def flush_all(self) -> int:
        """Flush every dirty host. Returns count flushed."""
        count = 0
        for h in list(self._dirty):
            if self.flush(h):
                count += 1
        return count

    # ---------------------------------------------------------- pages
    def record_page(self, url: str, *, name: str = "") -> KnownPage:
        """Mark a URL as a known page on this host. Returns the entry."""
        host = _host_of(url)
        mem = self.get_host(host)
        path = _path_of(url)
        now = time.time()
        existing = mem.pages.get(path)
        if existing is None:
            page = KnownPage(path=path, name=name, seen_count=1, last_seen=now)
            mem.pages[path] = page
        else:
            existing.seen_count += 1
            existing.last_seen = now
            if name and not existing.name:
                existing.name = name
            page = existing
        mem.last_seen = now
        mem.visit_count += 1
        self._mark_dirty(host)
        return page

    def known_pages(self, url_or_host: str) -> dict[str, KnownPage]:
        return dict(self.get_host(url_or_host).pages)

    def find_page_by_name(self, url_or_host: str, name: str) -> KnownPage | None:
        """Return the most-recently seen page whose name matches."""
        candidates = [
            p for p in self.get_host(url_or_host).pages.values()
            if p.name == name
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.last_seen, reverse=True)
        return candidates[0]

    # ---------------------------------------------------------- buttons
    def record_button(
        self,
        url: str,
        intent: str,
        selector: str,
        *,
        success: bool = True,
    ) -> None:
        """Bump success/fail counters for a (intent, selector) pair."""
        host = _host_of(url)
        path = _path_of(url)
        mem = self.get_host(host)
        bucket = mem.buttons.setdefault(intent, [])
        match = next(
            (b for b in bucket if b.selector == selector and b.url_path == path),
            None,
        )
        now = time.time()
        if match is None:
            match = KnownButton(
                intent=intent, selector=selector,
                url_path=path, last_used=now,
            )
            bucket.append(match)
        if success:
            match.success_count += 1
        else:
            match.fail_count += 1
        match.last_used = now
        # Keep buckets ranked by confidence so callers can pop[0].
        bucket.sort(
            key=lambda b: (b.confidence, b.success_count, b.last_used),
            reverse=True,
        )
        # Cap to a reasonable size — we don't need every selector ever tried.
        del bucket[20:]
        self._mark_dirty(host)

    def get_buttons(
        self, url: str, intent: str, *, min_confidence: float = 0.0,
    ) -> list[KnownButton]:
        """Return ranked button candidates for an intent on this host.

        ``url`` is used to scope the lookup to the same path when an
        entry exists for it; if none does, we fall back to host-wide
        candidates so a button learned on the home page can still help
        on a sub-page.
        """
        mem = self.get_host(url)
        candidates = mem.buttons.get(intent) or []
        path = _path_of(url)
        if path:
            same_path = [b for b in candidates if b.url_path == path]
            if same_path:
                candidates = same_path
        return [b for b in candidates if b.confidence >= min_confidence]

    # ---------------------------------------------------------- nav paths
    def record_nav(
        self,
        url: str,
        *,
        from_page: str,
        to_page: str,
        intent: str = "",
        selector: str = "",
        success: bool = True,
    ) -> None:
        """Record that the agent crossed from ``from_page`` to ``to_page``."""
        host = _host_of(url)
        mem = self.get_host(host)
        bucket = mem.nav_paths.setdefault(to_page, [])
        match = next(
            (
                n for n in bucket
                if n.from_page == from_page and n.selector == selector
                and n.intent == intent
            ),
            None,
        )
        now = time.time()
        if match is None:
            match = NavStep(
                from_page=from_page, to_page=to_page,
                intent=intent, selector=selector, last_used=now,
            )
            bucket.append(match)
        if success:
            match.success_count += 1
        else:
            match.fail_count += 1
        match.last_used = now
        bucket.sort(
            key=lambda n: (n.confidence, n.success_count, n.last_used),
            reverse=True,
        )
        del bucket[10:]
        self._mark_dirty(host)

    def get_nav_path(
        self, url: str, *, to_page: str,
    ) -> NavStep | None:
        """Return the highest-confidence nav step toward ``to_page``."""
        mem = self.get_host(url)
        bucket = mem.nav_paths.get(to_page) or []
        return bucket[0] if bucket else None

    # ---------------------------------------------------------- success signals
    def record_success_signal(
        self,
        url: str,
        *,
        goal: str,
        kind: str,
        value: str,
        weight: float = 1.0,
        success: bool = True,
    ) -> None:
        """Reinforce or weaken a success signal for ``goal`` on this host."""
        host = _host_of(url)
        mem = self.get_host(host)
        bucket = mem.success_signals.setdefault(goal, [])
        match = next(
            (s for s in bucket if s.kind == kind and s.value == value),
            None,
        )
        if match is None:
            match = SuccessSignal(kind=kind, value=value, weight=weight)
            bucket.append(match)
        if success:
            match.success_count += 1
        else:
            match.fail_count += 1
        # Adjust weight slightly toward observed success rate so a flaky
        # signal automatically loses influence over time.
        total = match.success_count + match.fail_count
        if total >= 3:
            match.weight = round(
                max(0.1, min(2.0, match.success_count / (total + 1.0) * 2.0)),
                3,
            )
        bucket.sort(
            key=lambda s: (s.weight, s.success_count), reverse=True,
        )
        del bucket[15:]
        self._mark_dirty(host)

    def get_success_signals(
        self, url: str, *, goal: str,
    ) -> list[SuccessSignal]:
        return list(self.get_host(url).success_signals.get(goal) or [])

    # ---------------------------------------------------------- helpers
    def _path_for(self, host: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", host)[:128]
        return self.root / f"{safe}.json"

    def _mark_dirty(self, host: str) -> None:
        self._dirty.add(host)
        if self.autoflush:
            self.flush(host)


# ----------------------------------------------------------- url helpers
def _host_of(url_or_host: str) -> str:
    """Extract the lowercase host from a URL or pass-through a host string."""
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return "unknown"
    if "://" not in raw:
        # Already a host (or close to one). Strip any path-ish tail.
        return raw.split("/", 1)[0] or "unknown"
    parts = urlsplit(raw)
    return parts.hostname or "unknown"


def _path_of(url: str) -> str:
    """Normalize URL → bare path, dropping query/fragment/port."""
    if not url:
        return "/"
    if "://" not in url:
        # Plain path
        return url.split("?", 1)[0].split("#", 1)[0] or "/"
    parts = urlsplit(url)
    path = parts.path or "/"
    return path
