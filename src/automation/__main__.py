"""Entry points: ``python -m automation`` runs the framework.

Subcommands::

    python -m automation server                # API + engine + Telegram bot
    python -m automation bot                   # Telegram bot only (no FastAPI)
    python -m automation worker                # standalone worker node
    python -m automation cli ...               # CLI client
    python -m automation doctor                # validate configuration

The Telegram-first deployment story:

  1. Copy ``.env.example`` → ``.env`` and fill in TELEGRAM_BOT_TOKEN +
     TELEGRAM_ALLOWED_CHAT_IDS + GROQ_API_KEY (at minimum).
  2. ``python -m automation server`` boots everything; the startup banner
     reports which components are configured.
  3. From Telegram: send any natural-language instruction or use one of the
     ``/`` commands listed in ``/help``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from automation.config.manager import ConfigManager
from automation.config.settings import init_settings, get_settings
from automation.logging_system.setup import setup_logging

log = logging.getLogger("automation.main")


# ============================================================================
#  doctor — validate configuration without booting the framework
# ============================================================================
def _run_doctor(master_path: str | None, env_path: str | None) -> int:
    settings = init_settings(master_path, env_path)
    print(settings.render_startup_banner())
    rows = settings.startup_report()
    failed = [(n, d) for n, ok, d in rows if not ok]
    if not failed:
        print("All required components are configured.")
        return 0
    print("Missing/misconfigured:")
    for name, detail in failed:
        print(f"  - {name}: {detail}")
    print("\nFix .env and re-run.")
    return 1


# ============================================================================
#  server — full stack (FastAPI + agent + Telegram)
# ============================================================================
async def _run_server(
    master_path: str | None,
    env_path: str | None,
    host: str,
    port: int,
) -> None:
    import uvicorn

    settings = init_settings(master_path, env_path)
    legacy = settings.legacy_config_dict()

    # Logging first so subsequent module imports surface warnings cleanly.
    log_cfg = settings.get("logging", {}) or {}
    setup_logging(
        log_dir=log_cfg.get("dir", "data/logs"),
        level=log_cfg.get("level", "INFO"),
        debug_logs=bool(log_cfg.get("debug", False)),
    )

    print(settings.render_startup_banner())

    # Late imports so logging is configured first.
    from automation.accounts.manager import AccountManager
    from automation.agent.adaptive_executor import AdaptiveExecutor
    from automation.agent.agent import BrowserAgent
    from automation.agent.execution_template import ExecutionTemplateStore
    from automation.agent.nl_planner import NLPlanner
    from automation.agent.site_memory import SiteMemory
    from automation.agent.template_recorder import TemplateRecorder
    from automation.agent.template_replayer import TemplateReplayer
    from automation.agent.templates import TemplateStore
    from automation.ai.brain import AIBrain
    from automation.ai.llm import build_llm_from_settings
    from automation.api.app import create_app
    from automation.browser.manager import BrowserConfig, BrowserManager
    from automation.controllers.telegram_command_center import CommandCenter
    from automation.controllers.workflow_engine import WorkflowEngine
    from automation.core.engine import Engine

    # Legacy ConfigManager is still used by the FastAPI layer + engine while we
    # complete the migration. It reads master_config.json now (via the
    # Settings._find_master_config preference) and will deprecate eventually.
    legacy_config_path = (
        master_path or settings.master_path
    )
    config = ConfigManager(legacy_config_path)
    # Apply env-layer changes from Settings into the legacy config so existing
    # consumers see consistent values.
    for k, v in legacy.items():
        config._set_dotted(k, v)  # noqa: SLF001  (intentional adapter)

    engine = Engine(config)

    # Accounts
    accounts: AccountManager | None = None
    accounts_file = settings.get("accounts.source_file")
    if accounts_file and Path(accounts_file).exists():
        accounts = AccountManager(
            source_file=accounts_file,
            state_file=settings.get(
                "accounts.state_file", "data/state/accounts.sqlite",
            ),
            max_attempts=int(settings.get("accounts.max_attempts", 3)),
            lease_seconds=float(settings.get("accounts.lease_seconds", 600.0)),
            require_password=bool(settings.get("accounts.require_password", True)),
        )
        accounts.load()

    # Browser
    browser: BrowserManager | None = None
    if settings.get("playwright.enabled", True) is not False:
        browser = BrowserManager(BrowserConfig.from_dict(legacy))

    # AI brain (heuristic) + LLM (Groq Qwen)
    brain: AIBrain | None = None
    if settings.get("ai.enabled", True):
        brain = AIBrain.from_config(legacy)
    llm = build_llm_from_settings(settings)
    nl_planner = NLPlanner(llm=llm)

    workflow_engine = WorkflowEngine(brain=brain)

    # Templates + site memory
    templates = TemplateStore(
        root=settings.get("templates.dir", "data/templates"),
    )
    site_memory = SiteMemory(
        root=settings.get("memory.site_memory_dir", "data/learning/sites"),
    )

    # Adaptive Execution Mode (token optimisation): replay-first, AI-fallback.
    # Account #1 teaches via the brain; #2..N replay deterministically.
    exec_template_store = ExecutionTemplateStore(
        root=settings.get(
            "memory.execution_templates_dir", "data/execution_templates",
        ),
    )
    adaptive_executor = AdaptiveExecutor(
        store=exec_template_store,
        replayer=TemplateReplayer(),
        recorder=TemplateRecorder(exec_template_store),
    )

    # Browser agent (the autonomous outer loop)
    agent: BrowserAgent | None = None
    if brain:
        agent = BrowserAgent(
            brain=brain,
            browser=browser,
            accounts_manager=accounts,
            event_bus=engine.event_bus,
            runs_root=config.get("agent.runs_root", "data/runs"),
            max_parallel=int(config.get("agent.max_parallel", 4)),
            # ----- v2 deterministic-first stack ---------------------
            # Each of these can be flipped off independently. The
            # constructor only builds a component when the master
            # ``deterministic_first`` flag is true; setting it to
            # ``false`` here skips the entire new pipeline and
            # restores the legacy brain.run flow.
            deterministic_first=bool(
                config.get("agent.deterministic_first", True),
            ),
            site_memory_root=config.get(
                "agent.site_memory_root", "data/learning/sites",
            ),
            template_store_root=config.get(
                "agent.template_store_root", "data/learning/templates",
            ),
            rules_path=config.get("agent.rules_path"),
            runs_root=settings.get("runs.path", "data/runs"),
            max_parallel=int(settings.get("execution.max_parallel_workers", 5)),
            site_memory=site_memory,
            llm=llm,
            adaptive_executor=adaptive_executor,
        )

    # FastAPI app (still useful for the dashboard-ui frontend)
    app = create_app(
        engine=engine,
        accounts=accounts,
        browser=browser,
        brain=brain,
        workflow_engine=workflow_engine,
        agent=agent,
        workflows_dir=settings.get("workflows_dir", "config/workflows"),
        enable_dashboard=bool(settings.get("dashboard.enabled", True)),
    )

    await engine.start()

    # Telegram-first command center (the *primary* user surface)
    command_center: CommandCenter | None = CommandCenter.from_settings(
        settings,
        agent=agent,
        accounts=accounts,
        browser=browser,
        templates=templates,
        site_memory=site_memory,
        nl_planner=nl_planner,
        execution_templates=exec_template_store,
    )
    if command_center:
        await command_center.start()

    # Optional config + accounts watchers (hot reload)
    watcher_task: asyncio.Task | None = None
    if bool(settings.get("config_watch", True)):
        watcher_task = asyncio.create_task(
            config.watch(
                interval=float(settings.get("config_watch_interval", 2.0)),
            ),
            name="config-watch",
        )
    if accounts and bool(settings.get("accounts.watch", True)):
        accounts.start_watcher(
            interval=float(settings.get("accounts.watch_interval_seconds", 2.0)),
        )

    log.info("starting API on %s:%d", host, port)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info"),
    )
    try:
        await server.serve()
    finally:
        if command_center:
            await command_center.stop()
        if watcher_task:
            watcher_task.cancel()
        if accounts:
            await accounts.stop_watcher()
            accounts.close()
        if browser:
            await browser.stop()
        await engine.stop()


# ============================================================================
#  bot — Telegram-only mode (no FastAPI; lighter footprint)
# ============================================================================
async def _run_bot(master_path: str | None, env_path: str | None) -> None:
    settings = init_settings(master_path, env_path)
    setup_logging(
        log_dir=settings.get("logging.dir", "data/logs"),
        level=settings.get("logging.level", "INFO"),
        debug_logs=bool(settings.get("logging.debug", False)),
    )
    print(settings.render_startup_banner())

    from automation.accounts.manager import AccountManager
    from automation.agent.adaptive_executor import AdaptiveExecutor
    from automation.agent.agent import BrowserAgent
    from automation.agent.execution_template import ExecutionTemplateStore
    from automation.agent.nl_planner import NLPlanner
    from automation.agent.site_memory import SiteMemory
    from automation.agent.template_recorder import TemplateRecorder
    from automation.agent.template_replayer import TemplateReplayer
    from automation.agent.templates import TemplateStore
    from automation.ai.brain import AIBrain
    from automation.ai.llm import build_llm_from_settings
    from automation.browser.manager import BrowserConfig, BrowserManager
    from automation.controllers.telegram_command_center import CommandCenter

    legacy = settings.legacy_config_dict()
    accounts: AccountManager | None = None
    accounts_file = settings.get("accounts.source_file")
    if accounts_file and Path(accounts_file).exists():
        accounts = AccountManager(
            source_file=accounts_file,
            state_file=settings.get(
                "accounts.state_file", "data/state/accounts.sqlite",
            ),
        )
        accounts.load()

    browser: BrowserManager | None = None
    if settings.get("playwright.enabled", True) is not False:
        browser = BrowserManager(BrowserConfig.from_dict(legacy))

    brain = AIBrain.from_config(legacy) if settings.get("ai.enabled", True) else None
    llm = build_llm_from_settings(settings)
    nl_planner = NLPlanner(llm=llm)
    templates = TemplateStore(
        root=settings.get("templates.dir", "data/templates"),
    )
    site_memory = SiteMemory(
        root=settings.get("memory.site_memory_dir", "data/learning/sites"),
    )
    exec_template_store = ExecutionTemplateStore(
        root=settings.get(
            "memory.execution_templates_dir", "data/execution_templates",
        ),
    )
    adaptive_executor = AdaptiveExecutor(
        store=exec_template_store,
        replayer=TemplateReplayer(),
        recorder=TemplateRecorder(exec_template_store),
    )

    agent: BrowserAgent | None = None
    if brain:
        agent = BrowserAgent(
            brain=brain,
            browser=browser,
            accounts_manager=accounts,
            runs_root=settings.get("runs.path", "data/runs"),
            max_parallel=int(settings.get("execution.max_parallel_workers", 5)),
            site_memory=site_memory,
            llm=llm,
            adaptive_executor=adaptive_executor,
        )

    cc = CommandCenter.from_settings(
        settings,
        agent=agent,
        accounts=accounts,
        browser=browser,
        templates=templates,
        site_memory=site_memory,
        nl_planner=nl_planner,
        execution_templates=exec_template_store,
    )
    if not cc:
        log.error("TELEGRAM_BOT_TOKEN not configured. Edit .env and try again.")
        return
    await cc.start()
    log.info("Telegram bot is running. Press Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await cc.stop()
        if browser:
            await browser.stop()
        if accounts:
            accounts.close()


# ============================================================================
#  worker
# ============================================================================
async def _run_worker(coordinator_url: str, capabilities: list[str]) -> None:
    settings = init_settings()
    setup_logging(
        log_dir=settings.get("logging.dir", "data/logs"),
        level=settings.get("logging.level", "INFO"),
    )
    from automation.distributed.worker import WorkerNode

    token = settings.get_secret("AUTOMATION_API_TOKEN")
    worker = WorkerNode(
        coordinator_url=coordinator_url, api_token=token,
        capabilities=capabilities,
    )

    async def handler(assignment: dict) -> dict:
        log.info(
            "worker handling assignment id=%s kind=%s",
            assignment.get("id"), assignment.get("kind"),
        )
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


# ============================================================================
#  root
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="automation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_server = sub.add_parser("server", help="run full stack: API + agent + Telegram")
    p_server.add_argument(
        "--config",
        default=os.environ.get("AUTOMATION_CONFIG"),
        help="path to master_config.json (defaults to config/master_config.json)",
    )
    p_server.add_argument(
        "--env",
        default=os.environ.get("AUTOMATION_ENV_FILE", ".env"),
        help="path to .env (default: .env)",
    )
    p_server.add_argument("--host", default=os.environ.get("AUTOMATION_HOST", "0.0.0.0"))
    p_server.add_argument(
        "--port", type=int, default=int(os.environ.get("AUTOMATION_PORT", "8080")),
    )

    p_bot = sub.add_parser("bot", help="run Telegram bot only (no API server)")
    p_bot.add_argument(
        "--config", default=os.environ.get("AUTOMATION_CONFIG"),
    )
    p_bot.add_argument(
        "--env", default=os.environ.get("AUTOMATION_ENV_FILE", ".env"),
    )

    p_doctor = sub.add_parser("doctor", help="validate configuration and exit")
    p_doctor.add_argument("--config", default=os.environ.get("AUTOMATION_CONFIG"))
    p_doctor.add_argument(
        "--env", default=os.environ.get("AUTOMATION_ENV_FILE", ".env"),
    )

    p_worker = sub.add_parser("worker", help="run a standalone worker node")
    p_worker.add_argument(
        "--coordinator-url",
        default=os.environ.get("AUTOMATION_API_URL", "http://127.0.0.1:8080"),
    )
    p_worker.add_argument("--capabilities", nargs="*", default=[])

    p_cli = sub.add_parser("cli", help="run the CLI client")
    p_cli.add_argument("args", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "server":
            asyncio.run(_run_server(args.config, args.env, args.host, args.port))
            return 0
        if args.cmd == "bot":
            asyncio.run(_run_bot(args.config, args.env))
            return 0
        if args.cmd == "doctor":
            return _run_doctor(args.config, args.env)
        if args.cmd == "worker":
            asyncio.run(_run_worker(args.coordinator_url, args.capabilities))
            return 0
        if args.cmd == "cli":
            from automation.controllers.cli import main as cli_main
            return cli_main(args.args)
    except KeyboardInterrupt:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
