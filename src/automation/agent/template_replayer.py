"""TemplateReplayer: deterministic execution of an ExecutionTemplate.

The replayer takes the JSON template produced by :class:`TemplateRecorder`
and walks it action-by-action against a Playwright ``Page``. **No LLM is
involved.** No fixed ``sleep()`` is involved either — every wait is
delegated to :class:`automation.agent.waiter.AdaptiveWaiter`, which
monitors actual browser state (DOM mutations / network idle / spinners /
URL changes / success messages) instead of arbitrary timers.

The contract:

  * **Success** = every action executes AND every embedded
    :class:`ActionKind.VERIFY` step's hints match the page content.
  * **Failure** = any action throws, any wait condition does not resolve
    within its per-step ``timeout_ms``, or a verify step finds no hint.

On failure the replayer returns a :class:`ReplayResult` with the index
of the offending action plus a human-readable reason. The
:class:`AdaptiveExecutor` uses that information to decide whether to
re-engage the AI brain.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from automation.agent.execution_template import (
    ActionKind,
    ExecutionTemplate,
    TemplateAction,
    interpolate,
)
from automation.agent.waiter import AdaptiveWaiter, WaitCondition, WaitResult

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
#  result types
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class ReplayActionResult:
    """Outcome of one action inside a replay."""
    index: int
    kind: str
    success: bool
    duration_ms: int = 0
    error: str = ""
    wait_resolved: bool | None = None
    selector: str | None = None
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "kind": self.kind,
            "success": self.success, "duration_ms": self.duration_ms,
            "error": self.error, "wait_resolved": self.wait_resolved,
            "selector": self.selector, "url": self.url,
        }


@dataclass(slots=True)
class ReplayResult:
    """Outcome of one full replay attempt."""
    success: bool
    template_domain: str
    template_workflow: str
    template_version: int
    duration_ms: int
    actions: list[ReplayActionResult] = field(default_factory=list)
    failed_index: int | None = None
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "template": {
                "domain": self.template_domain,
                "workflow": self.template_workflow,
                "version": self.template_version,
            },
            "duration_ms": self.duration_ms,
            "actions": [a.to_dict() for a in self.actions],
            "failed_index": self.failed_index,
            "failure_reason": self.failure_reason,
        }


# -----------------------------------------------------------------------------
#  replayer
# -----------------------------------------------------------------------------
class TemplateReplayer:
    """Run a stored :class:`ExecutionTemplate` against a live Playwright page."""

    def __init__(
        self,
        *,
        waiter: AdaptiveWaiter | None = None,
        verifier: Any | None = None,
    ) -> None:
        self.waiter = waiter or AdaptiveWaiter()
        # Optional - if provided we delegate VERIFY actions to it. The
        # replayer falls back to a simple text-hint scan otherwise.
        self.verifier = verifier

    async def replay(
        self,
        page: Any,
        template: ExecutionTemplate,
        inputs: dict[str, Any],
        *,
        recorder: Any | None = None,
    ) -> ReplayResult:
        """Execute every action in the template. See module docstring."""
        started = time.time()
        results: list[ReplayActionResult] = []
        failed_index: int | None = None
        failure_reason = ""

        for idx, action in enumerate(template.actions):
            action_started = time.time()
            wait_resolved: bool | None = None
            try:
                wait_resolved = await self._execute_action(
                    page, action, inputs, recorder=recorder,
                )
            except Exception as exc:  # noqa: BLE001
                duration = int((time.time() - action_started) * 1000)
                err = repr(exc)[:300]
                results.append(ReplayActionResult(
                    index=idx, kind=action.kind.value, success=False,
                    duration_ms=duration, error=err,
                    selector=action.selector, url=_safe_page_url(page),
                ))
                failed_index = idx
                failure_reason = (
                    f"action {idx} ({action.kind.value}) raised: {err}"
                )
                break

            if wait_resolved is False:
                duration = int((time.time() - action_started) * 1000)
                results.append(ReplayActionResult(
                    index=idx, kind=action.kind.value, success=False,
                    duration_ms=duration,
                    error=f"wait condition {action.wait_for!r} did not resolve",
                    wait_resolved=False,
                    selector=action.selector, url=_safe_page_url(page),
                ))
                failed_index = idx
                failure_reason = (
                    f"action {idx} ({action.kind.value}) wait "
                    f"{action.wait_for!r} timed out"
                )
                break

            duration = int((time.time() - action_started) * 1000)
            results.append(ReplayActionResult(
                index=idx, kind=action.kind.value, success=True,
                duration_ms=duration, wait_resolved=wait_resolved,
                selector=action.selector, url=_safe_page_url(page),
            ))

        total = int((time.time() - started) * 1000)
        return ReplayResult(
            success=failed_index is None,
            template_domain=template.domain,
            template_workflow=template.workflow,
            template_version=template.version,
            duration_ms=total,
            actions=results,
            failed_index=failed_index,
            failure_reason=failure_reason,
        )

    # -------------------------------------------------------- per-action
    async def _execute_action(
        self,
        page: Any,
        action: TemplateAction,
        inputs: dict[str, Any],
        *,
        recorder: Any | None,
    ) -> bool | None:
        """Execute one action. Returns ``wait_resolved`` (True/False/None).

        Raises whatever Playwright raises on failed clicks/fills so the
        outer loop can record the error verbatim. Verify failures are
        reported as ``wait_resolved=False`` so the same outer loop
        treats them like a wait timeout.
        """
        kind = action.kind

        if kind == ActionKind.NAVIGATE:
            url = interpolate(action.url_template, inputs)
            if not url:
                raise ValueError("navigate action has empty url_template")
            await page.goto(url, timeout=action.timeout_ms)
            if recorder is not None:
                recorder.record_navigate(url, duration_ms=0)
            return await self._wait(page, action)

        if kind == ActionKind.FILL:
            value = interpolate(action.value_template or "", inputs)
            await page.fill(
                action.selector or "", value, timeout=action.timeout_ms,
            )
            if recorder is not None:
                recorder.record_fill(
                    action.selector or "", value, url=_safe_page_url(page),
                )
            return await self._wait(page, action)

        if kind == ActionKind.CLICK:
            from_url = _safe_page_url(page)
            await page.click(action.selector or "", timeout=action.timeout_ms)
            if recorder is not None:
                recorder.record_click(
                    action.selector or "", url=from_url,
                )
            return await self._wait(page, action, before_url=from_url)

        if kind == ActionKind.SELECT:
            value = interpolate(action.value_template or "", inputs)
            await page.select_option(
                action.selector or "", value, timeout=action.timeout_ms,
            )
            return await self._wait(page, action)

        if kind == ActionKind.CHECK:
            await page.check(action.selector or "", timeout=action.timeout_ms)
            return await self._wait(page, action)

        if kind == ActionKind.PRESS:
            value = interpolate(action.value_template or "", inputs)
            await page.press(
                action.selector or "", value, timeout=action.timeout_ms,
            )
            return await self._wait(page, action)

        if kind == ActionKind.WAIT:
            return await self._wait(page, action, force=True)

        if kind == ActionKind.VERIFY:
            return await self._verify(page, action.hints)

        raise ValueError(f"unknown action kind: {kind}")

    # ----------------------------------------------------------- subroutines
    async def _wait(
        self,
        page: Any,
        action: TemplateAction,
        *,
        force: bool = False,
        before_url: str | None = None,
    ) -> bool | None:
        """Apply this action's intelligent wait, if any.

        Returns ``None`` when the action specifies no wait and ``force``
        is False — meaning "no wait was requested". Returns ``True`` /
        ``False`` when a wait was attempted.
        """
        cond = action.wait_for
        if not cond:
            if not force:
                return None
            cond = "page_load"

        timeout_ms = action.timeout_ms

        try:
            condition = WaitCondition(cond)
        except ValueError:
            condition = WaitCondition.PAGE_LOAD

        result = await self.waiter.smart_wait(
            page,
            condition=condition,
            timeout_ms=timeout_ms,
            hints=action.hints or None,
            selector=action.selector,
            from_url=before_url,
        )
        if isinstance(result, WaitResult):
            return bool(result.resolved)
        return None  # pragma: no cover  - defensive

    async def _verify(self, page: Any, hints: list[str]) -> bool:
        """VERIFY action: text-hint match + (optional) verifier integration."""
        if self.verifier is not None:
            try:
                v = await self.verifier.verify(page, hints=hints)
                return bool(getattr(v, "passed", False))
            except Exception:  # noqa: BLE001
                # fall through to the lightweight scan
                pass

        if not hints:
            # No hints provided; accept any page that's reachable.
            return True
        try:
            text = await page.evaluate(
                "() => (document.body.innerText || '').toLowerCase().slice(0, 8000)",
            )
        except Exception:  # noqa: BLE001
            return False
        text = (text or "").lower()
        return any(h.lower() in text for h in hints if h)


# -----------------------------------------------------------------------------
#  helpers
# -----------------------------------------------------------------------------
def _safe_page_url(page: Any) -> str:
    try:
        return getattr(page, "url", "") or ""
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "ReplayActionResult",
    "ReplayResult",
    "TemplateReplayer",
]
