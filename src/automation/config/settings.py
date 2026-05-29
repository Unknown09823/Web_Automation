"""Unified settings + secret manager.

Single source of truth for ALL framework configuration. Loads in this order
of precedence (later wins):

  1. ``config/master_config.json`` (or legacy ``config/config.json``)
  2. ``.env`` file in repo root
  3. Process environment variables
  4. ``AUTOMATION__SECTION__KEY`` dotted env overrides

Secrets are never written to logs / dashboards / Telegram messages. The
:class:`Settings` class exposes:

  * ``settings.get(key, default=None)``    - dotted accessor (works for both
    config keys and env-style flat keys via the alias map)
  * ``settings.get_secret(key)``           - same, but flagged for masking
  * ``settings.set(key, value, persist=True)`` - update a non-secret config
    key and persist to ``master_config.json`` (used by Telegram /config set)
  * ``settings.mask(key, value)``          - mask a value when key is sensitive
  * ``settings.public_dict()``             - full config with all secrets
    replaced by ``"****"`` (safe to display)
  * ``settings.startup_report()``          - list of (component, ok, detail)
    tuples for the boot banner

The class is intentionally dependency-free: no python-dotenv, no pydantic-
settings. Stdlib only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Keys whose values must never be displayed in clear text. Match is
# case-insensitive against the *final* path segment.
_SECRET_KEY_HINTS = (
    "token", "secret", "key", "password", "passwd", "credential",
    "api_key", "hmac",
)


# Map flat-style env var names to dotted config keys. The Settings class
# uses this so callers can write either ``settings.get("MAX_PARALLEL_WORKERS")``
# (env name) or ``settings.get("execution.max_parallel_workers")`` and get
# the same value.
_ENV_TO_CONFIG: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN":        "telegram.token",
    "TELEGRAM_ALLOWED_CHAT_IDS": "telegram.allowed_chat_ids",
    "TELEGRAM_DEFAULT_CHAT_ID":  "telegram.default_chat_id",
    "GROQ_API_KEY":              "ai.groq_api_key",
    "OPENROUTER_API_KEY":        "ai.openrouter_api_key",
    "OPENAI_API_KEY":            "ai.openai_api_key",
    "ANTHROPIC_API_KEY":         "ai.anthropic_api_key",
    "AUTOMATION_API_TOKEN":      "api.token",
    "AUTOMATION_API_URL":        "api.url",
    "CALLBACK_HMAC_KEY":         "api.callback_hmac_key",
    "HEADLESS":                  "browser.headless",
    "DEFAULT_TIMEOUT":           "execution.default_timeout_seconds",
    "MAX_PARALLEL_WORKERS":      "execution.max_parallel_workers",
    "MAX_RETRIES":               "execution.max_retries",
    "SCREENSHOT_PATH":           "screenshots.dir",
    "RUNS_PATH":                 "runs.path",
    "TEMPLATES_PATH":            "templates.dir",
    "LEARNING_PATH":             "memory.learning_dir",
    "DEFAULT_PLANNER_MODEL":     "ai.default_planner_model",
    "DEFAULT_REASONING_MODEL":   "ai.default_reasoning_model",
}


def _is_secret(key: str) -> bool:
    """Return True if a dotted or flat key is sensitive."""
    leaf = key.rsplit(".", 1)[-1].lower()
    return any(hint in leaf for hint in _SECRET_KEY_HINTS)


def _coerce(value: str) -> Any:
    """Coerce env strings to bool/int/float/json/list when possible."""
    v = value.strip()
    if v == "":
        return ""
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none"):
        return None
    try:
        if "." in v and not v.startswith("0."):
            return float(v)
        return int(v)
    except ValueError:
        pass
    if v.startswith(("[", "{")):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
    if "," in v and not v.startswith("/") and not v.startswith("http"):
        # comma-separated values become a list
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if len(parts) > 1:
            return parts
    return v


def _parse_dotenv(text: str) -> dict[str, str]:
    """Tiny .env parser: KEY=VALUE per line, # comments, optional quotes."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip matching surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # Strip inline comment for unquoted values (safe: ignore # inside #)
        if "#" in value and not (raw.lstrip().startswith(("'", '"'))):
            value = value.split("#", 1)[0].rstrip()
        out[key] = value
    return out


