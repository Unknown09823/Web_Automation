"""Health monitor: collects system metrics and component status."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - optional in some envs
    psutil = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


@dataclass(slots=True)
class HealthSnapshot:
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    disk_percent: float
    uptime_seconds: float
    status: str = "ok"
    components: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class HealthMonitor:
    """Tracks process and component health."""

    def __init__(self) -> None:
        self._started = time.time()
        self._components: dict[str, str] = {}

    def set_component(self, name: str, status: str) -> None:
        self._components[name] = status

    def remove_component(self, name: str) -> None:
        self._components.pop(name, None)

    def snapshot(self) -> HealthSnapshot:
        cpu = mem = mem_mb = disk = 0.0
        if psutil is not None:
            try:
                cpu = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                mem = vm.percent
                mem_mb = (vm.total - vm.available) / (1024 * 1024)
                disk = psutil.disk_usage("/").percent
            except Exception:  # noqa: BLE001
                log.exception("Failed to collect psutil metrics")
        status = "ok"
        if any(v == "error" for v in self._components.values()):
            status = "degraded"
        if cpu > 95 or mem > 95:
            status = "degraded"
        return HealthSnapshot(
            timestamp=time.time(),
            cpu_percent=cpu,
            memory_percent=mem,
            memory_used_mb=mem_mb,
            disk_percent=disk,
            uptime_seconds=time.time() - self._started,
            status=status,
            components=dict(self._components),
        )
