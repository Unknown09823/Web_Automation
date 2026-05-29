"""Multi-signal success verification.

Do not mark a goal successful merely because steps executed without error.
Verify actual outcomes using multiple signals:
  - URL changed to expected pattern
  - Success message visible on page
  - Target element appeared / disappeared
  - Page title changed
  - No error messages visible
  - Specific text present in page body
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class VerificationResult:
    """Outcome of a multi-signal verification."""

    passed: bool
    confidence: float  # 0.0 - 1.0
    signals: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "confidence": self.confidence,
            "signals": self.signals,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
        }



# Common success/error patterns
_SUCCESS_PATTERNS = [
    "success", "successfully", "created", "registered", "welcome",
    "logged in", "dashboard", "complete", "done", "verified",
    "confirmed", "account created", "profile", "thank you",
]

_ERROR_PATTERNS = [
    "error", "failed", "invalid", "incorrect", "wrong",
    "try again", "not found", "already exists", "duplicate",
    "expired", "denied", "forbidden", "unauthorized",
]


class SuccessVerifier:
    """Verify goal completion using multiple independent signals.

    Each signal contributes a weight to the overall confidence score.
    A goal is considered verified when total confidence >= threshold.
    """

    def __init__(self, *, threshold: float = 0.6) -> None:
        self.threshold = threshold

    async def verify(
        self,
        page: Any,
        *,
        goal: str = "",
        hints: list[str] | None = None,
        before_url: str = "",
        expected_url_part: str | None = None,
    ) -> VerificationResult:
        """Run all verification signals and compute confidence."""
        started = time.time()
        signals: list[dict[str, Any]] = []
        hints = hints or []

        # Signal 1: No error messages visible
        error_signal = await self._check_no_errors(page)
        signals.append(error_signal)

        # Signal 2: URL changed (if we had a before_url)
        if before_url:
            url_signal = self._check_url_change(page, before_url, expected_url_part)
            signals.append(url_signal)

        # Signal 3: Success message visible
        success_signal = await self._check_success_message(page)
        signals.append(success_signal)

        # Signal 4: Hint text present
        if hints:
            hint_signal = await self._check_hints(page, hints)
            signals.append(hint_signal)

        # Signal 5: Page appears functional (no crash/blank)
        health_signal = await self._check_page_health(page)
        signals.append(health_signal)

        # Compute weighted confidence
        total_weight = sum(s.get("weight", 1.0) for s in signals)
        weighted_score = sum(
            s.get("score", 0.0) * s.get("weight", 1.0) for s in signals
        )
        confidence = weighted_score / max(total_weight, 0.01)
        passed = confidence >= self.threshold

        duration_ms = int((time.time() - started) * 1000)
        reason = (
            f"verification {'passed' if passed else 'failed'}: "
            f"confidence={confidence:.2f} (threshold={self.threshold})"
        )

        return VerificationResult(
            passed=passed,
            confidence=round(confidence, 3),
            signals=signals,
            reason=reason,
            duration_ms=duration_ms,
        )

    async def _check_no_errors(self, page: Any) -> dict[str, Any]:
        """Check that no error messages are visible."""
        try:
            text = await page.evaluate(
                "() => (document.body.innerText || '').toLowerCase().slice(0, 5000)"
            )
            errors_found = [p for p in _ERROR_PATTERNS if p in text]
            if errors_found:
                return {
                    "name": "no_errors",
                    "score": 0.2,
                    "weight": 1.5,
                    "detail": f"errors found: {errors_found[:3]}",
                }
            return {"name": "no_errors", "score": 1.0, "weight": 1.5, "detail": "clean"}
        except Exception:  # noqa: BLE001
            return {"name": "no_errors", "score": 0.5, "weight": 1.0, "detail": "could not check"}

    def _check_url_change(
        self, page: Any, before: str, expected_part: str | None
    ) -> dict[str, Any]:
        """Check if URL changed and optionally contains expected part."""
        try:
            current = page.url
        except Exception:  # noqa: BLE001
            return {"name": "url_change", "score": 0.5, "weight": 1.0, "detail": "no url"}
        if current == before:
            return {
                "name": "url_change", "score": 0.3, "weight": 1.0,
                "detail": "url unchanged",
            }
        if expected_part and expected_part in current:
            return {
                "name": "url_change", "score": 1.0, "weight": 1.5,
                "detail": f"url contains {expected_part}",
            }
        return {
            "name": "url_change", "score": 0.7, "weight": 1.0,
            "detail": f"url changed to {current[:100]}",
        }

    async def _check_success_message(self, page: Any) -> dict[str, Any]:
        """Look for success-related text on the page."""
        try:
            text = await page.evaluate(
                "() => (document.body.innerText || '').toLowerCase().slice(0, 5000)"
            )
            matches = [p for p in _SUCCESS_PATTERNS if p in text]
            if matches:
                return {
                    "name": "success_message", "score": 1.0, "weight": 1.2,
                    "detail": f"found: {matches[:3]}",
                }
            return {
                "name": "success_message", "score": 0.4, "weight": 0.8,
                "detail": "no success text found",
            }
        except Exception:  # noqa: BLE001
            return {
                "name": "success_message", "score": 0.5, "weight": 0.5,
                "detail": "could not check",
            }

    async def _check_hints(self, page: Any, hints: list[str]) -> dict[str, Any]:
        """Check if any of the provided hints match page content."""
        try:
            text = await page.evaluate(
                "() => (document.body.innerText || '').toLowerCase().slice(0, 8000)"
            )
            url = page.url.lower()
            title = await page.title()
            combined = f"{text} {url} {title.lower()}"
            matched = [h for h in hints if h.lower() in combined]
            if matched:
                return {
                    "name": "hints", "score": 1.0, "weight": 1.5,
                    "detail": f"matched: {matched[:3]}",
                }
            return {
                "name": "hints", "score": 0.3, "weight": 1.0,
                "detail": "no hints matched",
            }
        except Exception:  # noqa: BLE001
            return {"name": "hints", "score": 0.5, "weight": 0.5, "detail": "could not check"}

    async def _check_page_health(self, page: Any) -> dict[str, Any]:
        """Basic check that the page is alive and has content."""
        try:
            ready = await page.evaluate("() => document.readyState")
            body_len = await page.evaluate(
                "() => (document.body.innerText || '').length"
            )
            if ready == "complete" and body_len > 10:
                return {
                    "name": "page_health", "score": 1.0, "weight": 0.5,
                    "detail": f"ready={ready} body_len={body_len}",
                }
            return {
                "name": "page_health", "score": 0.3, "weight": 0.5,
                "detail": f"ready={ready} body_len={body_len}",
            }
        except Exception:  # noqa: BLE001
            return {
                "name": "page_health", "score": 0.0, "weight": 1.0,
                "detail": "page unreachable",
            }