class Settings:
    """The single configuration object the rest of the framework reads."""

    ENV_PREFIX = "AUTOMATION__"

    def __init__(
        self,
        master_config_path: str | Path | None = None,
        env_file: str | Path | None = None,
    ) -> None:
        self.master_path = (
            Path(master_config_path)
            if master_config_path
            else self._find_master_config()
        )
        self.env_file = Path(env_file) if env_file else Path(".env")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._dotenv: dict[str, str] = {}
        self.load()

    # ---------------------------------------------------------------- discovery
    @staticmethod
    def _find_master_config() -> Path:
        """Prefer master_config.json over the legacy config.json."""
        master = Path("config/master_config.json")
        if master.exists():
            return master
        legacy = Path("config/config.json")
        return legacy

    # ---------------------------------------------------------------- loading
    def load(self) -> None:
        with self._lock:
            data: dict[str, Any] = {}
            if self.master_path.exists():
                try:
                    raw = self.master_path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("master config must be a JSON object")
                except Exception:  # noqa: BLE001
                    log.exception(
                        "could not load %s; starting with empty config",
                        self.master_path,
                    )
                    data = {}
            else:
                log.warning(
                    "Master config %s missing; defaults only", self.master_path,
                )

            self._data = data

            # Layer .env file on top
            if self.env_file.exists():
                try:
                    self._dotenv = _parse_dotenv(
                        self.env_file.read_text(encoding="utf-8")
                    )
                except Exception:  # noqa: BLE001
                    log.exception("could not parse %s", self.env_file)
                    self._dotenv = {}
            else:
                self._dotenv = {}

            self._apply_env_layer()

    def _apply_env_layer(self) -> None:
        """Apply dotenv + os.environ + AUTOMATION__ overrides onto self._data."""
        # First the dotenv (only fills variables NOT already in os.environ —
        # process env wins so deployments can override .env).
        for k, v in self._dotenv.items():
            os.environ.setdefault(k, v)

        # Friendly env-var aliases (HEADLESS, GROQ_API_KEY, etc.) → dotted keys
        for env_name, dotted in _ENV_TO_CONFIG.items():
            val = os.environ.get(env_name)
            if val is not None and val != "":
                self._set_dotted(dotted, _coerce(val))

        # AUTOMATION__SECTION__KEY pattern → dotted overrides
        for key, val in os.environ.items():
            if not key.startswith(self.ENV_PREFIX):
                continue
            dotted = key[len(self.ENV_PREFIX):].lower().replace("__", ".")
            self._set_dotted(dotted, _coerce(val))

    # -------------------------------------------------------------- accessors
    def get(self, key: str, default: Any = None) -> Any:
        """Read a config value by dotted key OR by env-var alias.

        ``settings.get("GROQ_API_KEY")`` and
        ``settings.get("ai.groq_api_key")`` return the same value.
        """
        with self._lock:
            # env alias path
            if key in _ENV_TO_CONFIG:
                dotted = _ENV_TO_CONFIG[key]
                v = self._get_dotted(dotted)
                if v not in (None, ""):
                    return v
                # also check raw os.environ for dynamic values
                env_v = os.environ.get(key)
                if env_v not in (None, ""):
                    return _coerce(env_v)
                return default

            # plain dotted access
            v = self._get_dotted(key)
            return v if v is not None else default

    def _get_dotted(self, dotted: str) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    def _set_dotted(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for p in parts[:-1]:
            if p not in node or not isinstance(node[p], dict):
                node[p] = {}
            node = node[p]
        node[parts[-1]] = value

    def set(
        self,
        key: str,
        value: Any,
        *,
        persist: bool = True,
        allow_secrets: bool = False,
    ) -> None:
        """Update a config key. By default refuses to set secret keys.

        ``persist=True`` writes the change back to master_config.json so
        it survives a restart. Used by ``/config set`` from Telegram.
        """
        if _is_secret(key) and not allow_secrets:
            raise PermissionError(
                f"refusing to set secret key {key!r}; edit .env instead",
            )
        dotted = _ENV_TO_CONFIG.get(key, key)
        with self._lock:
            self._set_dotted(dotted, value)
            if persist:
                self._persist_master()

    def _persist_master(self) -> None:
        # Strip transient/derived keys before saving
        try:
            self.master_path.parent.mkdir(parents=True, exist_ok=True)
            self.master_path.write_text(
                json.dumps(self._data, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            log.exception("could not persist master config")

    def get_secret(self, key: str) -> str:
        """Read a secret. Returns "" when not configured."""
        v = self.get(key, "")
        return str(v) if v is not None else ""

    def has_secret(self, key: str) -> bool:
        v = self.get_secret(key)
        return bool(v and str(v).strip())

    # --------------------------------------------------------------- masking
    def mask(self, key: str, value: Any) -> Any:
        """Mask a value when its key is sensitive."""
        if _is_secret(key) and value not in (None, "", []):
            text = str(value)
            return "****" if len(text) <= 8 else f"{text[:2]}****{text[-2:]}"
        return value

    def public_dict(self) -> dict[str, Any]:
        """Full config with secrets masked. Safe to display."""
        return _scrub(self._data, _is_secret_key=_is_secret)

    def as_dict(self) -> dict[str, Any]:
        """Full config including secrets. Internal use only."""
        with self._lock:
            return json.loads(json.dumps(self._data, default=str))

    # --------------------------------------------------------------- aliases
    def telegram_allowed_chat_ids(self) -> list[int]:
        """Parse TELEGRAM_ALLOWED_CHAT_IDS into a list of ints."""
        raw = self.get("TELEGRAM_ALLOWED_CHAT_IDS")
        if raw is None:
            return []
        if isinstance(raw, list):
            return [int(x) for x in raw if str(x).strip()]
        if isinstance(raw, (int, float)):
            return [int(raw)]
        return [int(x.strip()) for x in str(raw).split(",") if x.strip()]

    def default_chat_id(self) -> int | None:
        v = self.get("TELEGRAM_DEFAULT_CHAT_ID")
        if v in (None, ""):
            allowed = self.telegram_allowed_chat_ids()
            return allowed[0] if allowed else None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    # --------------------------------------------------------------- legacy adapter
    def legacy_config_dict(self) -> dict[str, Any]:
        """Adapt the new layout to what existing modules expect.

        Several modules (``BrowserConfig.from_dict``, ``AIBrain.from_config``,
        ``__main__._run_server``) currently read from the *old* config.json
        shape. This method composes a dict with both layouts merged so we
        don't have to rewrite every consumer at once.
        """
        with self._lock:
            base = self.as_dict()

            # Map the new "browser" section into the legacy "playwright" key
            # so BrowserConfig.from_dict() keeps working.
            browser = base.get("browser") or {}
            playwright = {
                "enabled": True,
                "headless": bool(self.get("browser.headless", True)),
                "timeout": int(self.get("execution.default_timeout_seconds", 60))
                            * 1000,
                "browser": browser.get("engine", "chromium"),
                "viewport": browser.get("viewport") or {"width": 1280, "height": 800},
                "profiles_root": browser.get("profiles_root", "data/profiles"),
                "downloads_dir": browser.get("downloads_dir", "data/downloads"),
                "args": browser.get("args", []),
                "slow_mo_ms": browser.get("slow_mo_ms", 0),
                "proxy": browser.get("proxy"),
                "user_agent": browser.get("user_agent"),
                "locale": browser.get("locale"),
                "timezone_id": browser.get("timezone_id"),
            }
            base.setdefault("playwright", playwright)

            # Map "memory.selector_memory_path" into legacy ai.memory_path
            ai_legacy = base.get("ai", {}) or {}
            ai_legacy.setdefault(
                "memory_enabled", bool(self.get("memory.enabled", True)),
            )
            ai_legacy.setdefault(
                "memory_path",
                self.get("memory.selector_memory_path",
                         "data/learning/memory.sqlite"),
            )
            ai_legacy.setdefault(
                "screenshot_dir", self.get("screenshots.dir", "data/screenshots"),
            )
            ai_legacy.setdefault(
                "max_heal_attempts",
                int(self.get("execution.max_retries", 3)),
            )
            base["ai"] = ai_legacy

            # Map accounts.* from new layout to flat legacy keys
            accounts = base.get("accounts") or {}
            base.setdefault("accounts_file", accounts.get("source_file"))
            base.setdefault("accounts_state", accounts.get("state_file"))
            base.setdefault(
                "accounts_max_attempts", accounts.get("max_attempts", 3),
            )
            base.setdefault(
                "accounts_lease_seconds", accounts.get("lease_seconds", 600),
            )
            base.setdefault(
                "accounts_require_password", accounts.get("require_password", True),
            )
            base.setdefault("accounts_watch", accounts.get("watch", True))
            base.setdefault(
                "accounts_watch_interval",
                accounts.get("watch_interval_seconds", 2.0),
            )

            # agent.* legacy keys
            agent = base.get("agent") or {}
            agent.setdefault(
                "runs_root", self.get("runs.path", "data/runs"),
            )
            agent.setdefault(
                "max_parallel", int(self.get("execution.max_parallel_workers", 5)),
            )
            agent.setdefault("enabled", True)
            agent.setdefault(
                "default_timeout_seconds",
                int(self.get("execution.default_timeout_seconds", 120)),
            )
            agent.setdefault(
                "adaptive_wait_poll_ms",
                int(self.get("execution.adaptive_wait_poll_ms", 200)),
            )
            agent.setdefault(
                "dom_settle_ms", int(self.get("execution.dom_settle_ms", 500)),
            )
            agent.setdefault(
                "verification_threshold",
                float(self.get("execution.verification_threshold", 0.6)),
            )
            base["agent"] = agent
            return base

    # -------------------------------------------------------- startup report
    def startup_report(self) -> list[tuple[str, bool, str]]:
        """Return rows of (component_name, ok, detail) for the boot banner.

        ``ok`` follows the convention: ``True`` for required-and-present or
        optional-and-present, ``False`` for required-and-missing. Optional
        components produce their own row with ``optional=True`` flagged in
        the detail string.
        """
        rows: list[tuple[str, bool, str]] = []

        # Telegram (the primary control surface; optional but warned)
        if self.has_secret("TELEGRAM_BOT_TOKEN"):
            chats = self.telegram_allowed_chat_ids()
            chat_summary = (
                f"chats={len(chats)}" if chats else "no allowed chats (open!)"
            )
            rows.append(("Telegram", True, f"configured ({chat_summary})"))
        else:
            rows.append(("Telegram", False, "TELEGRAM_BOT_TOKEN missing"))

        # AI provider chain — at least one must be present for LLM features
        ai_provider = (self.get("ai.provider") or "groq").lower()
        if self.has_secret("GROQ_API_KEY"):
            rows.append((
                "Groq",
                True,
                f"model={self.get('ai.default_planner_model', 'qwen/qwen3-32b')}",
            ))
        else:
            rows.append((
                "Groq",
                ai_provider != "groq",  # not failing if user picked another
                "GROQ_API_KEY missing",
            ))

        for prov, env_var in (
            ("OpenRouter", "OPENROUTER_API_KEY"),
            ("OpenAI",     "OPENAI_API_KEY"),
            ("Anthropic",  "ANTHROPIC_API_KEY"),
        ):
            if self.has_secret(env_var):
                rows.append((prov, True, "configured (optional)"))
            else:
                rows.append((prov, True, "missing (optional)"))

        # Local API
        if self.has_secret("AUTOMATION_API_TOKEN"):
            rows.append(("API auth", True, "bearer token set"))
        else:
            rows.append((
                "API auth", True,
                "AUTOMATION_API_TOKEN missing — running OPEN (do not expose)",
            ))

        # Storage
        runs_dir = Path(self.get("runs.path", "data/runs"))
        ok = self._ensure_dir(runs_dir)
        rows.append(("Storage", ok, f"runs at {runs_dir}"))

        # Browser engine name (validation that key resolves; not that it works)
        engine = self.get("browser.engine", "chromium")
        rows.append((
            "Browser",
            True,
            f"engine={engine} headless={bool(self.get('browser.headless', True))}",
        ))

        # Templates dir
        tmpl = Path(self.get("templates.dir", "data/templates"))
        ok = self._ensure_dir(tmpl)
        rows.append(("Templates", ok, str(tmpl)))

        # Site memory dir
        sites = Path(self.get("memory.site_memory_dir", "data/learning/sites"))
        ok = self._ensure_dir(sites)
        rows.append(("Site memory", ok, str(sites)))

        return rows

    @staticmethod
    def _ensure_dir(path: Path) -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return True
        except Exception:  # noqa: BLE001
            return False

    def render_startup_banner(self) -> str:
        """Pretty-printed startup report. Use this in __main__."""
        lines = ["", "=" * 60, " AUTOMATION FRAMEWORK STARTUP", "=" * 60]
        for name, ok, detail in self.startup_report():
            mark = "OK  " if ok else "MISS"
            lines.append(f"  [{mark}] {name:<14} {detail}")
        lines.append("=" * 60)
        lines.append("")
        return "\n".join(lines)


def _scrub(value: Any, *, _is_secret_key, _path: str = "") -> Any:
    """Recursively mask secret keys in a config tree."""
    if isinstance(value, dict):
        return {
            k: ("****" if _is_secret_key(k) else _scrub(
                v, _is_secret_key=_is_secret_key, _path=f"{_path}.{k}")
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _scrub(item, _is_secret_key=_is_secret_key, _path=_path)
            for item in value
        ]
    return value


# ---------------------------------------------------------------- module-level
_settings_singleton: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance."""
    global _settings_singleton
    if _settings_singleton is None:
        _settings_singleton = Settings()
    return _settings_singleton


def init_settings(
    master_config_path: str | Path | None = None,
    env_file: str | Path | None = None,
) -> Settings:
    """Reset the singleton with explicit paths (for tests / startup)."""
    global _settings_singleton
    _settings_singleton = Settings(master_config_path, env_file)
    return _settings_singleton


__all__ = ["Settings", "get_settings", "init_settings"]
