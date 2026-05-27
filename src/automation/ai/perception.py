"""Page perception: turn a Playwright page into a structured ``PageSnapshot``.

The perception layer extracts every potentially-interesting element with its
DOM, ARIA, and visual context, then asks the ``IntentMatcher`` to label it
with semantic intents. Output is a compact, JSON-serializable snapshot the
planner can reason over without re-touching the browser.

If Playwright is not installed (e.g. in tests), a stub adapter is used and
``capture_from_page`` raises ``RuntimeError``. ``capture_from_html`` still
works for offline analysis.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from automation.ai.intents import IntentMatcher, IntentMatch

log = logging.getLogger(__name__)


# JS executed inside the browser. Returns one entry per candidate element.
_EXTRACT_JS = r"""
() => {
  const SEL = [
    'a', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="textbox"]',
    '[role="checkbox"]', '[role="dialog"]',
    'form', 'nav', 'dialog',
  ];
  const seen = new Set();
  const out = [];
  for (const sel of SEL) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const r = el.getBoundingClientRect();
      const visible = !!(el.offsetWidth || el.offsetHeight) &&
                      window.getComputedStyle(el).visibility !== 'hidden';
      const text = (el.innerText || el.textContent || '').trim().slice(0, 200);
      out.push({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        id: el.id || '',
        name: el.getAttribute('name') || '',
        cls: el.className && typeof el.className === 'string' ? el.className.slice(0, 200) : '',
        role: el.getAttribute('role') || el.tagName.toLowerCase(),
        aria: el.getAttribute('aria-label') || '',
        placeholder: el.getAttribute('placeholder') || '',
        href: el.getAttribute('href') || '',
        value: (el.value || '').toString().slice(0, 200),
        text,
        visible,
        x: Math.round(r.x), y: Math.round(r.y),
        w: Math.round(r.width), h: Math.round(r.height),
      });
    }
  }
  return {
    title: document.title,
    url: location.href,
    elements: out,
    h1: Array.from(document.querySelectorAll('h1')).map(h => h.innerText.trim()).slice(0, 5),
    forms: document.querySelectorAll('form').length,
  };
}
"""


@dataclass(slots=True)
class DetectedElement:
    """A single perceived element with semantic labels and stable selectors."""

    tag: str
    role: str
    text: str
    aria: str
    placeholder: str
    name: str
    type: str
    id: str
    href: str
    selector: str
    intents: list[IntentMatch] = field(default_factory=list)
    visible: bool = True
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def primary_intent(self) -> str | None:
        return self.intents[0].intent if self.intents else None

    @property
    def confidence(self) -> float:
        return self.intents[0].score if self.intents else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "role": self.role,
            "text": self.text,
            "aria": self.aria,
            "placeholder": self.placeholder,
            "name": self.name,
            "type": self.type,
            "id": self.id,
            "href": self.href,
            "selector": self.selector,
            "visible": self.visible,
            "bbox": list(self.bbox),
            "intents": [
                {"intent": m.intent, "score": m.score, "matched_on": m.matched_on}
                for m in self.intents
            ],
        }


@dataclass(slots=True)
class PageSnapshot:
    """Self-contained, JSON-serializable view of a page."""

    url: str
    title: str
    h1: list[str]
    forms: int
    elements: list[DetectedElement]
    signature: str
    screenshot_path: str | None = None

    def by_intent(self, intent: str, min_score: float = 0.4) -> list[DetectedElement]:
        return [
            e for e in self.elements
            if any(m.intent == intent and m.score >= min_score for m in e.intents)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "h1": self.h1,
            "forms": self.forms,
            "signature": self.signature,
            "screenshot_path": self.screenshot_path,
            "elements": [e.to_dict() for e in self.elements],
        }


def _build_selector(el: dict[str, Any]) -> str:
    """Pick the most stable CSS selector we can derive from raw element data."""
    if el.get("id"):
        return f"#{el['id']}"
    name = el.get("name")
    tag = el.get("tag") or "*"
    if name:
        return f'{tag}[name="{name}"]'
    aria = el.get("aria")
    if aria:
        return f'{tag}[aria-label="{aria}"]'
    placeholder = el.get("placeholder")
    if placeholder:
        return f'{tag}[placeholder="{placeholder}"]'
    text = (el.get("text") or "").strip()
    if text and 2 <= len(text) <= 60 and tag in ("button", "a"):
        # text-based playwright pseudo-selector; still works in standard CSS engines
        # via has-text patterns when used through Playwright.
        return f'{tag}:has-text("{text[:40]}")'
    cls = (el.get("cls") or "").split()
    if cls:
        return f'{tag}.{cls[0]}'
    return tag


def _signature(url: str, title: str, elements: list[dict[str, Any]]) -> str:
    """Stable hash of page structure used as the memory key.

    Uses URL path (not query/fragment) + title + element role/text fingerprint
    so that small content changes don't invalidate learned selectors.
    """
    path = re.sub(r"[?#].*$", "", url or "")
    path = re.sub(r"https?://[^/]+", "", path)
    path = re.sub(r"/\d+", "/{id}", path)
    fingerprint = "|".join(
        f"{e.get('role','')}:{(e.get('text') or e.get('aria') or e.get('placeholder') or '')[:30]}"
        for e in elements[:80]
    )
    return hashlib.sha1(f"{path}::{title}::{fingerprint}".encode()).hexdigest()[:16]


class PagePerception:
    """Build a ``PageSnapshot`` from a Playwright Page or raw HTML."""

    def __init__(self, intent_matcher: IntentMatcher | None = None) -> None:
        self.matcher = intent_matcher or IntentMatcher()

    async def capture_from_page(self, page: Any, screenshot_path: str | None = None) -> PageSnapshot:
        """Capture from a live Playwright ``Page``.

        Catches all browser-side errors and returns an empty snapshot rather
        than propagating — the brain will treat this as ``no page understood``.
        """
        try:
            data = await page.evaluate(_EXTRACT_JS)
        except Exception:  # noqa: BLE001
            log.exception("perception: page.evaluate failed")
            return PageSnapshot(url="", title="", h1=[], forms=0, elements=[], signature="")
        if screenshot_path:
            try:
                await page.screenshot(path=screenshot_path, full_page=False)
            except Exception:  # noqa: BLE001
                log.exception("perception: screenshot failed")
                screenshot_path = None
        return self._build_snapshot(data, screenshot_path)

    def capture_from_html(self, html: str, url: str = "", title: str = "") -> PageSnapshot:
        """Lightweight offline snapshot builder for tests / replay."""
        elements = _parse_html_elements(html)
        data = {"url": url, "title": title, "elements": elements, "h1": [], "forms": 0}
        return self._build_snapshot(data, None)

    # ---------------------------------------------------------------- builders
    def _build_snapshot(
        self, data: dict[str, Any], screenshot_path: str | None
    ) -> PageSnapshot:
        raw_elements: list[dict[str, Any]] = data.get("elements", []) or []
        detected: list[DetectedElement] = []
        for el in raw_elements:
            matches = self.matcher.match(
                text=el.get("text"),
                aria_label=el.get("aria"),
                placeholder=el.get("placeholder"),
                role=el.get("role"),
                name=el.get("name"),
            )
            detected.append(
                DetectedElement(
                    tag=el.get("tag", ""),
                    role=el.get("role", ""),
                    text=el.get("text", ""),
                    aria=el.get("aria", ""),
                    placeholder=el.get("placeholder", ""),
                    name=el.get("name", ""),
                    type=el.get("type", ""),
                    id=el.get("id", ""),
                    href=el.get("href", ""),
                    selector=_build_selector(el),
                    intents=matches,
                    visible=bool(el.get("visible", True)),
                    bbox=(
                        int(el.get("x", 0)),
                        int(el.get("y", 0)),
                        int(el.get("w", 0)),
                        int(el.get("h", 0)),
                    ),
                )
            )
        sig = _signature(data.get("url", ""), data.get("title", ""), raw_elements)
        return PageSnapshot(
            url=data.get("url", ""),
            title=data.get("title", ""),
            h1=list(data.get("h1") or []),
            forms=int(data.get("forms") or 0),
            elements=detected,
            signature=sig,
            screenshot_path=screenshot_path,
        )


# ----------------------------------------------------------- offline HTML utils
_TAG_RE = re.compile(
    r'<(?P<tag>a|button|input|select|textarea|form|nav|dialog)\b(?P<attrs>[^>]*)>(?P<inner>.*?)</\1>|'
    r'<(?P<stag>input|button)\b(?P<sattrs>[^>]*)/?>',
    re.IGNORECASE | re.DOTALL,
)
_ATTR_RE = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"', re.IGNORECASE)


def _parse_html_elements(html: str) -> list[dict[str, Any]]:
    """Best-effort regex HTML parser for offline snapshot construction.

    This is intentionally simple — production capture should use Playwright.
    Used for tests, fixtures, and AI replay.
    """
    out: list[dict[str, Any]] = []
    for m in _TAG_RE.finditer(html):
        tag = (m.group("tag") or m.group("stag") or "").lower()
        attrs = m.group("attrs") or m.group("sattrs") or ""
        inner = (m.group("inner") or "").strip()
        attr_dict = {k.lower(): v for k, v in _ATTR_RE.findall(attrs)}
        # strip nested tags from inner text
        text = re.sub(r"<[^>]+>", "", inner).strip()
        out.append({
            "tag": tag,
            "type": attr_dict.get("type", ""),
            "id": attr_dict.get("id", ""),
            "name": attr_dict.get("name", ""),
            "cls": attr_dict.get("class", ""),
            "role": attr_dict.get("role", tag),
            "aria": attr_dict.get("aria-label", ""),
            "placeholder": attr_dict.get("placeholder", ""),
            "href": attr_dict.get("href", ""),
            "value": attr_dict.get("value", ""),
            "text": text[:200],
            "visible": True,
            "x": 0, "y": 0, "w": 0, "h": 0,
        })
    return out
