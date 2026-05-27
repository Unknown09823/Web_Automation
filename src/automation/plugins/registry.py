"""Plugin registry: tracks loaded plugins and their state."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automation.plugins.base import Plugin

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PluginRecord:
    name: str
    instance: "Plugin"
    module_path: str
    enabled: bool = True
    started: bool = False
    error: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class PluginRegistry:
    """In-memory registry of loaded plugins."""

    def __init__(self) -> None:
        self._records: dict[str, PluginRecord] = {}

    def register(self, record: PluginRecord) -> None:
        if record.name in self._records:
            log.warning("Replacing existing plugin record: %s", record.name)
        self._records[record.name] = record

    def unregister(self, name: str) -> PluginRecord | None:
        return self._records.pop(name, None)

    def get(self, name: str) -> PluginRecord | None:
        return self._records.get(name)

    def all(self) -> list[PluginRecord]:
        return list(self._records.values())

    def names(self) -> list[str]:
        return list(self._records.keys())
