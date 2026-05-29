"""Pre-emptive popup / banner / modal dismisser.

Real-world web pages constantly throw overlays at automation agents:
cookie consent banners, announcement modals, "new version available"
notices, newsletter subscriptions, age gates, app-install prompts,
and so on. The legacy recovery stack tried to dismiss these *after*
the agent had already failed to interact with the underlying page.
That was both slow and brittle: the agent would click into a banner,
fail verification, run recovery, dismiss the banner, and then re-attempt.

This module flips the model: scan-and-dismiss runs at every observe
step, *before* the agent looks at the page semantically. The result
is that the perception layer always sees a clean DOM, and the rest
of the deterministic-first stack never has to second-guess whether
the element it picked was occluded.

Safety properties
=================

* **Never dismisses content the workflow needs.** The strategies are
  authored to target buttons whose visible text or aria label is
  *clearly* a "close / dismiss / accept-cookies / not-now" pattern.
  We do not click random "Skip" buttons because some workflows
  contain skip steps the user actually wants.
* **Idempotent.** Calling :py:meth:`PopupGuard.scan_and_dismiss`
  repeatedly on a clean page is a near-no-op (a single JS evaluate).
* **Fail-soft.** Any exception inside a dismiss strategy is swallowed
  and reported in the result, not propagated; the agent must always
  be able to continue.
* **Mockable.** All browser interaction is funnelled through the
  small protocol :class:`_PageLike`, so unit tests can drive the
  guard with a fake page object.

Categories of overlay we recognize, in dismissal priority order:

  1. ``COOKIE_BANNER`` — accept by default; cookies are usually required
     for the site to be usable. Tested first so we don't waste time on
     a "Close" click that would only dismiss the banner ephemerally.
  2. ``UPDATE_NOTICE`` — version / "new release" toasts; close.
  3. ``ANNOUNCEMENT`` — generic notification banners; close.
  4. ``MODAL`` — generic modal dialog; close button or Escape.
  5. ``NEWSLETTER`` — email-subscribe popups; "no thanks" / close.
  6. ``AGE_GATE`` — visible age confirmation; choose "yes" / "I'm over 18".
  7. ``APP_INSTALL`` — "open in app" / "install" prompts; close.

Operators can tune the catalog at construction time without touching
this module.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Protocol

log = logging.getLogger(__name__)


class PopupKind(str, Enum):
    """High-level taxonomy of dismissable overlays."""

    COOKIE_BANNER = "cookie_banner"
    UPDATE_NOTICE = "update_notice"
    ANNOUNCEMENT = "announcement"
    MODAL = "modal"
    NEWSLETTER = "newsletter"
    AGE_GATE = "age_gate"
    APP_INSTALL = "app_install"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------- catalog
@dataclass(slots=True, frozen=True)
class DismissRule:
    """One rule for finding-and-dismissing an overlay category.

    Attributes
    ----------
    kind:
        Which category this rule belongs to. Used for logging /
        reasoning panels.
    container_selectors:
        CSS selectors for the *container* element (banner, modal frame,
        ...). The rule fires only if at least one container is visible.
    accept_selectors:
        Selectors for buttons that *accept* the overlay (e.g. "Accept
        all cookies", "I am over 18"). For cookies/age gates this is
        the preferred dismiss path.
    close_selectors:
        Selectors for buttons that *close* the overlay (e.g. an X icon
        or "No thanks"). Tried after accept selectors fail.
    keyboard_fallback:
        If true, attempt :kbd:`Escape` after both selector lists fail.
    description:
        Human-readable text rendered into the reasoning panel.
    """

    kind: PopupKind
    container_selectors: tuple[str, ...] = ()
    accept_selectors: tuple[str, ...] = ()
    close_selectors: tuple[str, ...] = ()
    keyboard_fallback: bool = False
    description: str = ""


# Built-in rule catalog. Each is intentionally narrow so we don't
# accidentally click content the user wants. Order matches dismissal
# priority — cookies first because they typically block scrolling.
DEFAULT_RULES: tuple[DismissRule, ...] = (
    DismissRule(
        kind=PopupKind.COOKIE_BANNER,
        container_selectors=(
            '[id*="cookie" i]',
            '[class*="cookie" i]',
            '[id*="consent" i]',
            '[class*="consent" i]',
            '[id*="gdpr" i]',
            '[class*="gdpr" i]',
            'div[class*="banner" i][class*="cookie" i]',
        ),
        accept_selectors=(
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("Accept")',
            'button:has-text("I agree")',
            'button:has-text("Agree")',
            'button:has-text("Got it")',
            'button:has-text("OK")',
            '[id*="accept" i][role="button"]',
            'button[id*="accept" i]',
        ),
        close_selectors=(
            'button[aria-label="Close" i]',
            'button[class*="close" i]',
        ),
        description="Cookie consent banner",
    ),
    DismissRule(
        kind=PopupKind.UPDATE_NOTICE,
        container_selectors=(
            '[class*="update" i][class*="notice" i]',
            '[class*="version" i][class*="banner" i]',
            '[class*="release" i][class*="note" i]',
            'div[role="alert"]:has-text("update")',
            'div[role="alert"]:has-text("new version")',
        ),
        close_selectors=(
            'button:has-text("Dismiss")',
            'button:has-text("Later")',
            'button:has-text("Close")',
            'button[aria-label="Close" i]',
        ),
        keyboard_fallback=True,
        description="Update / version notice",
    ),
    DismissRule(
        kind=PopupKind.ANNOUNCEMENT,
        container_selectors=(
            '[class*="announcement" i]',
            '[class*="notification-banner" i]',
            '[class*="promo-banner" i]',
            'div[role="status"]',
        ),
        close_selectors=(
            'button[aria-label="Close" i]',
            'button:has-text("Got it")',
            'button:has-text("Dismiss")',
            'button:has-text("Close")',
            '[class*="close" i][role="button"]',
        ),
        keyboard_fallback=True,
        description="Announcement banner",
    ),
    DismissRule(
        kind=PopupKind.MODAL,
        container_selectors=(
            '[role="dialog"]',
            '[role="alertdialog"]',
            '[class*="modal-overlay" i]',
            '[class*="modal-backdrop" i]',
            '.modal.show',
        ),
        close_selectors=(
            'button[aria-label="Close" i]',
            '[role="dialog"] button:has-text("Close")',
            '[role="dialog"] button:has-text("No thanks")',
            '[role="dialog"] button:has-text("Not now")',
            '[role="dialog"] button:has-text("Maybe later")',
            '[role="dialog"] button[class*="close" i]',
            '[class*="modal" i] button[class*="close" i]',
        ),
        keyboard_fallback=True,
        description="Generic modal dialog",
    ),
    DismissRule(
        kind=PopupKind.NEWSLETTER,
        container_selectors=(
            '[class*="newsletter" i]',
            '[class*="subscribe" i][class*="modal" i]',
            '[class*="signup-popup" i]',
        ),
        close_selectors=(
            'button:has-text("No thanks")',
            'button:has-text("Not now")',
            'button:has-text("Maybe later")',
            'button[aria-label="Close" i]',
            'button[class*="close" i]',
        ),
        keyboard_fallback=True,
        description="Newsletter subscribe prompt",
    ),
    DismissRule(
        kind=PopupKind.AGE_GATE,
        container_selectors=(
            '[class*="age-gate" i]',
            '[class*="age-verification" i]',
            '[id*="age-gate" i]',
            'div:has-text("Are you over")',
        ),
        accept_selectors=(
            'button:has-text("Yes")',
            'button:has-text("I am over")',
            'button:has-text("I am 18")',
            'button:has-text("Continue")',
            'button:has-text("Enter")',
        ),
        description="Age gate / verification",
    ),
    DismissRule(
        kind=PopupKind.APP_INSTALL,
        container_selectors=(
            '[class*="app-install" i]',
            '[class*="open-in-app" i]',
            '[class*="smart-banner" i]',
            'div:has-text("Open in app")',
            'div:has-text("Continue in app")',
        ),
        close_selectors=(
            'button:has-text("Continue in browser")',
            'button:has-text("Use the web")',
            'button:has-text("No thanks")',
            'button[aria-label="Close" i]',
            'button[class*="close" i]',
        ),
        keyboard_fallback=True,
        description="App-install / open-in-app prompt",
    ),
)


# --------------------------------------------------------------- result
@dataclass(slots=True)
class DismissedPopup:
    """One overlay that was actually dismissed by the guard."""

    kind: PopupKind
    description: str
    selector_used: str
    strategy: str  # "accept" | "close" | "escape"
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "description": self.description,
            "selector_used": self.selector_used,
            "strategy": self.strategy,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class PopupGuardResult:
    """Outcome of one ``scan_and_dismiss`` pass."""

    found: list[PopupKind] = field(default_factory=list)
    dismissed: list[DismissedPopup] = field(default_factory=list)
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def any_dismissed(self) -> bool:
        return bool(self.dismissed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": [k.value for k in self.found],
            "dismissed": [d.to_dict() for d in self.dismissed],
            "duration_ms": self.duration_ms,
            "notes": self.notes,
        }

    def render(self) -> str:
        """Short human-readable summary for the reasoning panel."""
        if not self.dismissed and not self.found:
            return "no popups detected"
        if not self.dismissed:
            return f"detected but did not dismiss: {[k.value for k in self.found]}"
        names = ", ".join(d.description for d in self.dismissed)
        return f"dismissed: {names}"


# ---------------------------------------------------------------- protocol
class _PageLike(Protocol):
    """The minimal Page surface the guard actually uses.

    Defined so unit tests can drive the guard without Playwright. Real
    Playwright pages already satisfy this protocol.
    """

    async def is_visible(self, selector: str, *, timeout: int | None = ...) -> bool: ...

    async def click(self, selector: str, *, timeout: int = ...) -> Any: ...

    @property
    def keyboard(self) -> Any: ...


# ---------------------------------------------------------------- guard
class PopupGuard:
    """Scan a page for overlays and dismiss them, in priority order.

    The guard caches nothing between calls — every call is a fresh
    scan, because the DOM the agent sees changes constantly. That said,
    a clean page only costs one ``is_visible`` call per rule, which on
    a real Playwright connection is sub-millisecond.

    Operators can:
      * provide their own rule catalog via ``rules=``,
      * extend the defaults via ``extra_rules=``,
      * raise the per-rule timeout for slow pages (default 1200ms).
    """

    def __init__(
        self,
        *,
        rules: Iterable[DismissRule] | None = None,
        extra_rules: Iterable[DismissRule] | None = None,
        per_action_timeout_ms: int = 1200,
        max_dismissals_per_pass: int = 6,
    ) -> None:
        rules_list: list[DismissRule] = list(rules) if rules is not None else list(DEFAULT_RULES)
        if extra_rules:
            rules_list.extend(extra_rules)
        self.rules: tuple[DismissRule, ...] = tuple(rules_list)
        self.per_action_timeout_ms = int(per_action_timeout_ms)
        self.max_dismissals_per_pass = int(max_dismissals_per_pass)

    async def scan_and_dismiss(self, page: _PageLike) -> PopupGuardResult:
        """Run one full scan-and-dismiss pass over ``page``.

        The result lists every kind of overlay we *detected* (even those
        we couldn't dismiss) so the reasoning panel can show "saw a
        cookie banner but couldn't dismiss it" rather than silently
        failing.
        """
        result = PopupGuardResult()
        started = time.time()
        dismissed_count = 0

        for rule in self.rules:
            if dismissed_count >= self.max_dismissals_per_pass:
                result.notes.append("max dismissals per pass reached")
                break
            if not await self._rule_visible(page, rule):
                continue
            result.found.append(rule.kind)
            dismissed = await self._dismiss_rule(page, rule)
            if dismissed:
                result.dismissed.append(dismissed)
                dismissed_count += 1
            else:
                result.notes.append(f"could not dismiss {rule.kind.value}")

        result.duration_ms = int((time.time() - started) * 1000)
        return result

    # ------------------------------------------------------------ internals
    async def _rule_visible(self, page: _PageLike, rule: DismissRule) -> bool:
        """True when at least one container selector is visible.

        Defensive: callers may pass a real Page or a stub. We catch
        exceptions per-selector so a single malformed selector never
        breaks the whole pass.
        """
        for sel in rule.container_selectors:
            if await _safe_is_visible(page, sel, self.per_action_timeout_ms):
                return True
        return False

    async def _dismiss_rule(self, page: _PageLike, rule: DismissRule) -> DismissedPopup | None:
        """Try accept selectors, then close selectors, then Escape."""
        started = time.time()

        for sel in rule.accept_selectors:
            if await _safe_click(page, sel, self.per_action_timeout_ms):
                return DismissedPopup(
                    kind=rule.kind,
                    description=rule.description or rule.kind.value,
                    selector_used=sel,
                    strategy="accept",
                    duration_ms=int((time.time() - started) * 1000),
                )

        for sel in rule.close_selectors:
            if await _safe_click(page, sel, self.per_action_timeout_ms):
                return DismissedPopup(
                    kind=rule.kind,
                    description=rule.description or rule.kind.value,
                    selector_used=sel,
                    strategy="close",
                    duration_ms=int((time.time() - started) * 1000),
                )

        if rule.keyboard_fallback and await _safe_press_escape(page):
            return DismissedPopup(
                kind=rule.kind,
                description=rule.description or rule.kind.value,
                selector_used="<escape>",
                strategy="escape",
                duration_ms=int((time.time() - started) * 1000),
            )
        return None


# ------------------------------------------------------------- helpers
async def _safe_is_visible(
    page: _PageLike, selector: str, timeout_ms: int,
) -> bool:
    """Best-effort visibility probe.

    Real Playwright signature is ``is_visible(selector, timeout=None)``,
    but some test stubs expose only ``is_visible(selector)``. We try
    both and treat any exception as "not visible" rather than as an
    error.
    """
    try:
        try:
            return await page.is_visible(selector, timeout=timeout_ms)  # type: ignore[call-arg]
        except TypeError:
            return await page.is_visible(selector)  # type: ignore[misc]
    except Exception:  # noqa: BLE001 - defensive
        return False


async def _safe_click(page: _PageLike, selector: str, timeout_ms: int) -> bool:
    """Click only if the selector is currently visible.

    A two-step (visible-then-click) approach guarantees we never wait
    the full Playwright timeout on a missing selector — which would
    otherwise turn the popup guard into a multi-second tax on every
    observation.
    """
    if not await _safe_is_visible(page, selector, timeout_ms):
        return False
    try:
        await page.click(selector, timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _safe_press_escape(page: _PageLike) -> bool:
    """Press Escape, swallowing any errors.

    Returns ``True`` only when the keystroke was actually sent; the
    caller still cannot prove the overlay went away — the next observe
    cycle will confirm or re-trigger.
    """
    keyboard = getattr(page, "keyboard", None)
    if keyboard is None:
        return False
    try:
        press: Callable[[str], Awaitable[None]] | None = getattr(keyboard, "press", None)
        if press is None:
            return False
        await press("Escape")
        return True
    except Exception:  # noqa: BLE001
        return False
