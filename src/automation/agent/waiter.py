"""Adaptive waiting system.

Replaces fixed sleeps with intelligent state monitoring. The waiter
continuously probes browser state until the required condition is met
or a timeout is reached.

Monitors:
  - DOM stability (mutation count settles)
  - Network idle (no pending requests)
  - URL changes
  - Loading indicators / spinners disappearing
  - Progress bars completing
  - Download completion
  - Success / error messages appearing
  - New windows / tabs opening
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class WaitCondition(str, Enum):
    """What the waiter is monitoring for."""
    PAGE_LOAD = "page_load"
    DOM_STABLE = "dom_stable"
    NETWORK_IDLE = "network_idle"
    URL_CHANGE = "url_change"
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_GONE = "element_gone"
    DOWNLOAD_COMPLETE = "download_complete"
    SUCCESS_MESSAGE = "success_message"
    NO_LOADING = "no_loading"
    NAVIGATION_COMPLETE = "navigation_complete"
    CUSTOM = "custom"


@dataclass(slots=True)
class WaitResult:
    """Outcome of an adaptive wait."""
    condition: WaitCondition
    resolved: bool
    elapsed_ms: int
    reason: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition.value,
            "resolved": self.resolved,
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
            "data": self.data,
        }


# JS injected to monitor DOM mutations + network activity
_INSTALL_OBSERVER_JS = """
() => {
    if (window.__agent_observer) return;
    window.__agent_mutation_count = 0;
    window.__agent_pending_requests = 0;
    window.__agent_last_mutation = Date.now();
    window.__agent_url = location.href;

    // DOM mutation observer
    const observer = new MutationObserver((mutations) => {
        window.__agent_mutation_count += mutations.length;
        window.__agent_last_mutation = Date.now();
    });
    observer.observe(document.body || document.documentElement, {
        childList: true, subtree: true, attributes: true
    });

    // Network activity tracking via fetch/XHR interception
    const origFetch = window.fetch;
    window.fetch = function(...args) {
        window.__agent_pending_requests++;
        return origFetch.apply(this, args).finally(() => {
            window.__agent_pending_requests = Math.max(0, window.__agent_pending_requests - 1);
        });
    };

    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(...args) {
        this.__agent_tracked = true;
        return origOpen.apply(this, args);
    };
    XMLHttpRequest.prototype.send = function(...args) {
        if (this.__agent_tracked) {
            window.__agent_pending_requests++;
            this.addEventListener('loadend', () => {
                window.__agent_pending_requests = Math.max(0, window.__agent_pending_requests - 1);
            }, {once: true});
        }
        return origSend.apply(this, args);
    };

    window.__agent_observer = true;
}
"""

_GET_STATE_JS = """
() => {
    const loaders = document.querySelectorAll(
        '[class*="spinner"], [class*="loading"], [class*="loader"], ' +
        '[role="progressbar"], .progress, [class*="skeleton"]'
    );
    const visibleLoaders = Array.from(loaders).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });

    const successSels = [
        '[class*="success"]', '[class*="alert-success"]',
        '[class*="toast"]', '[role="alert"]'
    ];
    const successEls = [];
    for (const sel of successSels) {
        for (const el of document.querySelectorAll(sel)) {
            const text = (el.innerText || '').toLowerCase();
            if (text.includes('success') || text.includes('complete') ||
                text.includes('created') || text.includes('welcome') ||
                text.includes('verified') || text.includes('done')) {
                successEls.push(text.slice(0, 100));
            }
        }
    }

    const errorEls = [];
    for (const sel of ['[class*="error"]', '[class*="alert-danger"]', '[class*="alert-error"]']) {
        for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
                errorEls.push((el.innerText || '').slice(0, 100));
            }
        }
    }

    return {
        url: location.href,
        title: document.title,
        mutation_count: window.__agent_mutation_count || 0,
        last_mutation_ms_ago: Date.now() - (window.__agent_last_mutation || Date.now()),
        pending_requests: window.__agent_pending_requests || 0,
        loading_visible: visibleLoaders.length,
        success_messages: successEls.slice(0, 5),
        error_messages: errorEls.slice(0, 5),
        ready_state: document.readyState,
    };
}
"""


class AdaptiveWaiter:
    """Intelligent waiting that monitors real browser state.

    Instead of sleeping for fixed durations, the waiter continuously polls
    browser conditions and resolves as soon as the target state is reached.
    """

    def __init__(
        self,
        *,
        poll_interval_ms: int = 200,
        dom_settle_ms: int = 500,
        network_idle_ms: int = 300,
        default_timeout_ms: int = 30_000,
        max_timeout_ms: int = 120_000,
    ) -> None:
        self.poll_interval_ms = poll_interval_ms
        self.dom_settle_ms = dom_settle_ms
        self.network_idle_ms = network_idle_ms
        self.default_timeout_ms = default_timeout_ms
        self.max_timeout_ms = max_timeout_ms

    async def install_observer(self, page: Any) -> None:
        """Install the DOM/network observer into the page. Safe to call repeatedly."""
        try:
            await page.evaluate(_INSTALL_OBSERVER_JS)
        except Exception:  # noqa: BLE001
            log.debug("waiter: could not install observer (page may be navigating)")

    async def get_page_state(self, page: Any) -> dict[str, Any]:
        """Get current browser state snapshot."""
        try:
            return await page.evaluate(_GET_STATE_JS)
        except Exception:  # noqa: BLE001
            return {
                "url": "",
                "mutation_count": 0,
                "last_mutation_ms_ago": 0,
                "pending_requests": 0,
                "loading_visible": 0,
                "success_messages": [],
                "error_messages": [],
                "ready_state": "unknown",
            }

    async def wait_for_page_ready(
        self, page: Any, *, timeout_ms: int | None = None
    ) -> WaitResult:
        """Wait until page is fully loaded and stable.

        Combines: DOM settled + network idle + no visible loaders.
        """
        timeout = timeout_ms or self.default_timeout_ms
        started = time.time()
        await self.install_observer(page)

        stable_since: float | None = None
        while True:
            elapsed = int((time.time() - started) * 1000)
            if elapsed >= timeout:
                return WaitResult(
                    condition=WaitCondition.PAGE_LOAD,
                    resolved=False,
                    elapsed_ms=elapsed,
                    reason="timeout waiting for page ready",
                )

            state = await self.get_page_state(page)

            dom_settled = state["last_mutation_ms_ago"] >= self.dom_settle_ms
            network_idle = state["pending_requests"] == 0
            no_loaders = state["loading_visible"] == 0
            doc_ready = state["ready_state"] in ("complete", "interactive")

            all_stable = dom_settled and network_idle and no_loaders and doc_ready

            if all_stable:
                if stable_since is None:
                    stable_since = time.time()
                elif (time.time() - stable_since) * 1000 >= self.network_idle_ms:
                    return WaitResult(
                        condition=WaitCondition.PAGE_LOAD,
                        resolved=True,
                        elapsed_ms=int((time.time() - started) * 1000),
                        reason="page ready: DOM settled, network idle, no loaders",
                        data=state,
                    )
            else:
                stable_since = None

            await asyncio.sleep(self.poll_interval_ms / 1000)

    async def wait_for_url_change(
        self, page: Any, from_url: str, *, timeout_ms: int | None = None
    ) -> WaitResult:
        """Wait until the page URL changes from ``from_url``."""
        timeout = timeout_ms or self.default_timeout_ms
        started = time.time()

        while True:
            elapsed = int((time.time() - started) * 1000)
            if elapsed >= timeout:
                return WaitResult(
                    condition=WaitCondition.URL_CHANGE,
                    resolved=False,
                    elapsed_ms=elapsed,
                    reason=f"timeout waiting for URL change from {from_url}",
                )
            try:
                current = page.url
            except Exception:  # noqa: BLE001
                current = ""
            if current and current != from_url:
                return WaitResult(
                    condition=WaitCondition.URL_CHANGE,
                    resolved=True,
                    elapsed_ms=int((time.time() - started) * 1000),
                    reason=f"URL changed to {current}",
                    data={"from": from_url, "to": current},
                )
            await asyncio.sleep(self.poll_interval_ms / 1000)

    async def wait_for_navigation(
        self, page: Any, *, timeout_ms: int | None = None
    ) -> WaitResult:
        """Wait for a navigation + page settle (URL change + page ready)."""
        timeout = timeout_ms or self.default_timeout_ms
        started = time.time()
        try:
            current_url = page.url
        except Exception:  # noqa: BLE001
            current_url = ""

        # Wait for URL change first
        url_result = await self.wait_for_url_change(
            page, current_url, timeout_ms=timeout
        )
        if not url_result.resolved:
            # URL didn't change, but page might still have reloaded
            pass

        # Now wait for page to settle
        remaining = max(1000, timeout - int((time.time() - started) * 1000))
        ready_result = await self.wait_for_page_ready(page, timeout_ms=remaining)
        elapsed = int((time.time() - started) * 1000)

        return WaitResult(
            condition=WaitCondition.NAVIGATION_COMPLETE,
            resolved=ready_result.resolved,
            elapsed_ms=elapsed,
            reason=f"nav: url_changed={url_result.resolved} page_ready={ready_result.resolved}",
            data={
                "url_change": url_result.to_dict(),
                "page_ready": ready_result.to_dict(),
            },
        )

    async def wait_for_element(
        self,
        page: Any,
        selector: str,
        *,
        visible: bool = True,
        timeout_ms: int | None = None,
    ) -> WaitResult:
        """Wait for an element to appear (and optionally be visible)."""
        timeout = timeout_ms or self.default_timeout_ms
        started = time.time()
        try:
            state = "visible" if visible else "attached"
            await page.wait_for_selector(selector, state=state, timeout=timeout)
            return WaitResult(
                condition=WaitCondition.ELEMENT_VISIBLE,
                resolved=True,
                elapsed_ms=int((time.time() - started) * 1000),
                reason=f"element found: {selector}",
            )
        except Exception as exc:  # noqa: BLE001
            return WaitResult(
                condition=WaitCondition.ELEMENT_VISIBLE,
                resolved=False,
                elapsed_ms=int((time.time() - started) * 1000),
                reason=f"timeout waiting for {selector}: {exc!r}",
            )

    async def wait_for_download(
        self, page: Any, *, timeout_ms: int | None = None
    ) -> WaitResult:
        """Wait for a download to start and complete."""
        timeout = timeout_ms or 60_000  # downloads can be slow
        started = time.time()
        try:
            async with page.expect_download(timeout=timeout) as download_info:
                download = await download_info.value
                path = await download.path()
                return WaitResult(
                    condition=WaitCondition.DOWNLOAD_COMPLETE,
                    resolved=True,
                    elapsed_ms=int((time.time() - started) * 1000),
                    reason=f"download complete: {download.suggested_filename}",
                    data={"filename": download.suggested_filename, "path": str(path)},
                )
        except Exception as exc:  # noqa: BLE001
            return WaitResult(
                condition=WaitCondition.DOWNLOAD_COMPLETE,
                resolved=False,
                elapsed_ms=int((time.time() - started) * 1000),
                reason=f"download wait failed: {exc!r}",
            )

    async def wait_for_success_signal(
        self, page: Any, *, hints: list[str] | None = None, timeout_ms: int | None = None
    ) -> WaitResult:
        """Wait for a success signal: URL change, success message, or page stable.

        Uses provided hints to look for specific text on the page.
        """
        timeout = timeout_ms or self.default_timeout_ms
        started = time.time()
        hints = hints or []
        await self.install_observer(page)

        while True:
            elapsed = int((time.time() - started) * 1000)
            if elapsed >= timeout:
                return WaitResult(
                    condition=WaitCondition.SUCCESS_MESSAGE,
                    resolved=False,
                    elapsed_ms=elapsed,
                    reason="timeout waiting for success signal",
                )

            state = await self.get_page_state(page)

            # Check for success messages
            if state.get("success_messages"):
                return WaitResult(
                    condition=WaitCondition.SUCCESS_MESSAGE,
                    resolved=True,
                    elapsed_ms=elapsed,
                    reason=f"success message detected: {state['success_messages'][0]}",
                    data=state,
                )

            # Check hints against page content
            if hints:
                try:
                    body_text = await page.evaluate(
                        "() => (document.body.innerText || '').toLowerCase().slice(0, 5000)"
                    )
                    for hint in hints:
                        if hint.lower() in body_text:
                            return WaitResult(
                                condition=WaitCondition.SUCCESS_MESSAGE,
                                resolved=True,
                                elapsed_ms=elapsed,
                                reason=f"hint matched: {hint}",
                                data={"matched_hint": hint},
                            )
                except Exception:  # noqa: BLE001
                    pass

            # If page is stable and no errors, consider it success after settle time
            dom_settled = state["last_mutation_ms_ago"] >= self.dom_settle_ms * 2
            network_idle = state["pending_requests"] == 0
            no_errors = not state.get("error_messages")
            if dom_settled and network_idle and no_errors and elapsed > 2000:
                return WaitResult(
                    condition=WaitCondition.SUCCESS_MESSAGE,
                    resolved=True,
                    elapsed_ms=elapsed,
                    reason="page settled with no errors (implicit success)",
                    data=state,
                )

            await asyncio.sleep(self.poll_interval_ms / 1000)

    async def smart_wait(
        self,
        page: Any,
        *,
        condition: WaitCondition = WaitCondition.PAGE_LOAD,
        timeout_ms: int | None = None,
        hints: list[str] | None = None,
        selector: str | None = None,
        from_url: str | None = None,
    ) -> WaitResult:
        """Dispatch to the appropriate wait method based on condition."""
        if condition == WaitCondition.PAGE_LOAD:
            return await self.wait_for_page_ready(page, timeout_ms=timeout_ms)
        if condition == WaitCondition.URL_CHANGE:
            return await self.wait_for_url_change(
                page, from_url or "", timeout_ms=timeout_ms
            )
        if condition == WaitCondition.NAVIGATION_COMPLETE:
            return await self.wait_for_navigation(page, timeout_ms=timeout_ms)
        if condition == WaitCondition.ELEMENT_VISIBLE:
            return await self.wait_for_element(
                page, selector or "body", timeout_ms=timeout_ms
            )
        if condition == WaitCondition.DOWNLOAD_COMPLETE:
            return await self.wait_for_download(page, timeout_ms=timeout_ms)
        if condition == WaitCondition.SUCCESS_MESSAGE:
            return await self.wait_for_success_signal(
                page, hints=hints, timeout_ms=timeout_ms
            )
        # Default: page ready
        return await self.wait_for_page_ready(page, timeout_ms=timeout_ms)
