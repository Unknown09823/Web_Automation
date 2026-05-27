"""Browser manager with isolated per-account profiles and auto-recovery.

Each account gets its own persistent ``BrowserContext`` rooted at
``data/profiles/<profile_id>``. By default ``profile_id == account_id`` so
every account has its own cookies, local storage, cache, and IndexedDB —
that's the isolation guarantee the rest of the framework relies on.

Per-session overrides
---------------------
The framework also supports *legitimate* identity testing: deliberately
sharing a browser profile or a network identity across accounts so a target
site can detect repeat behavior. Callers pass a :class:`BrowserOverrides`
instance to :py:meth:`get_or_create` and the manager:

* uses ``overrides.profile_id`` as the profile directory key (multiple
  accounts can share one device by using the same value),
* applies the proxy / user-agent / viewport / locale / timezone /
  geolocation / extra headers / permissions only for that session.

Two accounts that share a ``profile_id`` cannot run a browser session at
the same time — Chromium locks the profile directory. The manager closes
any existing session that holds the directory before creating a new one,
so sequential runs work without conflict. Run the test workflow with
``parallel: false`` (the default) when sharing profiles.

If Playwright is not installed the manager imports lazily and only fails
when a browser session is actually requested — the rest of the framework
keeps working (e.g. for tests, CLI, or non-browser plugins).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

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
    locale: str | None = None
    timezone_id: str | None = None

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
            locale=pw.get("locale"),
            timezone_id=pw.get("timezone_id"),
        )


@dataclass(slots=True)
class BrowserOverrides:
    """Per-session overrides, typically sourced from ``account.metadata``.

    All fields are optional. Unset values fall back to the framework-wide
    :class:`BrowserConfig`. The manager only applies what's set, so callers
    can mix-and-match: e.g. share a proxy across accounts while letting each
    keep its own profile, or vice versa.

    ``proxy`` accepts either a URL string (``http://user:pass@host:port`` or
    ``socks5://...``) or Playwright's full proxy dict
    (``{"server": ..., "username": ..., "password": ...}``). Strings are
    parsed with :func:`urllib.parse.urlsplit` so embedded credentials work.

    ``profile_id`` overrides the directory used for the persistent profile.
    Two accounts that share a ``profile_id`` are treated as the same device.
    The manager guarantees only one session at a time per profile directory
    by closing the existing holder before opening a new one.
    """

    proxy: str | dict[str, str] | None = None
    user_agent: str | None = None
    viewport: tuple[int, int] | None = None
    locale: str | None = None
    timezone_id: str | None = None
    extra_http_headers: dict[str, str] | None = None
    geolocation: dict[str, float] | None = None
    permissions: list[str] | None = None
    color_scheme: str | None = None
    device_scale_factor: float | None = None
    profile_id: str | None = None

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> "BrowserOverrides":
        """Build from an account's ``metadata`` dict.

        Recognized keys (all optional): ``proxy``, ``user_agent``, ``viewport``,
        ``locale``, ``timezone_id``, ``extra_http_headers``, ``geolocation``,
        ``permissions``, ``color_scheme``, ``device_scale_factor``, ``profile_id``.

        Unknown keys are ignored so plugins / workflows can store other data
        in the same metadata dict without confusing the browser layer.
        """
        m = metadata or {}
        return cls(
            proxy=_normalize_proxy(m.get("proxy")),
            user_agent=_strip_or_none(m.get("user_agent")),
            viewport=_normalize_viewport(m.get("viewport")),
            locale=_strip_or_none(m.get("locale")),
            timezone_id=_strip_or_none(m.get("timezone_id")),
            extra_http_headers=(dict(m["extra_http_headers"])
                                if isinstance(m.get("extra_http_headers"), dict) else None),
            geolocation=_normalize_geolocation(m.get("geolocation")),
            permissions=(list(m["permissions"])
                         if isinstance(m.get("permissions"), list) else None),
            color_scheme=_strip_or_none(m.get("color_scheme")),
            device_scale_factor=(float(m["device_scale_factor"])
                                 if m.get("device_scale_factor") is not None else None),
            profile_id=_strip_or_none(m.get("profile_id")),
        )

    def merge_into(self, base: dict[str, Any]) -> dict[str, Any]:
        """Apply overrides onto a Playwright launch/context kwargs dict.

        Only sets keys that are non-None on this overrides instance. Returns
        ``base`` for chaining; mutates in place.
        """
        if self.proxy is not None:
            base["proxy"] = self.proxy
        if self.user_agent is not None:
            base["user_agent"] = self.user_agent
        if self.viewport is not None:
            base["viewport"] = {"width": self.viewport[0], "height": self.viewport[1]}
        if self.locale is not None:
            base["locale"] = self.locale
        if self.timezone_id is not None:
            base["timezone_id"] = self.timezone_id
        if self.extra_http_headers is not None:
            base["extra_http_headers"] = self.extra_http_headers
        if self.geolocation is not None:
            base["geolocation"] = self.geolocation
        if self.permissions is not None:
            base["permissions"] = self.permissions
        if self.color_scheme is not None:
            base["color_scheme"] = self.color_scheme
        if self.device_scale_factor is not None:
            base["device_scale_factor"] = self.device_scale_factor
        return base


@dataclass(slots=True)
class BrowserSession:
    """A live, isolated browser session bound to one account.

    ``profile_dir`` reflects the actual directory used (after ``profile_id``
    override resolution). Multiple accounts may have sessions that resolve
    to the same ``profile_dir`` over time, but never simultaneously.
    """

    account_id: str
    context: "BrowserContext"
    page: "Page"
    profile_dir: Path
    started_at: float
    healthy: bool = True
    profile_id: str = ""
    proxy: str | None = None


class BrowserManager:
    """Pool of isolated Playwright sessions with auto-recovery."""

    def __init__(self, cfg: BrowserConfig | None = None) -> None:
        self.cfg = cfg or BrowserConfig()
        self.cfg.profiles_root.mkdir(parents=True, exist_ok=True)
        self.cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._playwright: "Playwright" | None = None
        self._browser: "Browser" | None = None
        self._sessions: dict[str, BrowserSession] = {}
        # which account currently holds a given profile dir
        self._profile_owner: dict[Path, str] = {}
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

    async def get_or_create(
        self,
        account_id: str,
        overrides: BrowserOverrides | None = None,
    ) -> BrowserSession:
        """Return a ready-to-use session for ``account_id``.

        ``overrides`` (typically from ``account.metadata``) controls proxy,
        fingerprint, and profile sharing. If the requested profile directory
        is currently held by *another* account, that other session is closed
        first — Chromium does not allow concurrent use of the same profile
        directory. Sequential test runs across accounts that share a
        profile_id therefore work correctly.
        """
        ov = overrides or BrowserOverrides()
        async with self._lock:
            existing = self._sessions.get(account_id)
            target_profile = self._resolve_profile_dir(account_id, ov)
            if existing and existing.healthy and existing.profile_dir == target_profile:
                return existing
            if existing:
                await self._close_session_locked(account_id)
            # If a different account holds this profile dir, evict them.
            holder = self._profile_owner.get(target_profile)
            if holder and holder != account_id:
                log.info(
                    "profile %s currently held by %s; closing for %s",
                    target_profile, holder, account_id,
                )
                await self._close_session_locked(holder)
            return await self._create_session_locked(account_id, ov, target_profile)

    async def close(self, account_id: str) -> bool:
        async with self._lock:
            return await self._close_session_locked(account_id)

    async def reset_profile(self, account_id: str) -> None:
        """Delete the persistent profile directory for an account.

        Uses the *default* profile (``profiles_root/account_id``); shared
        profiles are not deleted by this call to avoid wiping another
        account's state.
        """
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
    def _resolve_profile_dir(
        self, account_id: str, overrides: BrowserOverrides,
    ) -> Path:
        key = overrides.profile_id or account_id
        return self.cfg.profiles_root / _safe(key)

    def _build_launch_kwargs(self, overrides: BrowserOverrides) -> dict[str, Any]:
        """Compose Playwright kwargs from base config + overrides."""
        kwargs: dict[str, Any] = {
            "headless": self.cfg.headless,
            "slow_mo": self.cfg.slow_mo_ms,
            "args": self.cfg.args,
            "viewport": {"width": self.cfg.viewport[0], "height": self.cfg.viewport[1]},
            "user_agent": self.cfg.user_agent,
            "locale": self.cfg.locale,
            "timezone_id": self.cfg.timezone_id,
            "downloads_path": str(self.cfg.downloads_dir),
        }
        if self.cfg.proxy:
            kwargs["proxy"] = self.cfg.proxy
        overrides.merge_into(kwargs)
        # remove None to satisfy Playwright API
        return {k: v for k, v in kwargs.items() if v is not None}

    async def _create_session_locked(
        self,
        account_id: str,
        overrides: BrowserOverrides,
        profile: Path,
    ) -> BrowserSession:
        await self.start()
        assert self._playwright is not None
        profile.mkdir(parents=True, exist_ok=True)
        launch_kwargs = self._build_launch_kwargs(overrides)

        browser_type = getattr(self._playwright, self.cfg.browser)
        try:
            context = await browser_type.launch_persistent_context(
                str(profile), **launch_kwargs
            )
        except Exception:  # noqa: BLE001
            log.exception("launch_persistent_context failed; falling back to ephemeral")
            if not self._browser:
                self._browser = await browser_type.launch(headless=self.cfg.headless)
            # ephemeral context: pass only context-compatible kwargs (no
            # downloads_path / args / slow_mo / headless)
            ctx_kwargs = {
                k: v for k, v in launch_kwargs.items()
                if k not in {"args", "slow_mo", "headless", "downloads_path"}
            }
            context = await self._browser.new_context(**ctx_kwargs)
        context.set_default_timeout(self.cfg.timeout_ms)
        page = context.pages[0] if context.pages else await context.new_page()

        import time
        proxy_repr = _proxy_label(launch_kwargs.get("proxy"))
        session = BrowserSession(
            account_id=account_id,
            context=context,
            page=page,
            profile_dir=profile,
            started_at=time.time(),
            healthy=True,
            profile_id=overrides.profile_id or account_id,
            proxy=proxy_repr,
        )
        self._sessions[account_id] = session
        self._profile_owner[profile] = account_id
        log.info(
            "created browser session account=%s profile=%s proxy=%s",
            account_id, profile, proxy_repr or "(none)",
        )
        return session

    async def _close_session_locked(self, account_id: str) -> bool:
        session = self._sessions.pop(account_id, None)
        if not session:
            return False
        if self._profile_owner.get(session.profile_dir) == account_id:
            self._profile_owner.pop(session.profile_dir, None)
        try:
            await session.context.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing context for %s", account_id)
        return True


# -------------------------------------------------------------------- helpers
def _safe(name: str) -> str:
    """Make a string safe to use as a directory name."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:128]


def _strip_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _normalize_viewport(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            return (int(value.get("width", 0)), int(value.get("height", 0))) \
                if value.get("width") and value.get("height") else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _normalize_geolocation(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        out: dict[str, float] = {
            "latitude": float(value["latitude"]),
            "longitude": float(value["longitude"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if "accuracy" in value:
        try:
            out["accuracy"] = float(value["accuracy"])
        except (TypeError, ValueError):
            pass
    return out


def _normalize_proxy(value: Any) -> str | dict[str, str] | None:
    """Accept a URL string or Playwright's proxy dict.

    URL strings are parsed with :func:`urllib.parse.urlsplit` and converted
    into Playwright's dict form so embedded credentials are split into
    ``username`` / ``password`` (Playwright requires them separate).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value if "server" in value else None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if not parts.scheme or not parts.hostname:
        return None
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    out: dict[str, str] = {"server": server}
    if parts.username:
        out["username"] = parts.username
    if parts.password:
        out["password"] = parts.password
    return out


def _proxy_label(proxy: Any) -> str | None:
    """Return a credential-free string for logs."""
    if not proxy:
        return None
    if isinstance(proxy, dict):
        return proxy.get("server")
    if isinstance(proxy, str):
        parts = urlsplit(proxy)
        if parts.scheme and parts.hostname:
            host = f"{parts.scheme}://{parts.hostname}"
            if parts.port:
                host += f":{parts.port}"
            return host
        return proxy
    return None
