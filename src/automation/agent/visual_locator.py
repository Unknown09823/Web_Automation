"""Visual fallback locator: find buttons / fields when the DOM lies.

Most pages can be reasoned about purely from their DOM, and that's
where the framework starts. But three real-world scenarios make
DOM-only analysis fail:

  1. **Canvas-rendered apps** — the entire UI lives inside one
     ``<canvas>`` and there are no semantic elements at all.
  2. **Aggressive obfuscation** — every class name is a 6-char hash
     that changes on every deploy, every text label is a sprite.
  3. **Cross-origin iframes** — Playwright can drive them, but
     perception can't read their DOM through the frame boundary.

The visual locator handles those tail cases. It takes a screenshot,
asks "where on this image does the text 'Sign in' appear, in
button-shaped clickable form?", and returns coordinates the caller
can pass to ``page.mouse.click()`` or ``page.click()`` with a
synthesized CSS selector.

Architecture
============

OCR is delegated. The locator never *requires* an OCR backend:
when none is configured the visual path simply reports "OCR not
available" and the caller falls back to whatever it would have
done anyway. When a backend *is* configured (Tesseract via
``pytesseract``, or the operator's own ML model), it's plugged in
through the :class:`OCRBackend` protocol.

The locator owns:

  * **Capture** — screenshot the page (or a clipped region) into
    a stable on-disk file the caller can reuse for debugging.
  * **OCR** — call the backend, get back a list of
    :class:`OCRWord` records.
  * **Match** — fuzzy text matching against the requested label,
    with role-style filters (button vs textbox) inferred from
    geometry and adjacent words.
  * **Locate** — return :class:`VisualMatch` records with click
    coordinates, a synthetic CSS selector pointing to the closest
    real element (so the rest of the framework can keep using
    selectors), and a confidence score.

This module is pure-Python with zero hard dependencies on heavy
imaging libraries. ``pytesseract`` and ``Pillow`` are imported
lazily inside :class:`TesseractBackend`; the rest of the module
runs without them, returning empty results.
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- types
@dataclass(slots=True)
class OCRWord:
    """One word recognized by an OCR backend.

    Coordinates are in *page* pixels (i.e. the same units a
    :class:`PageSnapshot` element bbox uses). Backends that report
    in image pixels must rescale before constructing this record.
    """

    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = 1.0  # 0..1, OCR backend's own confidence

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "confidence": round(self.confidence, 3),
        }


@dataclass(slots=True)
class VisualMatch:
    """A single visual location for a caller-requested label."""

    label_query: str
    matched_text: str
    x: float
    y: float
    w: float
    h: float
    score: float            # 0..1 fuzzy match × OCR confidence
    role_hint: str = ""     # "button" | "textbox" | "" (best effort)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @property
    def click_selector(self) -> str:
        """A synthetic Playwright text selector usable by the executor.

        Real coordinate-clicks should go through :py:meth:`click_at`,
        but a text-based selector is often a more robust handoff to
        the rest of the toolchain (it survives small layout shifts).
        """
        # Playwright supports ``text=...`` in CSS-like selectors.
        # We emit the recognized text rather than the original query
        # so a near-miss ("Continue" vs "continue") still works.
        text = self.matched_text.replace('"', '\\"').strip()
        if not text:
            return ""
        return f'text="{text}"'

    def to_dict(self) -> dict[str, Any]:
        return {
            "label_query": self.label_query,
            "matched_text": self.matched_text,
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "center": list(self.center),
            "score": round(self.score, 3),
            "role_hint": self.role_hint,
            "click_selector": self.click_selector,
        }


# ---------------------------------------------------------------- backend
class OCRBackend(Protocol):
    """Pluggable OCR interface.

    A backend converts an in-memory image (as raw bytes from a PNG
    screenshot) into a list of :class:`OCRWord`. Implementations are
    free to be slow; the locator caches results per screenshot so a
    single call doesn't run OCR twice.
    """

    @property
    def available(self) -> bool: ...
    @property
    def name(self) -> str: ...
    def recognize(self, png_bytes: bytes) -> list[OCRWord]: ...


class _NullBackend:
    """Default no-op backend. Reports unavailable; returns no words."""

    name = "null"

    @property
    def available(self) -> bool:
        return False

    def recognize(self, png_bytes: bytes) -> list[OCRWord]:  # noqa: ARG002
        return []


class TesseractBackend:
    """Optional ``pytesseract`` adapter.

    Imports are deferred until first use so the framework keeps
    booting on systems without Tesseract installed. ``available``
    returns ``False`` whenever the import fails or the binary is
    not on ``$PATH``.
    """

    name = "tesseract"

    def __init__(
        self,
        *,
        lang: str = "eng",
        min_confidence: float = 0.55,
    ) -> None:
        self.lang = lang
        self.min_confidence = float(min_confidence)
        self._checked = False
        self._available = False
        self._tesseract: Any = None
        self._image: Any = None

    @property
    def available(self) -> bool:
        if not self._checked:
            self._probe()
        return self._available

    def _probe(self) -> None:
        self._checked = True
        try:
            import pytesseract  # type: ignore[import-not-found]
            from PIL import Image  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001
            self._available = False
            return
        # Confirm the binary is reachable. ``get_tesseract_version`` is
        # the lightest probe that touches subprocess.
        try:
            pytesseract.get_tesseract_version()
        except Exception:  # noqa: BLE001
            self._available = False
            return
        self._tesseract = pytesseract
        self._image = Image
        self._available = True

    def recognize(self, png_bytes: bytes) -> list[OCRWord]:
        if not self.available:
            return []
        import io
        try:
            img = self._image.open(io.BytesIO(png_bytes))
            data = self._tesseract.image_to_data(
                img, lang=self.lang,
                output_type=self._tesseract.Output.DICT,
            )
        except Exception:  # noqa: BLE001
            log.debug("tesseract recognize failed", exc_info=True)
            return []

        words: list[OCRWord] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf_raw = float(data["conf"][i])
            except (TypeError, ValueError):
                conf_raw = -1.0
            if conf_raw < 0:
                continue  # tesseract uses -1 for non-text rows
            confidence = max(0.0, min(1.0, conf_raw / 100.0))
            if confidence < self.min_confidence:
                continue
            try:
                x = float(data["left"][i])
                y = float(data["top"][i])
                w = float(data["width"][i])
                h = float(data["height"][i])
            except (KeyError, ValueError):
                continue
            words.append(OCRWord(
                text=text, x=x, y=y, w=w, h=h, confidence=confidence,
            ))
        return words


# ---------------------------------------------------------------- locator
@dataclass(slots=True)
class LocateResult:
    """Outcome of :py:meth:`VisualLocator.locate`."""

    query: str
    matches: list[VisualMatch] = field(default_factory=list)
    words: list[OCRWord] = field(default_factory=list)
    backend: str = ""
    available: bool = False
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> VisualMatch | None:
        return self.matches[0] if self.matches else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "backend": self.backend,
            "available": self.available,
            "duration_ms": self.duration_ms,
            "match_count": len(self.matches),
            "matches": [m.to_dict() for m in self.matches[:10]],
            "notes": list(self.notes),
        }


class VisualLocator:
    """Find UI elements by their on-screen text.

    Parameters
    ----------
    backend:
        OCR backend. Defaults to :class:`_NullBackend` so the
        locator can be safely instantiated everywhere; replace
        with :class:`TesseractBackend` (or a custom implementation)
        when OCR is desired.
    min_match_score:
        Required fuzzy-match score (0..1). Defaults to 0.65 — high
        enough to reject "Cancel" when the caller asked for "Confirm",
        low enough to tolerate OCR noise in close-but-not-exact
        recognitions ("S1gn ln" still maps to "sign in").
    """

    def __init__(
        self,
        *,
        backend: OCRBackend | None = None,
        min_match_score: float = 0.65,
    ) -> None:
        self.backend: OCRBackend = backend or _NullBackend()
        self.min_match_score = float(min_match_score)
        # Per-screenshot OCR cache — keyed on the raw bytes' hash so a
        # caller running multiple locate() queries on the same shot
        # only pays the OCR cost once.
        self._cache: dict[int, list[OCRWord]] = {}
        self._cache_capacity = 8

    # ------------------------------------------------------------ availability
    @property
    def available(self) -> bool:
        """True when the locator actually has an OCR backend wired."""
        return self.backend.available

    # ------------------------------------------------------------ capture
    async def capture(
        self,
        page: Any,
        *,
        path: str | None = None,
        full_page: bool = False,
    ) -> bytes | None:
        """Take a screenshot. Returns PNG bytes (and saves to ``path`` if given).

        Returns ``None`` on failure rather than raising — callers
        already have a "DOM-first" path to fall back on.
        """
        try:
            kwargs: dict[str, Any] = {"full_page": full_page}
            if path:
                kwargs["path"] = path
            return await page.screenshot(**kwargs)
        except Exception:  # noqa: BLE001
            log.debug("visual capture failed", exc_info=True)
            return None

    # ------------------------------------------------------------ recognize
    def recognize(self, png_bytes: bytes) -> list[OCRWord]:
        """Run OCR on a PNG, with a small in-memory cache."""
        if not png_bytes:
            return []
        if not self.backend.available:
            return []
        key = hash(png_bytes)  # cheap; collisions are tolerable
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        words = self.backend.recognize(png_bytes)
        if len(self._cache) >= self._cache_capacity:
            # Drop oldest insertion to keep memory bounded.
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = words
        return words

    # ------------------------------------------------------------ locate
    async def locate(
        self,
        page: Any,
        query: str,
        *,
        role_hint: str = "",
        max_results: int = 5,
        screenshot_path: str | None = None,
    ) -> LocateResult:
        """End-to-end: capture → OCR → fuzzy match → rank.

        ``role_hint`` is one of ``"button"``, ``"textbox"``, or
        empty. It biases scoring toward boxes whose aspect ratio
        matches the role (buttons are typically wider than tall,
        textboxes are even more so) but doesn't reject mismatches —
        OCR confidence is too noisy to use as a hard filter.
        """
        started = time.time()
        result = LocateResult(query=query, backend=self.backend.name,
                              available=self.backend.available)

        if not query or not query.strip():
            result.notes.append("empty query")
            result.duration_ms = _elapsed_ms(started)
            return result
        if not self.backend.available:
            result.notes.append("OCR backend not available")
            result.duration_ms = _elapsed_ms(started)
            return result

        png = await self.capture(page, path=screenshot_path)
        if png is None:
            result.notes.append("screenshot capture failed")
            result.duration_ms = _elapsed_ms(started)
            return result

        words = self.recognize(png)
        result.words = words
        if not words:
            result.notes.append("OCR returned no words")
            result.duration_ms = _elapsed_ms(started)
            return result

        # Group adjacent words on the same line into phrases — a
        # button labeled "Continue with Google" is three OCR words
        # but one logical match.
        phrases = _group_phrases(words)

        # Score each phrase against the query.
        candidates: list[VisualMatch] = []
        norm_query = _normalize(query)
        for phrase in phrases:
            text = phrase["text"]
            score = _fuzzy_score(_normalize(text), norm_query)
            if score < self.min_match_score:
                continue
            box = phrase["bbox"]
            confidence = phrase["confidence"]
            inferred_role = _infer_role(box)
            # Mild role-hint bonus when the operator told us what
            # they're looking for. Buttons get +0.05; matching
            # textbox-shaped boxes for "search" likewise.
            role_bonus = 0.0
            if role_hint and inferred_role and role_hint == inferred_role:
                role_bonus = 0.05
            blended = min(1.0, score * 0.7 + confidence * 0.3 + role_bonus)
            candidates.append(VisualMatch(
                label_query=query,
                matched_text=text,
                x=box[0], y=box[1], w=box[2], h=box[3],
                score=blended,
                role_hint=inferred_role,
            ))

        candidates.sort(key=lambda m: m.score, reverse=True)
        result.matches = candidates[: max(0, max_results)]
        if not result.matches:
            result.notes.append(
                f"no phrase scored above {self.min_match_score:.2f}"
            )
        result.duration_ms = _elapsed_ms(started)
        return result

    # ------------------------------------------------------------ click_at
    async def click_at(
        self,
        page: Any,
        match: VisualMatch,
        *,
        button: str = "left",
    ) -> bool:
        """Click the centre of a :class:`VisualMatch`.

        Tries the synthesised text selector first (more robust to
        layout shifts) and falls back to coordinate clicking via
        ``page.mouse.click`` only if the selector path raises.
        Returns ``True`` on success.
        """
        sel = match.click_selector
        if sel:
            try:
                await page.click(sel, timeout=4000, button=button)
                return True
            except Exception:  # noqa: BLE001
                # Fall through to coordinate click.
                pass
        try:
            cx, cy = match.center
            mouse = getattr(page, "mouse", None)
            if mouse is None:
                return False
            await mouse.click(cx, cy, button=button)
            return True
        except Exception:  # noqa: BLE001
            log.debug("visual click_at failed", exc_info=True)
            return False


# ---------------------------------------------------------------- helpers
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def _normalize(text: str) -> str:
    """Lowercase, strip non-word punctuation, collapse whitespace."""
    if not text:
        return ""
    s = _NON_WORD_RE.sub(" ", text.lower())
    return _WS_RE.sub(" ", s).strip()


def _fuzzy_score(actual: str, expected: str) -> float:
    """Return a simple 0..1 fuzzy match score.

    We use Python's standard library only — no ``rapidfuzz`` —
    because the framework's runtime image stays minimal. The
    chosen metric is a length-normalized SequenceMatcher ratio
    plus a small bonus for whole-word containment, which is good
    enough for OCR-noise tolerance without pulling extra deps.
    """
    if not actual or not expected:
        return 0.0
    if actual == expected:
        return 1.0
    if expected in actual:
        # Exact substring match → high score scaled by length ratio.
        length_ratio = len(expected) / max(len(actual), 1)
        return min(1.0, 0.85 + 0.15 * length_ratio)
    # Difflib-based similarity for noisy OCR ("S1gn ln" vs "sign in").
    from difflib import SequenceMatcher
    sm = SequenceMatcher(a=actual, b=expected, autojunk=False)
    return float(sm.ratio())


def _group_phrases(words: Iterable[OCRWord]) -> list[dict[str, Any]]:
    """Group OCR words on the same line + close horizontally into phrases.

    The grouping is line-aware (vertical centres within ~half the
    average word height) and proximity-aware (gaps no wider than
    ~1.5× the word height). Both bounds are intentionally loose —
    we'd rather over-group and let fuzzy matching disambiguate
    than under-group and miss multi-word labels entirely.
    """
    word_list = sorted(words, key=lambda w: (w.y, w.x))
    if not word_list:
        return []
    avg_h = sum(w.h for w in word_list) / len(word_list)
    line_tol = max(4.0, avg_h * 0.6)
    gap_tol = max(8.0, avg_h * 1.5)

    phrases: list[dict[str, Any]] = []
    current: list[OCRWord] = []

    def flush() -> None:
        if not current:
            return
        x = min(w.x for w in current)
        y = min(w.y for w in current)
        right = max(w.x + w.w for w in current)
        bottom = max(w.y + w.h for w in current)
        text = " ".join(w.text for w in current)
        confidence = sum(w.confidence for w in current) / len(current)
        phrases.append({
            "text": text,
            "bbox": (x, y, right - x, bottom - y),
            "confidence": confidence,
        })

    for word in word_list:
        if not current:
            current = [word]
            continue
        last = current[-1]
        same_line = abs((word.y + word.h / 2) - (last.y + last.h / 2)) < line_tol
        close = (word.x - (last.x + last.w)) <= gap_tol
        if same_line and close:
            current.append(word)
        else:
            flush()
            current = [word]
    flush()

    # Single-word phrases are also allowed — many real button labels
    # are one word ("Login", "Submit", "Next").
    return phrases


def _infer_role(bbox: tuple[float, float, float, float]) -> str:
    """Best-effort role inference from a bounding box's geometry.

    Buttons tend to be ``2:1 .. 6:1`` aspect ratio. Textboxes are
    usually ``> 6:1`` and short height. Anything else is left blank.
    These are *hints*, not hard rules; the caller's role_hint adds
    a small score bonus rather than a filter.
    """
    _, _, w, h = bbox
    if h <= 0:
        return ""
    aspect = w / h
    if aspect <= 1.0:
        return ""  # icon, nothing useful to say
    if aspect <= 6.0 and h >= 18:
        return "button"
    if aspect > 6.0 and h <= 60:
        return "textbox"
    return ""


def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
