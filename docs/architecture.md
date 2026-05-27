# Architecture

The framework is built around a small, async-first **engine** that
orchestrates loosely coupled subsystems through an **event bus**. A single
**AI Brain** is the only component that performs cognition; everything else
is mechanical.

```
                +----------------------------------+
                |          FastAPI API             |
                |   /status /control /plugins ...  |
                +----------------+-----------------+
                                 |
            +--------------------+----------------------+
            |                    |                      |
       +----v----+        +------v--------+      +------v------+
       | Engine  |  <-->  |  Event Bus    | <--> |   Plugins   |
       |         |        |  (pub/sub)    |      |             |
       +-+-------+        +---------------+      +-------------+
         |
   +-----+-----+----------+-----------+--------+-----------+
   |           |          |           |        |           |
+--v--+   +----v----+ +---v----+ +----v---+ +--v--+   +---v---+
|Sched|   | TaskMgr | | Queue  | | State  | |Watch|   | Health|
+-----+   +---------+ +--------+ +--------+ +-----+   +-------+

Optional subsystems (each fully isolated):

  AI Brain        -- single coordinator: perception, plan, act, heal, learn
  BrowserManager  -- Playwright, per-account isolated profiles
  AccountManager  -- JSON-loaded accounts with persistent state
  WorkflowEngine  -- declarative steps (navigate / analyze / ai_goal / ...)
  Coordinator     -- multi-node work distribution
  TelegramBot     -- relays /commands to the API
```

## Why a single Brain?

Multi-agent systems add coordination overhead, surface conflicting state,
and make decisions hard to inspect. A single Brain keeps reasoning linear:
it perceives, decides, acts, heals, and learns in one bounded loop. Plugins
remain numerous and isolated — only **cognition** is centralized.

## Lifecycle

1. **Boot.** `__main__.py` builds a `ConfigManager`, configures logging,
   constructs the `Engine` and optional subsystems, and starts the FastAPI
   server.
2. **Engine.start.** Discovers and starts plugins, starts scheduler,
   watchdog, signal handlers; emits `engine.started`.
3. **Hot reload.** A polling watcher sees config changes, calls
   `Engine.reload`, which re-reads config, re-applies workers/intervals,
   and runs `on_reload` for every plugin.
4. **Graceful shutdown.** SIGTERM/SIGINT triggers `Engine.stop`, which
   stops plugins (in order), scheduler, watchdog, and task manager. Each
   `stop` is exception-isolated.

## Exception isolation

- The event bus catches handler exceptions per subscriber.
- The task manager retries up to `max_retries` and emits `task.error` /
  `task.failed` events.
- The plugin loader wraps each plugin lifecycle hook in try/except.
- The watchdog monitors components and triggers bounded recovery.
- The browser manager marks unhealthy sessions and recreates them.
- The AI healer retries with alternate selectors and learned recoveries.

## Storage

| Data | Location | Format |
|---|---|---|
| Activity / error / debug logs | `data/logs/` | rotating JSON lines |
| Engine state | `data/state/state.json` | JSON |
| Account status | `data/state/accounts.json` | JSON |
| Browser profiles | `data/profiles/<account_id>` | Chromium profile dir |
| Screenshots | `data/screenshots/` | PNG |
| AI memory | `data/learning/memory.sqlite` | SQLite |

## Security

- API requires `AUTOMATION_API_TOKEN` (`Bearer` or `X-API-Token`). When
  unset, API runs in **open** mode and emits a startup warning.
- Telegram bot enforces an allow-list of chat IDs.
- Config view route redacts any key whose name contains `password`,
  `token`, `secret`, `api_key`, or `telegram_token`.
- All control actions are recorded in an in-memory audit log, exposed at
  `/control/audit`.
