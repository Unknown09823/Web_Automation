"""Engine: ties together event bus, plugins, scheduler, queue, state, watchdog.

The engine is the single entry point that brings up and tears down all
subsystems, dispatches lifecycle events, and supports graceful shutdown.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from automation.config.manager import ConfigManager
from automation.core.event_bus import Event, EventBus
from automation.core.health_monitor import HealthMonitor
from automation.core.queue_manager import QueueManager
from automation.core.scheduler import Scheduler
from automation.core.state_manager import StateManager
from automation.core.task_manager import TaskManager
from automation.core.watchdog import Watchdog
from automation.plugins.loader import PluginLoader

log = logging.getLogger(__name__)


class Engine:
    """Central orchestrator. Async-first, exception-isolated, hot-reload aware."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self.event_bus = EventBus()
        self.task_manager = TaskManager(self.event_bus)
        self.scheduler = Scheduler(
            max_workers=int(config.get("scheduler.max_workers", 10))
        )
        self.queue_manager = QueueManager()
        self.state = StateManager(path=config.get("state.path", "data/state/state.json"))
        self.health = HealthMonitor()
        self.watchdog = Watchdog(
            interval=float(config.get("watchdog.interval", 15)),
            max_recoveries=int(config.get("watchdog.max_recoveries", 5)),
        )
        self.plugins = PluginLoader(self)
        self._running = False
        self._stop_event = asyncio.Event()
        self._signals_installed = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        log.info("Engine starting...")
        self.health.set_component("engine", "starting")
        await self.event_bus.publish(Event("engine.starting"))
        # config hot reload hook
        self.config.add_listener(self._on_config_reload)
        # discover and start plugins
        await self.plugins.discover()
        await self.plugins.start_all()
        # start scheduler & watchdog
        await self.scheduler.start()
        await self.watchdog.start()
        self._install_signal_handlers()
        self._running = True
        self.health.set_component("engine", "ok")
        await self.event_bus.publish(Event("engine.started"))
        log.info("Engine started")

    async def stop(self) -> None:
        if not self._running:
            return
        log.info("Engine stopping...")
        await self.event_bus.publish(Event("engine.stopping"))
        self.health.set_component("engine", "stopping")
        try:
            await self.plugins.stop_all()
        except Exception:  # noqa: BLE001
            log.exception("Error stopping plugins")
        try:
            await self.scheduler.stop()
        except Exception:  # noqa: BLE001
            log.exception("Error stopping scheduler")
        try:
            await self.watchdog.stop()
        except Exception:  # noqa: BLE001
            log.exception("Error stopping watchdog")
        try:
            await self.task_manager.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("Error stopping task manager")
        self._running = False
        self.health.set_component("engine", "stopped")
        await self.event_bus.publish(Event("engine.stopped"))
        self._stop_event.set()
        log.info("Engine stopped")

    async def restart(self) -> None:
        await self.stop()
        # rebuild critical components that hold state across restart
        self._stop_event = asyncio.Event()
        await self.start()

    async def reload(self) -> None:
        """Re-read configuration and reload all plugins."""
        log.info("Engine reloading...")
        await self.event_bus.publish(Event("engine.reloading"))
        await self.config.reload()
        await self.plugins.reload_all()
        await self.event_bus.publish(Event("engine.reloaded"))
        log.info("Engine reloaded")

    async def wait_until_stopped(self) -> None:
        await self._stop_event.wait()

    async def _on_config_reload(self, _new: dict[str, Any]) -> None:
        log.info("Engine: config changed, propagating reload")
        try:
            self.scheduler.set_max_workers(
                int(self.config.get("scheduler.max_workers", 10))
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed applying scheduler config")
        await self.event_bus.publish(Event("config.reloaded"))

    def _install_signal_handlers(self) -> None:
        if self._signals_installed:
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
            except (NotImplementedError, RuntimeError):
                # Windows or non-main thread
                pass
        self._signals_installed = True
