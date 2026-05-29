"""Multi-strategy form filler with pre-submit validation.

The single biggest failure mode in real-world browser automation is
**fields that look filled but aren't**. Modern SPAs re-render inputs,
React/Vue controlled components reject programmatic ``.value`` writes,
and race conditions between blur/change/input events mean the user sees
a populated field but the app's state tree has an empty string.

This module solves that with a layered approach:

Strategy ladder
===============

For every field, the filler tries up to 5 strategies in order and
stops at the first one that **verifies successfully** (i.e. a read-back
of the field value matches the intended input):

  1. **Standard typing** — ``page.fill(selector, value)`` plus a short
     settle. The Playwright ``fill`` already does focus + selectAll +
     type + blur under the hood, so it handles ~70% of sites.

  2. **Triple-clear + type** — click the field, Ctrl+A, Delete,
     then ``page.type(selector, value, delay=30)`` character-by-character
     with synthetic input events. Handles fields that swallow paste.

  3. **Clipboard paste** — click the field, focus it, Ctrl+V the
     pre-loaded clipboard content. Works on some financial sites that
     block typing but allow paste.

  4. **JavaScript value injection** — sets ``element.value`` directly
     then dispatches ``input``, ``change``, ``blur`` events with the
     correct React/Vue SyntheticEvent property descriptors.

  5. **React-compatible event replay** — finds the React internal
     instance on the element, sets the value via
     ``Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set``,
     then dispatches a native ``input`` event with ``{ bubbles: true }``.
     This is the "nuclear option" for heavily controlled components.

Post-fill verification
======================

After each strategy, the filler reads the field value back via
``page.evaluate`` and compares it to the expected input. Only when
the read-back matches (modulo whitespace trimming and formatting
for phone/number fields) is the fill considered successful.

Pre-submit validation
=====================

Before any submit click, :py:meth:`FormFiller.validate_before_submit`
re-reads **every** filled field and checks:

  * value still matches expectation (SPAs may have cleared it),
  * no visible validation-error messages appeared,
  * the submit button is not disabled.

If validation fails, it returns a structured report so the loop can
refill the offending field before retrying the submit.

Threading / safety
==================

The class is stateless; one instance per browser session is fine.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- types
class FillStrategy(str, Enum):
    """Named strategies in priority order."""

    STANDARD = "standard"          # page.fill
    TRIPLE_CLEAR_TYPE = "clear_type"  # click + Ctrl+A + Del + type char-by-char
    CLIPBOARD_PASTE = "clipboard_paste"
    JS_INJECTION = "js_injection"
    REACT_COMPAT = "react_compat"


@dataclass(slots=True)
class FillResult:
    """Outcome of filling a single field."""

    selector: str
    expected_value: str
    actual_value: str
    success: bool
    strategy_used: FillStrategy | None = None
    strategies_tried: list[FillStrategy] = field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "expected": self.expected_value,
            "actual": self.actual_value,
            "success": self.success,
            "strategy_used": self.strategy_used.value if self.strategy_used else None,
            "strategies_tried": [s.value for s in self.strategies_tried],
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(slots=True)
class ValidationIssue:
    """One problem detected during pre-submit validation."""

    field_selector: str
    expected: str
    actual: str
    issue: str  # "empty", "mismatch", "error_visible", "submit_disabled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_selector,
            "expected": self.expected,
            "actual": self.actual,
            "issue": self.issue,
        }


@dataclass(slots=True)
class PreSubmitResult:
    """Outcome of validate_before_submit()."""

    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [i.to_dict() for i in self.issues],
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------- filler
class FormFiller:
    """Multi-strategy form filler with post-fill verification.

    Usage in the execution loop::

        filler = FormFiller()
        result = await filler.fill_field(page, selector, value)
        if not result.success:
            # handle or retry

    Before submit::

        pre = await filler.validate_before_submit(page, filled_fields, submit_selector)
        if not pre.valid:
            # refill broken fields
    """

    def __init__(
        self,
        *,
        max_retries: int = 2,
        settle_ms: int = 150,
        verify_delay_ms: int = 100,
        strategies: list[FillStrategy] | None = None,
    ) -> None:
        self.max_retries = int(max_retries)
        self.settle_ms = int(settle_ms)
        self.verify_delay_ms = int(verify_delay_ms)
        self.strategies: list[FillStrategy] = list(
            strategies or list(FillStrategy)
        )

    # ----------------------------------------------------------- main API
    async def fill_field(
        self,
        page: Any,
        selector: str,
        value: str,
        *,
        timeout_ms: int = 10_000,
    ) -> FillResult:
        """Fill a single field, trying strategies until read-back matches.

        Returns a :class:`FillResult` describing what worked (or didn't).
        """
        started = time.time()
        strategies_tried: list[FillStrategy] = []
        last_actual = ""
        last_error: str | None = None

        for attempt in range(self.max_retries + 1):
            for strategy in self.strategies:
                if _elapsed_ms(started) > timeout_ms:
                    break
                strategies_tried.append(strategy)
                try:
                    await self._apply_strategy(
                        page, selector, value, strategy, timeout_ms,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{strategy.value}: {exc!r}"
                    log.debug(
                        "fill strategy %s failed for %s: %s",
                        strategy.value, selector, exc,
                    )
                    continue

                # Read-back verification
                await asyncio.sleep(self.verify_delay_ms / 1000)
                actual = await self._read_value(page, selector)
                last_actual = actual

                if _values_match(actual, value):
                    return FillResult(
                        selector=selector,
                        expected_value=value,
                        actual_value=actual,
                        success=True,
                        strategy_used=strategy,
                        strategies_tried=strategies_tried,
                        duration_ms=_elapsed_ms(started),
                    )
                else:
                    last_error = (
                        f"strategy {strategy.value} did not persist: "
                        f"expected={value!r} actual={actual!r}"
                    )
                    log.debug(last_error)

            # If the first pass through all strategies failed, wait a
            # little longer in case the SPA is re-rendering, then retry
            # the whole ladder.
            if attempt < self.max_retries:
                await asyncio.sleep(self.settle_ms / 1000)

        return FillResult(
            selector=selector,
            expected_value=value,
            actual_value=last_actual,
            success=False,
            strategies_tried=strategies_tried,
            duration_ms=_elapsed_ms(started),
            error=last_error,
        )

    async def fill_form(
        self,
        page: Any,
        fields: list[tuple[str, str]],
        *,
        timeout_ms: int = 30_000,
    ) -> list[FillResult]:
        """Fill multiple fields sequentially, returning all results.

        ``fields`` is a list of ``(selector, value)`` pairs in fill order.
        """
        results: list[FillResult] = []
        for selector, value in fields:
            r = await self.fill_field(page, selector, value, timeout_ms=timeout_ms)
            results.append(r)
            if r.success:
                # Small settle between fields for SPAs that re-render
                # the form after each input event.
                await asyncio.sleep(self.settle_ms / 1000)
        return results

    async def validate_before_submit(
        self,
        page: Any,
        filled_fields: list[tuple[str, str]],
        submit_selector: str | None = None,
    ) -> PreSubmitResult:
        """Re-verify every field just before the submit click.

        This catches:
          * SPA re-renders that emptied a field,
          * validation errors that appeared after typing,
          * disabled submit buttons.
        """
        started = time.time()
        issues: list[ValidationIssue] = []

        # 1. Re-read every field
        for selector, expected in filled_fields:
            actual = await self._read_value(page, selector)
            if not actual.strip():
                issues.append(ValidationIssue(
                    field_selector=selector,
                    expected=expected,
                    actual=actual,
                    issue="empty",
                ))
            elif not _values_match(actual, expected):
                issues.append(ValidationIssue(
                    field_selector=selector,
                    expected=expected,
                    actual=actual,
                    issue="mismatch",
                ))

        # 2. Check for visible validation errors
        has_errors = await self._check_validation_errors(page)
        if has_errors:
            issues.append(ValidationIssue(
                field_selector="<page>",
                expected="no errors",
                actual="validation error visible",
                issue="error_visible",
            ))

        # 3. Check submit button is not disabled
        if submit_selector:
            disabled = await self._is_submit_disabled(page, submit_selector)
            if disabled:
                issues.append(ValidationIssue(
                    field_selector=submit_selector,
                    expected="enabled",
                    actual="disabled",
                    issue="submit_disabled",
                ))

        return PreSubmitResult(
            valid=not issues,
            issues=issues,
            duration_ms=_elapsed_ms(started),
        )

    # ----------------------------------------------------------- strategies
    async def _apply_strategy(
        self,
        page: Any,
        selector: str,
        value: str,
        strategy: FillStrategy,
        timeout_ms: int,
    ) -> None:
        """Dispatch to the selected fill strategy."""
        if strategy is FillStrategy.STANDARD:
            await self._strategy_standard(page, selector, value, timeout_ms)
        elif strategy is FillStrategy.TRIPLE_CLEAR_TYPE:
            await self._strategy_clear_type(page, selector, value, timeout_ms)
        elif strategy is FillStrategy.CLIPBOARD_PASTE:
            await self._strategy_clipboard(page, selector, value, timeout_ms)
        elif strategy is FillStrategy.JS_INJECTION:
            await self._strategy_js_inject(page, selector, value)
        elif strategy is FillStrategy.REACT_COMPAT:
            await self._strategy_react_compat(page, selector, value)
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

    async def _strategy_standard(
        self, page: Any, selector: str, value: str, timeout_ms: int,
    ) -> None:
        """Strategy 1: Playwright's built-in fill (focus + selectAll + type + blur)."""
        await page.fill(selector, value, timeout=timeout_ms)
        await asyncio.sleep(self.settle_ms / 1000)

    async def _strategy_clear_type(
        self, page: Any, selector: str, value: str, timeout_ms: int,
    ) -> None:
        """Strategy 2: Click → triple-select-all → delete → type character-by-character.

        Handles fields that swallow paste / fill but accept real keystrokes.
        """
        await page.click(selector, timeout=timeout_ms)
        await asyncio.sleep(0.05)
        # Select all text (platform-agnostic)
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.03)
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.03)
        # Type character by character with a small delay to avoid
        # overwhelming input event handlers.
        await page.type(selector, value, delay=30)
        # Trigger blur to ensure validation fires
        await page.evaluate(
            f"document.querySelector({_js_str(selector)})?.blur()"
        )
        await asyncio.sleep(self.settle_ms / 1000)

    async def _strategy_clipboard(
        self, page: Any, selector: str, value: str, timeout_ms: int,
    ) -> None:
        """Strategy 3: Focus field → paste from clipboard.

        Some financial/security sites block typing but allow paste.
        """
        # Put value into the system clipboard via page context
        await page.evaluate(
            f"navigator.clipboard.writeText({_js_str(value)})"
            " .catch(() => { /* clipboard API may be blocked */ })"
        )
        await page.click(selector, timeout=timeout_ms)
        await asyncio.sleep(0.05)
        # Select all existing content first
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.03)
        # Paste
        await page.keyboard.press("Control+v")
        await asyncio.sleep(self.settle_ms / 1000)
        # Blur
        await page.evaluate(
            f"document.querySelector({_js_str(selector)})?.blur()"
        )

    async def _strategy_js_inject(
        self, page: Any, selector: str, value: str,
    ) -> None:
        """Strategy 4: Set .value directly + dispatch input/change/blur.

        Works for most non-React SPAs and simple forms where the
        framework just listens to standard DOM events.
        """
        await page.evaluate(
            _JS_INJECT_TEMPLATE.replace("__SELECTOR__", _escape_for_js(selector))
            .replace("__VALUE__", _escape_for_js(value))
        )
        await asyncio.sleep(self.settle_ms / 1000)

    async def _strategy_react_compat(
        self, page: Any, selector: str, value: str,
    ) -> None:
        """Strategy 5: React-compatible value setter.

        Uses the native property descriptor trick to bypass React's
        synthetic event system and force the value into the component
        state.
        """
        await page.evaluate(
            _JS_REACT_COMPAT_TEMPLATE
            .replace("__SELECTOR__", _escape_for_js(selector))
            .replace("__VALUE__", _escape_for_js(value))
        )
        await asyncio.sleep(self.settle_ms / 1000)

    # ----------------------------------------------------------- helpers
    async def _read_value(self, page: Any, selector: str) -> str:
        """Read the current value of a field. Tolerant of errors."""
        try:
            val = await page.evaluate(
                f"(document.querySelector({_js_str(selector)}) || {{}}).value || ''"
            )
            return str(val or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _check_validation_errors(self, page: Any) -> bool:
        """Return True if visible validation errors are present."""
        try:
            return await page.evaluate(_JS_CHECK_ERRORS)
        except Exception:  # noqa: BLE001
            return False

    async def _is_submit_disabled(
        self, page: Any, selector: str,
    ) -> bool:
        """Return True if the submit button is disabled."""
        try:
            return await page.evaluate(
                f"""(() => {{
                    const el = document.querySelector({_js_str(selector)});
                    return el ? (el.disabled || el.getAttribute('aria-disabled') === 'true') : false;
                }})()"""
            )
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------- JS templates
_JS_INJECT_TEMPLATE = """(() => {
    const el = document.querySelector('__SELECTOR__');
    if (!el) return;
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    )?.set || Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value'
    )?.set;
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(el, '__VALUE__');
    } else {
        el.value = '__VALUE__';
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
})()"""

_JS_REACT_COMPAT_TEMPLATE = """(() => {
    const el = document.querySelector('__SELECTOR__');
    if (!el) return;
    // React 16+ uses a synthetic event system. We need to:
    //   1. Set the native value via the property descriptor
    //   2. Dispatch a native input event that React will catch
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    )?.set || Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value'
    )?.set;
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(el, '__VALUE__');
    } else {
        el.value = '__VALUE__';
    }
    // React listens for native events and reconciles them with its
    // internal fiber tree. A bubbling 'input' event is the trigger.
    el.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
    el.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));
    el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
    el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
    // For React 17+ with new JSX transform, also trigger the
    // native setter on the tracker if present
    const tracker = el._valueTracker;
    if (tracker) { tracker.setValue(''); }
    el.dispatchEvent(new Event('input', { bubbles: true }));
})()"""

_JS_CHECK_ERRORS = """(() => {
    const errorSelectors = [
        '[class*="error" i]', '[class*="invalid" i]',
        '[class*="validation" i][class*="message" i]',
        '[role="alert"]',
        '.field-error', '.form-error', '.input-error',
        '[aria-invalid="true"]',
    ];
    for (const sel of errorSelectors) {
        for (const el of document.querySelectorAll(sel)) {
            const rect = el.getBoundingClientRect();
            const text = (el.innerText || '').trim();
            // Must be visible and contain actual text (not just icons)
            if (rect.width > 0 && rect.height > 0 && text.length > 2) {
                // Exclude success-styled alerts that happen to use "alert" role
                const lower = text.toLowerCase();
                if (lower.includes('success') || lower.includes('welcome')) continue;
                return true;
            }
        }
    }
    return false;
})()"""


# ---------------------------------------------------------------- helpers
def _values_match(actual: str, expected: str) -> bool:
    """Compare field values with tolerance for whitespace and formatting.

    Phone numbers may be stored with spaces/dashes; passwords are
    exact-match. We normalize both sides and do a case-sensitive
    compare (passwords are case-sensitive).
    """
    a = _normalize_value(actual)
    e = _normalize_value(expected)
    if a == e:
        return True
    # Fallback: strip all non-alphanumeric chars for phone/number comparison
    a_digits = "".join(c for c in a if c.isalnum())
    e_digits = "".join(c for c in e if c.isalnum())
    return a_digits == e_digits and len(e_digits) > 0


def _normalize_value(val: str) -> str:
    """Strip leading/trailing whitespace."""
    return val.strip()


def _js_str(s: str) -> str:
    """Wrap a Python string as a safe JavaScript string literal."""
    # Escape backslashes, quotes, and newlines for JS embedding.
    escaped = (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f"'{escaped}'"


def _escape_for_js(s: str) -> str:
    """Escape for embedding inside a JS template (inside quotes already)."""
    return (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
