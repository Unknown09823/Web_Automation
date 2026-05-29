"""Captcha detection, classification, and handling.

When the agent encounters a captcha during execution, it must:

  1. **Detect** — recognize that a captcha is present on the page.
  2. **Classify** — determine which *kind* of captcha it is.
  3. **Attempt** — try supported automated solutions.
  4. **Verify** — confirm whether the captcha was solved.
  5. **Escalate** — mark for human intervention if unsolvable.

Supported captcha types
=======================

  * ``CHECKBOX`` — simple "I'm not a robot" checkboxes (Turnstile, reCAPTCHA v2 checkbox)
  * ``ARITHMETIC`` — "What is 3 + 7?" text challenges
  * ``TEXT`` — "Type the characters you see" (basic OCR-able)
  * ``IMAGE`` — image selection grids ("Select all traffic lights")
  * ``TURNSTILE`` — Cloudflare Turnstile (invisible or managed)
  * ``RECAPTCHA`` — Google reCAPTCHA v2/v3
  * ``HCAPTCHA`` — hCaptcha challenges

For checkbox-style captchas the handler clicks the checkbox and
checks for success. For arithmetic captchas it evaluates the
expression. For all others it marks the task for human intervention
unless an external solver service is configured.

Integration
===========

The handler integrates into two places:

  * **popup_guard** — detects captcha overlays *before* the agent
    attempts form actions (proactive).
  * **rule_engine** — the ``captcha_human_handoff`` rule fires when
    the body text contains captcha keywords (reactive, already exists).
  * **loop.py** — after a step fails, the recovery path can invoke
    the handler to check if a captcha appeared mid-flow.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)



# ---------------------------------------------------------------- types
class CaptchaType(str, Enum):
    """Known captcha categories."""

    CHECKBOX = "checkbox"        # Simple "I'm not a robot" click
    ARITHMETIC = "arithmetic"   # "What is 3 + 7?"
    TEXT = "text"               # "Type the characters"
    IMAGE = "image"             # Image grid selection
    TURNSTILE = "turnstile"     # Cloudflare Turnstile
    RECAPTCHA = "recaptcha"     # Google reCAPTCHA
    HCAPTCHA = "hcaptcha"       # hCaptcha
    UNKNOWN = "unknown"         # Detected but unclassified


class CaptchaStatus(str, Enum):
    """Resolution status."""

    DETECTED = "detected"
    SOLVING = "solving"
    SOLVED = "solved"
    FAILED = "failed"
    HUMAN_REQUIRED = "human_required"


@dataclass(slots=True)
class CaptchaDetection:
    """Result of scanning a page for captchas."""

    detected: bool = False
    captcha_type: CaptchaType = CaptchaType.UNKNOWN
    confidence: float = 0.0
    selector: str = ""
    iframe_src: str = ""
    challenge_text: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "type": self.captcha_type.value,
            "confidence": self.confidence,
            "selector": self.selector,
            "iframe_src": self.iframe_src,
            "challenge_text": self.challenge_text,
            "duration_ms": self.duration_ms,
        }



@dataclass(slots=True)
class CaptchaResult:
    """Outcome of attempting to solve a captcha."""

    status: CaptchaStatus
    captcha_type: CaptchaType = CaptchaType.UNKNOWN
    solution: str = ""
    attempts: int = 0
    duration_ms: int = 0
    error: str | None = None

    @property
    def solved(self) -> bool:
        return self.status is CaptchaStatus.SOLVED

    @property
    def needs_human(self) -> bool:
        return self.status is CaptchaStatus.HUMAN_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "type": self.captcha_type.value,
            "solution": self.solution[:20] if self.solution else "",
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }



# ---------------------------------------------------------------- detection selectors
# Each entry: (selector_or_pattern, CaptchaType, confidence, description)
_DETECTION_RULES: list[tuple[str, CaptchaType, float, str]] = [
    # Cloudflare Turnstile
    ('iframe[src*="challenges.cloudflare.com"]', CaptchaType.TURNSTILE, 0.95, "Turnstile iframe"),
    ('[class*="cf-turnstile"]', CaptchaType.TURNSTILE, 0.90, "Turnstile container"),
    ('#cf-turnstile-response', CaptchaType.TURNSTILE, 0.85, "Turnstile response field"),

    # Google reCAPTCHA
    ('iframe[src*="google.com/recaptcha"]', CaptchaType.RECAPTCHA, 0.95, "reCAPTCHA iframe"),
    ('[class*="g-recaptcha"]', CaptchaType.RECAPTCHA, 0.90, "reCAPTCHA container"),
    ('#g-recaptcha-response', CaptchaType.RECAPTCHA, 0.85, "reCAPTCHA response"),
    ('iframe[title*="reCAPTCHA"]', CaptchaType.RECAPTCHA, 0.90, "reCAPTCHA titled iframe"),

    # hCaptcha
    ('iframe[src*="hcaptcha.com"]', CaptchaType.HCAPTCHA, 0.95, "hCaptcha iframe"),
    ('[class*="h-captcha"]', CaptchaType.HCAPTCHA, 0.90, "hCaptcha container"),
    ('[data-hcaptcha-widget-id]', CaptchaType.HCAPTCHA, 0.85, "hCaptcha widget"),

    # Generic checkbox captchas
    ('[class*="captcha"] input[type="checkbox"]', CaptchaType.CHECKBOX, 0.80, "Captcha checkbox"),
    ('[id*="captcha"] input[type="checkbox"]', CaptchaType.CHECKBOX, 0.75, "Captcha ID checkbox"),

    # Image captchas
    ('[class*="captcha"] img', CaptchaType.IMAGE, 0.70, "Captcha with image"),
    ('img[src*="captcha"]', CaptchaType.IMAGE, 0.75, "Captcha image src"),
    ('canvas[class*="captcha"]', CaptchaType.IMAGE, 0.70, "Captcha canvas"),

    # Text/arithmetic captchas (detected via body text, not selectors)
    ('[class*="captcha"] input[type="text"]', CaptchaType.TEXT, 0.65, "Captcha text input"),
    ('[id*="captcha"] input[type="text"]', CaptchaType.TEXT, 0.65, "Captcha ID text input"),
]



# Body text patterns for classification
_ARITHMETIC_PATTERNS = [
    re.compile(r"what\s+is\s+(\d+)\s*([+\-*/x×])\s*(\d+)", re.IGNORECASE),
    re.compile(r"solve\s*:?\s*(\d+)\s*([+\-*/x×])\s*(\d+)", re.IGNORECASE),
    re.compile(r"(\d+)\s*([+\-*/x×])\s*(\d+)\s*=\s*\?", re.IGNORECASE),
    re.compile(r"calculate\s*:?\s*(\d+)\s*([+\-*/x×])\s*(\d+)", re.IGNORECASE),
]

_CAPTCHA_TEXT_INDICATORS = [
    "captcha", "verify you are human", "are you human",
    "i'm not a robot", "i am not a robot", "prove you're human",
    "security check", "bot detection", "human verification",
    "complete the challenge", "solve the puzzle",
    "type the characters", "enter the code",
    "press and hold", "slide to verify",
]


# ---------------------------------------------------------------- handler
class CaptchaHandler:
    """Detect, classify, and attempt to solve captchas.

    The handler is deliberately conservative: it only auto-solves
    captcha types where the success rate is near 100% (checkbox clicks,
    arithmetic). Everything else is escalated to human intervention.

    External solver services (2captcha, anti-captcha, etc.) can be
    plugged in via ``solver_callback`` — the handler invokes it with
    the captcha type and any relevant data (image bytes, site key),
    and expects a solution string back.
    """

    def __init__(
        self,
        *,
        solver_callback: Any = None,
        max_attempts: int = 2,
        post_solve_wait_ms: int = 2000,
    ) -> None:
        self.solver_callback = solver_callback
        self.max_attempts = int(max_attempts)
        self.post_solve_wait_ms = int(post_solve_wait_ms)


    # ----------------------------------------------------------- detect
    async def detect(self, page: Any) -> CaptchaDetection:
        """Scan the page for captcha presence and classify it.

        Returns a :class:`CaptchaDetection` with ``detected=False`` if
        no captcha is found — this is the fast path for normal pages.
        """
        started = time.time()

        # Phase 1: Check selectors
        for selector, ctype, confidence, desc in _DETECTION_RULES:
            if await _safe_is_visible(page, selector):
                iframe_src = ""
                if "iframe" in selector:
                    iframe_src = await _safe_get_attr(page, selector, "src")
                return CaptchaDetection(
                    detected=True,
                    captcha_type=ctype,
                    confidence=confidence,
                    selector=selector,
                    iframe_src=iframe_src,
                    challenge_text=desc,
                    duration_ms=_elapsed_ms(started),
                )

        # Phase 2: Check body text for captcha indicators
        body_text = await _safe_body_text(page)
        body_lower = body_text.lower()

        for indicator in _CAPTCHA_TEXT_INDICATORS:
            if indicator in body_lower:
                # Try to classify further
                ctype = self._classify_from_text(body_text)
                return CaptchaDetection(
                    detected=True,
                    captcha_type=ctype,
                    confidence=0.70,
                    challenge_text=indicator,
                    duration_ms=_elapsed_ms(started),
                )

        return CaptchaDetection(
            detected=False,
            duration_ms=_elapsed_ms(started),
        )


    # ----------------------------------------------------------- solve
    async def handle(
        self, page: Any, detection: CaptchaDetection | None = None,
    ) -> CaptchaResult:
        """Attempt to solve a detected captcha.

        If ``detection`` is None, runs :py:meth:`detect` first.
        """
        started = time.time()

        if detection is None:
            detection = await self.detect(page)

        if not detection.detected:
            return CaptchaResult(
                status=CaptchaStatus.SOLVED,
                captcha_type=CaptchaType.UNKNOWN,
                duration_ms=_elapsed_ms(started),
            )

        ctype = detection.captcha_type
        log.info(
            "captcha detected: type=%s confidence=%.2f selector=%s",
            ctype.value, detection.confidence, detection.selector,
        )

        # Route to appropriate solver
        for attempt in range(1, self.max_attempts + 1):
            result = await self._attempt_solve(page, detection, attempt)
            if result.solved:
                return result
            if result.needs_human:
                return result

        # All attempts exhausted
        return CaptchaResult(
            status=CaptchaStatus.HUMAN_REQUIRED,
            captcha_type=ctype,
            attempts=self.max_attempts,
            duration_ms=_elapsed_ms(started),
            error="max attempts exhausted",
        )

    async def _attempt_solve(
        self, page: Any, detection: CaptchaDetection, attempt: int,
    ) -> CaptchaResult:
        """Single solve attempt dispatched by captcha type."""
        started = time.time()
        ctype = detection.captcha_type

        try:
            if ctype is CaptchaType.CHECKBOX:
                return await self._solve_checkbox(page, detection, attempt)
            elif ctype is CaptchaType.ARITHMETIC:
                return await self._solve_arithmetic(page, detection, attempt)
            elif ctype in (
                CaptchaType.TURNSTILE, CaptchaType.RECAPTCHA,
                CaptchaType.HCAPTCHA,
            ):
                return await self._solve_provider(page, detection, attempt)
            elif ctype is CaptchaType.TEXT:
                return await self._solve_text(page, detection, attempt)
            elif ctype is CaptchaType.IMAGE:
                return await self._solve_image(page, detection, attempt)
            else:
                return CaptchaResult(
                    status=CaptchaStatus.HUMAN_REQUIRED,
                    captcha_type=ctype,
                    attempts=attempt,
                    duration_ms=_elapsed_ms(started),
                    error=f"no solver for type {ctype.value}",
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("captcha solve attempt %d failed: %s", attempt, exc)
            return CaptchaResult(
                status=CaptchaStatus.FAILED,
                captcha_type=ctype,
                attempts=attempt,
                duration_ms=_elapsed_ms(started),
                error=repr(exc),
            )


    # ----------------------------------------------------------- solvers
    async def _solve_checkbox(
        self, page: Any, detection: CaptchaDetection, attempt: int,
    ) -> CaptchaResult:
        """Click the "I'm not a robot" checkbox and verify."""
        started = time.time()
        # Try known checkbox selectors
        checkbox_selectors = [
            detection.selector,
            '[class*="captcha"] input[type="checkbox"]',
            '[id*="captcha"] input[type="checkbox"]',
            '.recaptcha-checkbox-border',
            '#recaptcha-anchor',
            '[class*="cf-turnstile"] input',
        ]
        clicked = False
        for sel in checkbox_selectors:
            if not sel:
                continue
            if await _safe_is_visible(page, sel):
                try:
                    await page.click(sel, timeout=5000)
                    clicked = True
                    break
                except Exception:  # noqa: BLE001
                    continue

        if not clicked:
            return CaptchaResult(
                status=CaptchaStatus.FAILED,
                captcha_type=CaptchaType.CHECKBOX,
                attempts=attempt,
                duration_ms=_elapsed_ms(started),
                error="could not find/click checkbox",
            )

        # Wait for resolution
        import asyncio
        await asyncio.sleep(self.post_solve_wait_ms / 1000)

        # Verify: check if captcha is still visible
        still_visible = await _safe_is_visible(page, detection.selector)
        if not still_visible or detection.selector == "":
            return CaptchaResult(
                status=CaptchaStatus.SOLVED,
                captcha_type=CaptchaType.CHECKBOX,
                attempts=attempt,
                duration_ms=_elapsed_ms(started),
            )

        return CaptchaResult(
            status=CaptchaStatus.FAILED,
            captcha_type=CaptchaType.CHECKBOX,
            attempts=attempt,
            duration_ms=_elapsed_ms(started),
            error="checkbox clicked but captcha still visible",
        )

    async def _solve_arithmetic(
        self, page: Any, detection: CaptchaDetection, attempt: int,
    ) -> CaptchaResult:
        """Parse and solve arithmetic captchas like 'What is 3 + 7?'."""
        started = time.time()
        body_text = await _safe_body_text(page)

        solution = self._evaluate_arithmetic(body_text)
        if solution is None:
            return CaptchaResult(
                status=CaptchaStatus.FAILED,
                captcha_type=CaptchaType.ARITHMETIC,
                attempts=attempt,
                duration_ms=_elapsed_ms(started),
                error="could not parse arithmetic expression",
            )

        # Find the input field near the captcha
        input_selectors = [
            '[class*="captcha"] input[type="text"]',
            '[id*="captcha"] input[type="text"]',
            'input[name*="captcha"]',
            'input[placeholder*="answer"]',
            'input[placeholder*="result"]',
        ]
        filled = False
        for sel in input_selectors:
            if await _safe_is_visible(page, sel):
                try:
                    await page.fill(sel, str(solution), timeout=3000)
                    filled = True
                    break
                except Exception:  # noqa: BLE001
                    continue

        if not filled:
            return CaptchaResult(
                status=CaptchaStatus.FAILED,
                captcha_type=CaptchaType.ARITHMETIC,
                solution=str(solution),
                attempts=attempt,
                duration_ms=_elapsed_ms(started),
                error="solved arithmetic but could not fill input",
            )

        return CaptchaResult(
            status=CaptchaStatus.SOLVED,
            captcha_type=CaptchaType.ARITHMETIC,
            solution=str(solution),
            attempts=attempt,
            duration_ms=_elapsed_ms(started),
        )


    async def _solve_provider(
        self, page: Any, detection: CaptchaDetection, attempt: int,
    ) -> CaptchaResult:
        """Handle Turnstile/reCAPTCHA/hCaptcha via external solver or human.

        If a ``solver_callback`` is configured, invokes it with the
        captcha metadata. Otherwise escalates to human.
        """
        started = time.time()
        ctype = detection.captcha_type

        if self.solver_callback is not None:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(self.solver_callback):
                    solution = await self.solver_callback(
                        ctype.value, detection.to_dict(),
                    )
                else:
                    solution = self.solver_callback(
                        ctype.value, detection.to_dict(),
                    )
                if solution:
                    # Inject the solution token into the response field
                    injected = await self._inject_token(page, ctype, str(solution))
                    if injected:
                        return CaptchaResult(
                            status=CaptchaStatus.SOLVED,
                            captcha_type=ctype,
                            solution=str(solution)[:20],
                            attempts=attempt,
                            duration_ms=_elapsed_ms(started),
                        )
            except Exception as exc:  # noqa: BLE001
                log.warning("solver callback failed: %s", exc)

        # No solver or solver failed -> human required
        return CaptchaResult(
            status=CaptchaStatus.HUMAN_REQUIRED,
            captcha_type=ctype,
            attempts=attempt,
            duration_ms=_elapsed_ms(started),
            error="external solver unavailable or failed",
        )

    async def _solve_text(
        self, page: Any, detection: CaptchaDetection, attempt: int,
    ) -> CaptchaResult:
        """Text captchas require OCR or external service."""
        started = time.time()
        if self.solver_callback is not None:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(self.solver_callback):
                    solution = await self.solver_callback("text", detection.to_dict())
                else:
                    solution = self.solver_callback("text", detection.to_dict())
                if solution:
                    # Fill into captcha input
                    for sel in [
                        '[class*="captcha"] input[type="text"]',
                        'input[name*="captcha"]',
                    ]:
                        if await _safe_is_visible(page, sel):
                            await page.fill(sel, str(solution), timeout=3000)
                            return CaptchaResult(
                                status=CaptchaStatus.SOLVED,
                                captcha_type=CaptchaType.TEXT,
                                solution=str(solution),
                                attempts=attempt,
                                duration_ms=_elapsed_ms(started),
                            )
            except Exception as exc:  # noqa: BLE001
                log.warning("text captcha solver failed: %s", exc)

        return CaptchaResult(
            status=CaptchaStatus.HUMAN_REQUIRED,
            captcha_type=CaptchaType.TEXT,
            attempts=attempt,
            duration_ms=_elapsed_ms(started),
            error="text captcha requires external solver or human",
        )

    async def _solve_image(
        self, page: Any, detection: CaptchaDetection, attempt: int,
    ) -> CaptchaResult:
        """Image captchas always require human or AI vision service."""
        return CaptchaResult(
            status=CaptchaStatus.HUMAN_REQUIRED,
            captcha_type=CaptchaType.IMAGE,
            attempts=attempt,
            duration_ms=0,
            error="image captcha requires human intervention",
        )


    # ----------------------------------------------------------- helpers
    async def _inject_token(
        self, page: Any, ctype: CaptchaType, token: str,
    ) -> bool:
        """Inject a solver token into the appropriate response field."""
        response_selectors: dict[CaptchaType, list[str]] = {
            CaptchaType.RECAPTCHA: [
                '#g-recaptcha-response',
                '[name="g-recaptcha-response"]',
                'textarea[id*="g-recaptcha-response"]',
            ],
            CaptchaType.HCAPTCHA: [
                '[name="h-captcha-response"]',
                'textarea[name="h-captcha-response"]',
            ],
            CaptchaType.TURNSTILE: [
                '#cf-turnstile-response',
                '[name="cf-turnstile-response"]',
                'input[name*="turnstile"]',
            ],
        }
        selectors = response_selectors.get(ctype, [])
        for sel in selectors:
            try:
                exists = await page.evaluate(
                    f"!!document.querySelector({_js_str(sel)})"
                )
                if exists:
                    await page.evaluate(
                        f"document.querySelector({_js_str(sel)}).value = {_js_str(token)}"
                    )
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _classify_from_text(self, body_text: str) -> CaptchaType:
        """Classify captcha type from page body text."""
        lower = body_text.lower()
        # Check arithmetic first
        for pattern in _ARITHMETIC_PATTERNS:
            if pattern.search(body_text):
                return CaptchaType.ARITHMETIC
        # Check for known provider keywords
        if "turnstile" in lower or "cloudflare" in lower:
            return CaptchaType.TURNSTILE
        if "recaptcha" in lower or "g-recaptcha" in lower:
            return CaptchaType.RECAPTCHA
        if "hcaptcha" in lower or "h-captcha" in lower:
            return CaptchaType.HCAPTCHA
        if "type the characters" in lower or "enter the code" in lower:
            return CaptchaType.TEXT
        if "select all" in lower or "click each" in lower:
            return CaptchaType.IMAGE
        if "not a robot" in lower or "checkbox" in lower:
            return CaptchaType.CHECKBOX
        return CaptchaType.UNKNOWN

    @staticmethod
    def _evaluate_arithmetic(text: str) -> int | None:
        """Extract and solve an arithmetic expression from text."""
        for pattern in _ARITHMETIC_PATTERNS:
            match = pattern.search(text)
            if match:
                a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
                op = op.lower()
                if op in ("+",):
                    return a + b
                elif op in ("-",):
                    return a - b
                elif op in ("*", "x", "×"):
                    return a * b
                elif op in ("/",):
                    return a // b if b != 0 else None
        return None



# ---------------------------------------------------------------- page helpers
async def _safe_is_visible(page: Any, selector: str) -> bool:
    """Check if a selector is visible, never raises."""
    if not selector:
        return False
    try:
        return await page.is_visible(selector, timeout=1000)
    except TypeError:
        try:
            return await page.is_visible(selector)
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


async def _safe_get_attr(page: Any, selector: str, attr: str) -> str:
    """Get an attribute from an element, never raises."""
    try:
        val = await page.evaluate(
            f"(document.querySelector({_js_str(selector)}) || {{}}).getAttribute('{attr}') || ''"
        )
        return str(val or "")
    except Exception:  # noqa: BLE001
        return ""


async def _safe_body_text(page: Any) -> str:
    """Get body text, never raises."""
    try:
        return await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 5000)"
        )
    except Exception:  # noqa: BLE001
        return ""


def _js_str(s: str) -> str:
    """Python string → safe JS string literal."""
    escaped = (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f"'{escaped}'"


def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
