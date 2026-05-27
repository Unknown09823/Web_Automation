# Automation Framework

A production-grade, plugin-driven Python automation platform with a single
central **AI Brain**. Designed for AWS EC2 (Ubuntu) and remote control from
Termux / SSH / Telegram / a web dashboard.

> **Important.** This framework only includes generic infrastructure. It
> contains no website-specific selectors, URLs, business logic, or reward
> systems. All targets, accounts, and workflows are loaded from
> configuration files you control. Use it only on systems you own or are
> explicitly authorized to test.

## Highlights

- **Single AI Brain** — one coordinator handles perception, planning,
  execution, healing, and learning. No multi-agent fan-out.
- **Plugin engine** — drop a Python file into `plugins_external/`, the
  loader auto-discovers and lifecycles it. Plugin crashes never take down
  the framework.
- **Central FastAPI control plane** — every other channel (CLI, Telegram,
  dashboard, workers) is a client of the same JSON API.
- **Browser orchestration** — Playwright-based per-account profiles,
  isolated cookies/storage, headless or headed, automatic recovery.
- **Account manager** — JSON-loaded test accounts with persistent state,
  retries, and checkpoints.
- **Declarative workflows** — JSON / YAML steps with an `ai_goal` step
  that delegates to the brain. No code changes to add new flows.
- **AI memory** — SQLite-backed learning of selectors, recovery
  strategies, and page patterns; survives restarts.
- **Self-healing** — failed steps trigger reinspection, role-based
  fallbacks, and memory-driven recovery before giving up.
- **Distributed mode** — coordinator + worker nodes for scaling across
  multiple EC2 instances.
- **Production deployment** — Docker, docker-compose, systemd, Nginx
  reverse proxy, DuckDNS helper, install script.

## Project layout

```
Web_Automation/
├── src/automation/
│   ├── core/            engine, event bus, scheduler, task manager,
│   │                    queue, state, watchdog, health monitor
│   ├── plugins/         plugin base, loader, registry
│   ├── ai/              brain, intents, perception, planner, executor,
│   │                    healer, memory
│   ├── browser/         Playwright manager with per-account profiles
│   ├── accounts/        JSON account manager with persistent state
│   ├── controllers/     workflow engine, CLI, Telegram bot
│   ├── api/             FastAPI app + routes (status, control, plugins,
│   │                    accounts, workflows, AI, distributed, ...)
│   ├── distributed/     coordinator + worker for multi-node deployments
│   ├── dashboard/       static HTML/JS dashboard
│   ├── config/          configuration manager (JSON/YAML, hot reload)
│   ├── logging_system/  structured rotating logs
│   └── utils/           security helpers
├── plugins_external/    drop-in plugins (heartbeat, metrics, example_task)
├── config/              config.json, accounts.json, workflows/
├── deploy/              docker, systemd, nginx, install scripts
├── docs/                detailed guides
└── tests/
```

## Quick start (local)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
playwright install chromium

cp .env.example .env       # set AUTOMATION_API_TOKEN
python -m automation server --config config/config.json
# open http://127.0.0.1:8080/dashboard
```

## Documentation

| Guide | What |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Design overview |
| [`docs/installation.md`](docs/installation.md) | Local setup |
| [`docs/ec2-deployment.md`](docs/ec2-deployment.md) | EC2 + Nginx + DuckDNS |
| [`docs/termux-control.md`](docs/termux-control.md) | Phone control |
| [`docs/plugins.md`](docs/plugins.md) | Writing a plugin |
| [`docs/workflows.md`](docs/workflows.md) | Workflow grammar |
| [`docs/accounts.md`](docs/accounts.md) | Account format, validation, locking, hot reload |
| [`docs/ai-brain.md`](docs/ai-brain.md) | AI internals & memory |

## License

MIT.
