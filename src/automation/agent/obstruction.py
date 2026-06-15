"""Pre-click obstruction detection and recovery.

A click that "succeeded" without producing any effect is the most
silently destructive failure mode in browser automation. The browser
returns no error: the framework's executor sees a green ``await
page.click(...)`` and moves on, the verifier later notices a goal
didn't complete, and the operator burns minutes triaging a phantom
issue. The root cause is almost always *obstruction* — a cookie
banner, a sticky header, a modal, or a transient toast sat on top of
the target element and absorbed the click.

This module is the deterministic check the framework runs *before*
every click that matters. It answers four questions about a target
element:

  1. **Is it in the DOM and visible?** Hidden / display:none / size 0
     elements are reported as ``not_found`` or ``hidden``.
  2. **Is it inside the viewport?** Off-screen elements are
     ``offscreen``; a scroll-into-view nudge usually fixes them.
  3. **Is it actually clickable?** ``disabled`` or
     ``pointer-events: none`` cannot be clicked at all.
  4. **Does the click point hit *it*?** This is the key check.
     ``elementFromPoint(centerX, centerY)`` is computed in the
     browser; if it returns a different node, that node is the
     **obstruction** and we surface it back to the caller.

When the result is ``blocked``, :class:`ObstructionDetector` exposes
:py:meth:`unblock` which tries — in priority order — three
remediations: dismiss the obstruction via :class:`PopupGuard`, hide
the obstruction via a one-off CSS injection, or scroll the target
element into a clear region.

The module is browser-agnostic: it talks through a small protocol so
unit tests can drive it with a fake page. Real Playwright pages
already satisfy the protocol. All exceptions inside browser calls
are converted to a structured :class:`ObstructionResult` so the
caller never has to wrap a click in a try block again.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Protocol

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- types
class ObstructionStatus(str, Enum):
    """The four steady states of a pre-click probe."""

    OK = "ok"                 # ready to click; no work needed
    NOT_FOUND = "not_found"   # selector resolves to no element
    HIDDEN = "hidden"         # element exists but is invisible
    OFFSCREEN = "offscreen"   # element is hidden by viewport, not by anything else
    UNCLICKABLE = "unclickable"  # disabled / pointer-events:none / inert
    BLOCKED = "blocked"       # another element is over the click point
    ERROR = "error"           # the probe itself failed (caller should retry)


@dataclass(slots=True)
class ObstructionInfo:
    """Details about *what* is in the way when status is ``BLOCKED``."""

    tag: str = ""
    id: str = ""
    classes: str = ""
    role: str = ""
    text: str = ""
    z_index: str = ""

    @property
    def description(self) -> str:
        bits = [self.tag or "?"]
        if self.id:
            bits.append(f"#{self.id}")
        if self.classes:
            cls = self.classes.split()[0] if self.classes else ""
            if cls:
                bits.append(f".{cls}")
        if self.text:
            bits.append(f"{self.text[:30]!r}")
        return "".join(bits[:1]) + (
            "".join(b if b.startswith(("#", ".")) else f" {b}" for b in bits[1:])
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "id": self.id,
            "classes": self.classes,
            "role": self.role,
            "text": self.text,
            "z_index": self.z_index,
            "description": self.description,
        }


@dataclass(slots=True)
class ObstructionResult:
    """Outcome of one :py:meth:`ObstructionDetector.probe` call."""

    status: ObstructionStatus
    selector: str
    visible: bool = False
    in_viewport: bool = False
    clickable: bool = False
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    obstruction: ObstructionInfo | None = None
    duration_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is ObstructionStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "selector": self.selector,
            "visible": self.visible,
            "in_viewport": self.in_viewport,
            "clickable": self.clickable,
            "bbox": list(self.bbox),
            "obstruction": (
                self.obstruction.to_dict() if self.obstruction else None
            ),
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(slots=True)
class UnblockAttempt:
    """One step in the unblock retry loop."""

    strategy: str  # "scroll" | "popup_guard" | "hide_overlay" | "wait"
    success: bool
    detail: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "success": self.success,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class UnblockResult:
    """Outcome of :py:meth:`ObstructionDetector.unblock`."""

    cleared: bool
    attempts: list[UnblockAttempt] = field(default_factory=list)
    final_probe: ObstructionResult | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cleared": self.cleared,
            "attempts": [a.to_dict() for a in self.attempts],
            "final_probe": (
                self.final_probe.to_dict() if self.final_probe else None
            ),
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------- protocol
class _PageLike(Protocol):
    """Minimal page surface the detector touches.

    Real Playwright pages satisfy this; tests pass a small fake.
    """

    async def evaluate(self, expr: str, *args: Any) -> Any: ...
    async def click(self, selector: str, *, timeout: int = ...) -> Any: ...


class _PopupGuardLike(Protocol):
    """The slice of :class:`PopupGuard` we use to recover."""

    async def scan_and_dismiss(self, page: Any) -> Any: ...


# ---------------------------------------------------------------- detector
class ObstructionDetector:
    """Probe a click target for visibility, clickability, and obstruction.

    The detector is stateless across calls. Browser interaction is
    funnelled through a single ``page.evaluate`` call so a probe
    costs one round-trip — we don't want to make pre-click checks
    expensive enough that operators turn them off.

    Parameters
    ----------
    popup_guard:
        When supplied, ``unblock`` will ask the guard to dismiss the
        offending overlay before falling back to CSS hiding. Reusing
        the agent's instance keeps the rule catalog in sync.
    hide_blockers:
        When ``True`` (default), the unblock fallback injects a tiny
        stylesheet that sets the obstruction to ``display: none !important``.
        Set to ``False`` for sites where hiding the overlay would
        break navigation (cookie banners that mount the rest of
        the page below themselves, for instance).
    """

    def __init__(
        self,
        *,
        popup_guard: _PopupGuardLike | None = None,
        hide_blockers: bool = True,
        scroll_attempts: int = 2,
        per_check_timeout_ms: int = 2_000,
    ) -> None:
        self.popup_guard = popup_guard
        self.hide_blockers = bool(hide_blockers)
        self.scroll_attempts = int(scroll_attempts)
        self.per_check_timeout_ms = int(per_check_timeout_ms)

    # ------------------------------------------------------------ probe
    async def probe(
        self,
        page: _PageLike,
        selector: str,
    ) -> ObstructionResult:
        """Single round-trip diagnostic of a click target.

        Returns an :class:`ObstructionResult` with everything the
        caller needs to either click safely or invoke
        :py:meth:`unblock`.
        """
        started = time.time()
        try:
            data = await page.evaluate(_PROBE_JS, selector)
        except Exception as exc:  # noqa: BLE001 — defensive, never raise
            return ObstructionResult(
                status=ObstructionStatus.ERROR,
                selector=selector,
                duration_ms=_elapsed_ms(started),
                error=f"probe evaluate failed: {exc!r}",
            )
        if not isinstance(data, dict):
            return ObstructionResult(
                status=ObstructionStatus.ERROR,
                selector=selector,
                duration_ms=_elapsed_ms(started),
                error=f"unexpected probe payload: {data!r}",
            )

        status = ObstructionStatus(data.get("status", "error"))
        bbox = data.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        info_raw = data.get("obstruction") or None
        info = (
            ObstructionInfo(
                tag=str(info_raw.get("tag", "")),
                id=str(info_raw.get("id", "")),
                classes=str(info_raw.get("classes", "")),
                role=str(info_raw.get("role", "")),
                text=str(info_raw.get("text", "")),
                z_index=str(info_raw.get("zIndex", "")),
            )
            if info_raw
            else None
        )
        return ObstructionResult(
            status=status,
            selector=selector,
            visible=bool(data.get("visible", False)),
            in_viewport=bool(data.get("inViewport", False)),
            clickable=bool(data.get("clickable", False)),
            bbox=tuple(float(b) for b in bbox[:4]),  # type: ignore[arg-type]
            obstruction=info,
            duration_ms=_elapsed_ms(started),
        )

    # ------------------------------------------------------------ unblock
    async def unblock(
        self,
        page: _PageLike,
        selector: str,
        *,
        initial: ObstructionResult | None = None,
        max_attempts: int = 4,
    ) -> UnblockResult:
        """Try to make the click safe.

        Strategies in priority order:

          1. **scroll-into-view** — fast, harmless, works for the
             "below the fold" case which is the most common.
          2. **popup_guard** — when supplied, asks the guard to
             dismiss any cookie banner / modal overlapping the
             target. This is the right answer for legitimate
             overlays that should be acknowledged anyway.
          3. **hide_overlay** — last-resort CSS injection that
             sets ``display: none !important`` on the specific
             element returned by ``elementFromPoint``. Works when
             the overlay is a static "smart banner" that won't
             reappear after dismissal.
          4. **wait + re-probe** — for transient toasts that fade
             out on their own; one short sleep is cheaper than a
             full retry of the goal.
        """
        started = time.time()
        result = UnblockResult(cleared=False)
        probe = initial or await self.probe(page, selector)

        for attempt in range(max(1, max_attempts)):
            if probe.ok:
                result.cleared = True
                result.final_probe = probe
                break

            if probe.status is ObstructionStatus.OFFSCREEN or not probe.in_viewport:
                ua = await self._scroll_into_view(page, selector)
                result.attempts.append(ua)
                probe = await self.probe(page, selector)
                continue

            if probe.status is ObstructionStatus.BLOCKED:
                # Try popup guard first if configured — clears legitimate banners.
                if self.popup_guard is not None and attempt == 0:
                    ua = await self._invoke_popup_guard(page)
                    result.attempts.append(ua)
                    probe = await self.probe(page, selector)
                    if probe.ok:
                        continue

                # Then scroll, in case the obstruction is a sticky header
                # that disappears once the target moves up.
                if attempt <= 1:
                    ua = await self._scroll_into_view(page, selector)
                    result.attempts.append(ua)
                    probe = await self.probe(page, selector)
                    if probe.ok:
                        continue

                # Last resort: actually hide the obstruction.
                if self.hide_blockers and probe.obstruction:
                    ua = await self._hide_blocker(page, probe.obstruction)
                    result.attempts.append(ua)
                    probe = await self.probe(page, selector)
                    continue

            if probe.status is ObstructionStatus.HIDDEN:
                # A short wait helps for transient hide-on-animation states.
                ua = await self._wait_brief(page)
                result.attempts.append(ua)
                probe = await self.probe(page, selector)
                continue

            # NOT_FOUND / UNCLICKABLE / ERROR — we can't do anything useful.
            break

        result.cleared = bool(probe.ok)
        result.final_probe = probe
        result.duration_ms = _elapsed_ms(started)
        return result

    # ---------------------------------------------------- safe-click helper
    async def safe_click(
        self,
        page: _PageLike,
        selector: str,
        *,
        timeout_ms: int = 10_000,
        unblock_attempts: int = 3,
    ) -> tuple[bool, ObstructionResult, UnblockResult | None]:
        """Probe → unblock if needed → click.

        Returns ``(clicked, final_probe, unblock_result_or_None)``.
        Callers wanting the dictionary form for events can call
        ``.to_dict()`` on the returned objects.

        The click itself is performed with the supplied ``timeout_ms``;
        a failed click after a successful unblock is reported as
        ``clicked=False`` with the error in the probe result, not as
        an exception. This keeps the executor free of try blocks.
        """
        probe = await self.probe(page, selector)
        unblock_result: UnblockResult | None = None
        if not probe.ok:
            unblock_result = await self.unblock(
                page, selector, initial=probe, max_attempts=unblock_attempts,
            )
            probe = unblock_result.final_probe or probe
            if not probe.ok:
                return False, probe, unblock_result

        try:
            await page.click(selector, timeout=timeout_ms)  # type: ignore[call-arg]
            return True, probe, unblock_result
        except Exception as exc:  # noqa: BLE001
            probe = ObstructionResult(
                status=ObstructionStatus.ERROR,
                selector=selector,
                error=f"click after unblock failed: {exc!r}",
                duration_ms=probe.duration_ms,
                visible=probe.visible,
                in_viewport=probe.in_viewport,
                clickable=probe.clickable,
                bbox=probe.bbox,
                obstruction=probe.obstruction,
            )
            return False, probe, unblock_result

    # --------------------------------------------------------- internals
    async def _scroll_into_view(
        self, page: _PageLike, selector: str,
    ) -> UnblockAttempt:
        started = time.time()
        try:
            await page.evaluate(_SCROLL_JS, selector)
            return UnblockAttempt(
                strategy="scroll",
                success=True,
                detail="element scrolled into view (centered)",
                duration_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return UnblockAttempt(
                strategy="scroll",
                success=False,
                detail=f"scroll failed: {exc!r}",
                duration_ms=_elapsed_ms(started),
            )

    async def _invoke_popup_guard(self, page: _PageLike) -> UnblockAttempt:
        started = time.time()
        try:
            res: Any = await self.popup_guard.scan_and_dismiss(page)  # type: ignore[union-attr]
            dismissed = bool(getattr(res, "any_dismissed", False))
            return UnblockAttempt(
                strategy="popup_guard",
                success=dismissed,
                detail=(
                    f"popup_guard dismissed {len(getattr(res, 'dismissed', []))} overlay(s)"
                    if dismissed
                    else "popup_guard found nothing to dismiss"
                ),
                duration_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return UnblockAttempt(
                strategy="popup_guard",
                success=False,
                detail=f"popup_guard raised: {exc!r}",
                duration_ms=_elapsed_ms(started),
            )

    async def _hide_blocker(
        self,
        page: _PageLike,
        info: ObstructionInfo,
    ) -> UnblockAttempt:
        """Inject CSS to hide the specific blocker.

        We never use ``display:none`` on every banner-shaped element on
        the page; we hide *only* the element whose tag/id/class
        signature matches the probed obstruction. That makes the
        operation reversible (operators can refresh) and prevents
        cascading layout damage.
        """
        started = time.time()
        try:
            await page.evaluate(_HIDE_JS, info.to_dict())
            return UnblockAttempt(
                strategy="hide_overlay",
                success=True,
                detail=f"hid overlay: {info.description}",
                duration_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return UnblockAttempt(
                strategy="hide_overlay",
                success=False,
                detail=f"hide failed: {exc!r}",
                duration_ms=_elapsed_ms(started),
            )

    async def _wait_brief(self, page: _PageLike) -> UnblockAttempt:
        import asyncio
        started = time.time()
        try:
            await asyncio.sleep(0.4)
            return UnblockAttempt(
                strategy="wait",
                success=True,
                detail="waited 400ms for transient state",
                duration_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return UnblockAttempt(
                strategy="wait",
                success=False,
                detail=f"wait raised: {exc!r}",
                duration_ms=_elapsed_ms(started),
            )


# ---------------------------------------------------------------- helpers
def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)


# ---------------------------------------------------------------- JS payloads
# All JS lives at module level so it's parsed once. The probe runs
# entirely inside the browser to keep the round-trip cost flat.
_PROBE_JS = r"""
(selector) => {
    function summarize(el) {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const cs = window.getComputedStyle(el);
        return {
            tag: (el.tagName || '').toLowerCase(),
            id: el.id || '',
            classes: typeof el.className === 'string' ? el.className.slice(0, 200) : '',
            role: el.getAttribute && el.getAttribute('role') || '',
            text: ((el.innerText || el.textContent || '') + '').trim().slice(0, 80),
            zIndex: cs.zIndex || '',
            x: r.x, y: r.y, w: r.width, h: r.height,
        };
    }
    const target = document.querySelector(selector);
    if (!target) {
        return {
            status: 'not_found', visible: false, inViewport: false,
            clickable: false, bbox: [0, 0, 0, 0], obstruction: null,
        };
    }
    const r = target.getBoundingClientRect();
    const cs = window.getComputedStyle(target);
    const sized = (r.width > 0 && r.height > 0);
    const visible = sized && cs.visibility !== 'hidden' && cs.display !== 'none'
                    && cs.opacity !== '0';
    if (!visible) {
        return {
            status: 'hidden', visible: false, inViewport: false,
            clickable: false, bbox: [r.x, r.y, r.width, r.height], obstruction: null,
        };
    }
    // Disabled / pointer-events:none / aria-disabled / inert ancestor.
    let clickable = !target.disabled
        && target.getAttribute('aria-disabled') !== 'true'
        && cs.pointerEvents !== 'none';
    if (clickable) {
        // inert is set on an ancestor when a modal traps focus.
        let n = target;
        while (n) {
            if (n.inert === true || n.hasAttribute && n.hasAttribute('inert')) {
                clickable = false;
                break;
            }
            n = n.parentElement;
        }
    }

    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const inViewport = (r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh);
    if (!inViewport) {
        return {
            status: 'offscreen', visible: true, inViewport: false,
            clickable: clickable,
            bbox: [r.x, r.y, r.width, r.height],
            obstruction: null,
        };
    }
    if (!clickable) {
        return {
            status: 'unclickable', visible: true, inViewport: true,
            clickable: false,
            bbox: [r.x, r.y, r.width, r.height],
            obstruction: null,
        };
    }
    // Hit-test the center of the visible portion of the target.
    const cx = Math.max(0, Math.min(vw - 1, r.left + r.width / 2));
    const cy = Math.max(0, Math.min(vh - 1, r.top + r.height / 2));
    let hit;
    try {
        hit = document.elementFromPoint(cx, cy);
    } catch (e) { hit = null; }
    if (!hit) {
        return {
            status: 'ok', visible: true, inViewport: true, clickable: true,
            bbox: [r.x, r.y, r.width, r.height], obstruction: null,
        };
    }
    if (hit === target || target.contains(hit) || hit.contains(target)) {
        return {
            status: 'ok', visible: true, inViewport: true, clickable: true,
            bbox: [r.x, r.y, r.width, r.height], obstruction: null,
        };
    }
    return {
        status: 'blocked', visible: true, inViewport: true, clickable: true,
        bbox: [r.x, r.y, r.width, r.height],
        obstruction: summarize(hit),
    };
}
"""

_SCROLL_JS = r"""
(selector) => {
    const el = document.querySelector(selector);
    if (!el || !el.scrollIntoView) return false;
    try {
        el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
    } catch (e) {
        try { el.scrollIntoView(); } catch (e2) {}
    }
    return true;
}
"""

_HIDE_JS = r"""
(info) => {
    if (!info) return false;
    // Find the most specific match; fall back to tag-only if needed.
    let candidates = [];
    if (info.id) {
        const byId = document.getElementById(info.id);
        if (byId) candidates.push(byId);
    }
    if (!candidates.length && info.classes) {
        const cls = (info.classes || '').split(/\s+/).filter(Boolean)[0];
        if (cls) {
            try {
                const list = document.getElementsByClassName(cls);
                for (const el of list) candidates.push(el);
            } catch (e) {}
        }
    }
    if (!candidates.length && info.tag) {
        const list = document.getElementsByTagName(info.tag);
        for (const el of list) {
            // Only hide elements whose role/text resemble the probed one.
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            const text = ((el.innerText || el.textContent || '') + '').trim().slice(0, 80);
            if (text === info.text) candidates.push(el);
        }
    }
    let hidden = 0;
    for (const el of candidates) {
        try {
            el.style.setProperty('display', 'none', 'important');
            el.setAttribute('data-automation-hidden', '1');
            hidden += 1;
        } catch (e) {}
    }
    return hidden > 0;
}
"""
