"""Configuration package.

Two layers:

* :class:`Settings` (``settings.py``) is the canonical, env-aware loader for
  the Telegram-first agent. New code should use :func:`get_settings`.
* :class:`ConfigManager` (``manager.py``) is the legacy hot-reloadable
  JSON/YAML loader still used by older modules. Kept for back-compat; it
  reads the same files Settings does.
"""
from automation.config.manager import ConfigManager
from automation.config.settings import Settings, get_settings, init_settings

__all__ = ["ConfigManager", "Settings", "get_settings", "init_settings"]
