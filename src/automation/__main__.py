"""Entry points: ``python -m automation`` runs the framework.

Subcommands::

    python -m automation server                # API + engine
    python -m automation worker                # standalone worker node
    python -m automation cli ...               # CLI client (delegates to controllers.cli)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from automation.config.manager import ConfigManager
from automation.logging_system.setup import setup_logging

log = logging.getLogger("automation.main")


# ---------------------------------------------------------------------- server
async def _run_server(config_path: str, host: str, port: int) -> None:
    import uvicorn

    config = ConfigManager(config_path)
    log_cfg = config.get("logging", {}) or {}
    setup_logging(
        log_dir=log_cfg.get("dir", "data/logs"),
        level=log_cfg.get("level", "INFO"),
        debug_logs=bool(log_cfg.get("debug", False)),
    )

    # late imports so logging is configured first
    from automation.accounts.manager import AccountManager
    from automation.ai.brain import AIBrain
    from automation.api.app import create_app
    from automation.browser.manager import BrowserConfig, BrowserManager
    from automation.controllers.telegram_bot import TelegramController
    from automation.controllers.workflow_engine import WorkflowEngine
    from automation.core.engine import Engine

    engine = Engine(config)

    accounts: AccountManager | None = None
    accounts_file = config.get("accounts_file")
    if accounts_file:
        accounts = AccountManager(
            source_file=accounts_file,
            state_file=config.get("accounts_state", "data/state/accounts.sqlite"),
            max_attempts=int(config.get("accounts_max_attempts", 3)),
            lease_seconds=float(config.get("accounts_lease_seconds", 600.0)),
            require_password=bool(config.get("accounts_require_password", True)),
        )
        accounts.load()

    browser: BrowserManager | None = None
    if config.get("playwright.enabled", True):
        browser = BrowserManager(BrowserConfig.from_dict(config.as_dict()))

    brain: AIBrain | None = None
    if config.get("ai.enabled", True):
        brain = AIBrain.from_config(config.as_dict())

    workflow_engine = WorkflowEngine(brain=brain)

    # Create autonomous browser agent
    from automation.agent.agent import BrowserAgent
    agent: BrowserAgent | None = None
    if brain:
        agent = BrowserAgent(
            brain=brain,
            browser=browser,
            accounts_manager=accounts,
            event_bus=engine.event_bus,
            runs_root=config.get("agent.runs_root", "data/runs"),
            max_parallel=int(config.get("agent.max_parallel", 4)),
        )

    app = create_app(
        engine=engine,
        accounts=accounts,
        browser=browser,
        brain=brain,
        workflow_engine=workflow_engine,
        agent=agent,
        workflows_dir=config.get("workflows_dir", "config/workflows"),
        enable_dashboard=bool(config.get("dashboard.enabled", True)),
    )

    # bring up the engine in-process so the API has a live engine to talk to
    await engine.start()

    # optional Telegram controller
    tg = TelegramController.from_config(config.as_dict())
    if tg:
        await tg.start()

    # optional config watcher
    watcher_task: asyncio.Task | None = None
    if bool(config.get("config_watch", True)):
        watcher_task = asyncio.create_task(
            config.watch(interval=float(config.get("config_watch_interval", 2.0))),
            name="config-watch",
        )

    # optional accounts.json hot reload watcher
    if accounts and bool(config.get("accounts_watch", True)):
        accounts.start_watcher(
            interval=float(config.get("accounts_watch_interval", 2.0))
        )

    log.info("starting API on %s:%d", host, port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    try:
        await server.serve()
    finally:
        if tg:
            await tg.stop()
        if watcher_task:
            watcher_task.cancel()
        if accounts:
            await accounts.stop_watcher()
            accounts.close()
        if browser:
            await browser.stop()
        await engine.stop()


# ---------------------------------------------------------------------- worker
async def _run_worker(coordinator_url: str, capabilities: list[str]) -> None:
    setup_logging()
    from automation.distributed.worker import WorkerNode

    token = os.environ.get("AUTOMATION_API_TOKEN")
    worker = WorkerNode(
        coordinator_url=coordinator_url, api_token=token, capabilities=capabilities,
    )

    async def handler(assignment: dict) -> dict:
        log.info("worker handling assignment id=%s kind=%s",
                 assignment.get("id"), assignment.get("kind"))
        # Default no-op handler. Plugins or downstream apps should subclass.
        return {"echoed": True, "kind": assignment.get("kind")}

    worker.set_handler(handler)
    await worker.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await worker.stop()


# ------------------------------------------------------------------------ root
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="automation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_server = sub.add_parser("server", help="run API + engine")
    p_server.add_argument("--config", default=os.environ.get("AUTOMATION_CONFIG", "config/config.json"))
    p_server.add_argument("--host", default=os.environ.get("AUTOMATION_HOST", "0.0.0.0"))
    p_server.add_argument("--port", type=int, default=int(os.environ.get("AUTOMATION_PORT", "8080")))

    p_worker = sub.add_parser("worker", help="run a standalone worker node")
    p_worker.add_argument(
        "--coordinator-url",
        default=os.environ.get("AUTOMATION_API_URL", "http://127.0.0.1:8080"),
    )
    p_worker.add_argument("--capabilities", nargs="*", default=[])

    p_cli = sub.add_parser("cli", help="run the CLI client")
    p_cli.add_argument("args", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    if args.cmd == "server":
        try:
            asyncio.run(_run_server(args.config, args.host, args.port))
        except KeyboardInterrupt:
            return 0
        return 0
    if args.cmd == "worker":
        try:
            asyncio.run(_run_worker(args.coordinator_url, args.capabilities))
        except KeyboardInterrupt:
            return 0
        return 0
    if args.cmd == "cli":
        from automation.controllers.cli import main as cli_main
        return cli_main(args.args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
