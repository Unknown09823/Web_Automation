"""Plugin system: base class, loader, registry."""
from automation.plugins.base import Plugin
from automation.plugins.loader import PluginLoader
from automation.plugins.registry import PluginRecord, PluginRegistry

__all__ = ["Plugin", "PluginLoader", "PluginRecord", "PluginRegistry"]
