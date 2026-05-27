"""Configuration manager: JSON/YAML loading, env overrides, hot reload."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - YAML is optional
    yaml = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

ConfigListener = Callable[[dict[str, Any]], Awaitable[None]]


class ConfigManager:
    """Loads JSON or YAML config with dotted-key access and hot reload.

    Env variable overrides:
      Any environment variable prefixed with ``AUTOMATION__`` overrides a key
      using ``__`` as the separator. Example::

          AUTOMATION__SCHEDULER__MAX_WORKERS=20  -> scheduler.max_workers = 20
    """

    ENV_PREFIX = "AUTOMATION__"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self._listeners: list[ConfigListener] = []
        self._lock = asyncio.Lock()
        self._mtime: float = 0.0
        self.load()

    # ---------------------------------------------------------------- loading
    def load(self) -> None:
        if not self.path.exists():
            log.warning("Config file %s not found, using defaults", self.path)
            self._data = {}
        else:
            text = self.path.read_text()
            data: dict[str, Any]
            if self.path.suffix.lower() in (".yaml", ".yml"):
                if yaml is None:
                    raise RuntimeError("YAML config requires PyYAML")
                data = yaml.safe_load(text) or {}
            else:
                data = json.loads(text)
            self._data = data
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime = 0.0
        self._apply_env_overrides()
        log.info("Config loaded from %s", self.path)

    def _apply_env_overrides(self) -> None:
        for key, val in os.environ.items():
            if not key.startswith(self.ENV_PREFIX):
                continue
            dotted = key[len(self.ENV_PREFIX) :].lower().replace("__", ".")
            self._set_dotted(dotted, _coerce(val))

    # ------------------------------------------------------------------ access
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def _set_dotted(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for p in parts[:-1]:
            if p not in node or not isinstance(node[p], dict):
                node[p] = {}
            node = node[p]
        node[parts[-1]] = value

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    # ------------------------------------------------------------- hot reload
    async def reload(self) -> None:
        async with self._lock:
            old = json.dumps(self._data, sort_keys=True, default=str)
            self.load()
            new = json.dumps(self._data, sort_keys=True, default=str)
            if old == new:
                return
            for listener in list(self._listeners):
                try:
                    await listener(self._data)
                except Exception:  # noqa: BLE001
                    log.exception("Config listener failed")

    def add_listener(self, listener: ConfigListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: ConfigListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    # ---------------------------------------------------------- file watching
    async def watch(self, interval: float = 2.0) -> None:
        """Polling file watcher (works everywhere, no inotify dependency)."""
        log.info("Config watcher started (interval=%.1fs)", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                if not self.path.exists():
                    continue
                mtime = self.path.stat().st_mtime
                if mtime > self._mtime:
                    self._mtime = mtime
                    log.info("Config file changed, reloading")
                    await self.reload()
            except Exception:  # noqa: BLE001
                log.exception("Config watcher tick failed")


def _coerce(value: str) -> Any:
    """Coerce env strings to bool/int/float/json when possible."""
    v = value.strip()
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", ""):
        return None
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        pass
    if v.startswith(("[", "{")):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
    return v
