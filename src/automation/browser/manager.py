"""Browser manager with isolated per-account profiles and auto-recovery.

Each account gets its own persistent ``BrowserContext`` rooted at
``data/profiles/<account_id>``. That guarantees independent cookies, local
storage, cache, and IndexedDB across accounts.

If Playwright is not installed the manager imports lazily and only fails when
a browser session is actually requested — the rest of the framework keeps
working (e.g. for tests, CLI, or non-browser plugins).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import (
        Browser, BrowserContext, Page, Playwright,
    )

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BrowserConfig:
    """Static configuration for the browser manager.

    Sourced from the ``playwright`` section of the framework config.
    """

    headless: bool = True
    timeout_ms: int = 30_000
    user_agent: str | None = None
    viewport: tuple[int, int] = (1280, 800)
    proxy: dict[str, str] | None = None
    profiles_root: Path = field(default_factory=lambda: Path("data/profiles"))
    browser: str = "chromium"  # chromium | firefox | webkit
    slow_mo_ms: int = 0
    downloads_dir: Path = field(default_factory=lambda: Path("data/downloads"))
    args: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrowserConfig":
        pw = (data or {}).get("playwright", {}) or {}
        viewport = pw.get("viewport") or {"width": 1280, "height": 800}
        return cls(
            headless=bool(pw.get("headless", True)),
            timeout_ms=int(pw.get("timeout", 30_000)),
            user_agent=pw.get("user_agent"),
            viewport=(int(viewport.get("width", 1280)), int(viewport.get("height", 800))),
            proxy=pw.get("proxy"),
            profiles_root=Path(pw.get("profiles_root", "data/profiles")),
            browser=pw.get("browser", "chromium"),
            slow_mo_ms=int(pw.get("slow_mo_ms", 0)),
            downloads_dir=Path(pw.get("downloads_dir", "data/downloads")),
            args=list(pw.get("args", []) or []),
        )


@dataclass(slots=True)
class BrowserSession:
    """A live, isolated browser session bound to one account."""

    account_id: str
    context: "BrowserContext"
    page: "Page"
    profile_dir: Path
    started_at: float
    healthy: bool = True


class BrowserManager:
    """Pool of isolated Playwright sessions with auto-recovery."""

    def __init__(self, cfg: BrowserConfig | None = None) -> None:
        self.cfg = cfg or BrowserConfig()
        self.cfg.profiles_root.mkdir(parents=True, exist_ok=True)
        self.cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._playwright: "Playwright" | None = None
        self._browser: "Browser" | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._playwright is not None:
            return
        try:
            from playwright.async_api import async_playwright  # local import
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Playwright is not installed. Install with: pip install playwright "
                "&& playwright install"
            ) from exc
        self._playwright = await async_playwright().start()
        log.info("Playwright started (browser=%s headless=%s)",
                 self.cfg.browser, self.cfg.headless)

    async def stop(self) -> None:
        async with self._lock:
            for session in list(self._sessions.values()):
                await self._close_session_locked(session.account_id)
        if self._browser:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                log.exception("error closing shared browser")
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                log.exception("error stopping playwright")
            self._playwright = None

    # ------------------------------------------------------------------ access
    @property
    def sessions(self) -> dict[str, BrowserSession]:
        return dict(self._sessions)

    async def get_or_create(self, account_id: str) -> BrowserSession:
        async with self._lock:
            existing = self._sessions.get(account_id)
            if existing and existing.healthy:
                return existing
            if existing:
                await self._close_session_locked(account_id)
            return await self._create_session_locked(account_id)

    async def close(self, account_id: str) -> bool:
        async with self._lock:
            return await self._close_session_locked(account_id)

    async def reset_profile(self, account_id: str) -> None:
        """Delete the persistent profile directory for an account."""
        await self.close(account_id)
        profile = self.cfg.profiles_root / _safe(account_id)
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
            log.info("reset profile for account=%s", account_id)

    # ------------------------------------------------------------------ health
    async def healthcheck(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for sid, session in list(self._sessions.items()):
            ok = True
            try:
                _ = session.page.url
                if session.page.is_closed():
                    ok = False
            except Exception:  # noqa: BLE001
                ok = False
            session.healthy = ok
            out[sid] = ok
        return out

    async def recover_unhealthy(self) -> int:
        """Recreate any session marked unhealthy. Returns count recovered."""
        recovered = 0
        for sid, session in list(self._sessions.items()):
            if session.healthy:
                continue
            log.warning("recovering unhealthy session account=%s", sid)
            try:
                await self.close(sid)
                await self.get_or_create(sid)
                recovered += 1
            except Exception:  # noqa: BLE001
                log.exception("failed recovering session %s", sid)
        return recovered

    # ----------------------------------------------------------------- private
    async def _create_session_locked(self, account_id: str) -> BrowserSession:
        await self.start()
        assert self._playwright is not None
        profile = self.cfg.profiles_root / _safe(account_id)
        profile.mkdir(parents=True, exist_ok=True)
        launch_kwargs: dict[str, Any] = {
            "headless": self.cfg.headless,
            "slow_mo": self.cfg.slow_mo_ms,
            "args": self.cfg.args,
            "viewport": {"width": self.cfg.viewport[0], "height": self.cfg.viewport[1]},
            "user_agent": self.cfg.user_agent,
            "downloads_path": str(self.cfg.downloads_dir),
        }
        if self.cfg.proxy:
            launch_kwargs["proxy"] = self.cfg.proxy
        # remove None to satisfy Playwright API
        launch_kwargs = {k: v for k, v in launch_kwargs.items() if v is not None}

        browser_type = getattr(self._playwright, self.cfg.browser)
        try:
            context = await browser_type.launch_persistent_context(
                str(profile), **launch_kwargs
            )
        except Exception:  # noqa: BLE001
            log.exception("launch_persistent_context failed; falling back to ephemeral")
            if not self._browser:
                self._browser = await browser_type.launch(headless=self.cfg.headless)
            context = await self._browser.new_context(
                viewport={"width": self.cfg.viewport[0], "height": self.cfg.viewport[1]},
                user_agent=self.cfg.user_agent,
            )
        context.set_default_timeout(self.cfg.timeout_ms)
        page = context.pages[0] if context.pages else await context.new_page()

        import time
        session = BrowserSession(
            account_id=account_id,
            context=context,
            page=page,
            profile_dir=profile,
            started_at=time.time(),
            healthy=True,
        )
        self._sessions[account_id] = session
        log.info("created browser session account=%s profile=%s", account_id, profile)
        return session

    async def _close_session_locked(self, account_id: str) -> bool:
        session = self._sessions.pop(account_id, None)
        if not session:
            return False
        try:
            await session.context.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing context for %s", account_id)
        return True


def _safe(name: str) -> str:
    """Make a string safe to use as a directory name."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:128]
