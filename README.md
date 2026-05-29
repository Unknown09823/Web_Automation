# Autonomous Browser Agent — Telegram-First

A production-grade autonomous browser agent that you control entirely from
**Telegram**. Send a free-form instruction such as

> *Create 5 accounts on https://example.com with password Test@123, login,
> then download the latest report.*

…and the agent plans, executes, recovers, and reports back — all from a
chat window. No JSON, no YAML, no SSH, no CLI required after the initial
deployment.

> **Important.** The framework contains no website-specific selectors,
> URLs, business logic, or reward systems. All targets, accounts, and
> workflows come from configuration you control. Use it only on systems
> you own or are explicitly authorized to test.


## Why Telegram-first?

Until now, operating the framework meant editing four different files:
`.env` for some secrets, `config/config.json` for others, account JSON,
workflow YAML — plus running CLI scripts over SSH to start anything. The
new control surface unifies all of that:

| Old way | New way |
|---|---|
| Edit `config/config.json` | One non-secret file: `config/master_config.json` |
| Tokens scattered across `.env`, `config.json`, dashboard | All secrets in `.env` only |
| Hand-write `workflows/*.yaml` | Just send the instruction in Telegram |
| `python -m automation cli ...` over SSH | Slash commands or chat |
| Tail logs to see progress | `/watch <run_id>` streams live updates |
| Restart from scratch on failure | `/resume <run_id>` from last checkpoint |


## What's inside

**Goal-based execution engine.** The agent doesn't run *steps*, it pursues
*goals* (`register_account`, `login`, `download_file`, `complete_onboarding`,
…). For each goal it perceives the page, plans actions, executes them with
adaptive waits, verifies success against multiple signals (URL change,
success message, hint match, page health), and only then checkpoints.

**Smart waiting.** A DOM-mutation + network-activity + loading-indicator
observer replaces every `sleep(...)` in the framework. Pages move forward
the instant they're actually ready.

**Supervisor with stacked recovery.** When a goal fails the agent tries:
immediate retry → wait-and-retry → dismiss overlay → scroll into view →
AI replan, then surfaces the failure. With `GROQ_API_KEY` set, the
supervisor uses Qwen reasoning models to brainstorm a fresh recovery plan.

**Per-run memory folder.** Every run writes
`data/runs/<run_id>/{plan.json, status.json, events.jsonl, <account>/
{screenshots, html, cookies, logs, memory.json, replay.json,
checkpoints.json}}`. A failure becomes a checkpoint, and `/resume` picks
up from the last successful goal.

**Self-learning per-site memory.** After every successful goal, the agent
appends to `data/learning/sites/<domain>.json`: known-good selectors,
dashboard URLs, average duration per workflow, success/failure ratio.
Future runs against the same site get faster and more reliable.

**Adaptive Execution Mode (token optimisation).** Account #1 runs in full
AI mode and the framework records the *exact* sequence of clicks, fills,
and intelligent waits that worked into
`data/execution_templates/<domain>/<workflow>_v<n>.json`. Accounts #2..N
**replay that template deterministically — no LLM calls at all**. Confidence
decays only when a real replay fails, at which point the agent
automatically falls back to AI, recovers, and records a new version.
Result: ~1 LLM call per (domain, workflow) instead of per (account, goal).
Use `/templates_exec` from Telegram to inspect, and `/relearn <domain>`
to force a fresh AI pass.

**Templates.** Save a flow once with `/template_save RegisterAndLogin <text>`
and re-run it with `/run RegisterAndLogin`.

**Single LLM, multiple providers.** Default backend is Groq (Qwen
`qwen/qwen3-32b` for planning, `qwen-qwq-32b` for reasoning). Same client
also speaks OpenAI / OpenRouter / any OpenAI-compatible endpoint by
swapping the API key. The LLM never drives the browser directly —
Playwright actions remain deterministic; the LLM produces *structured
JSON* the agent then executes.


## Quick start

```bash
git clone <this repo> && cd Web_Automation
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
playwright install chromium

# 1) Fill in only this file
cp .env.example .env
$EDITOR .env       # set TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS, GROQ_API_KEY

# 2) Validate (does not start the browser or bot)
python -m automation doctor

# 3) Start
python -m automation server      # full stack: API + agent + Telegram
# OR
python -m automation bot         # just the Telegram bot, no FastAPI
```

That's it. From Telegram now:

- Send any free-form instruction
- `/help` to see every command
- `/templates` to manage saved flows
- `/runs` to browse history, `/watch <run_id>` to stream a live one


## Telegram command reference

| Command | What |
|---|---|
| *(any free text)*                  | NL → plan → preview → confirm → run |
| `/newtask <text>`                  | Same as sending free text |
| `/run <template>`                  | Run a saved template |
| `/batch`                           | Wizard for bulk runs (accounts, parallel, task body) |
| `/status`                          | Active runs |
| `/runs [N]`                        | Recent runs (default 10) |
| `/stop <run_id>` / `/cancel`       | Cancel a running task |
| `/resume <run_id>`                 | Resume from the last checkpoint |
| `/replay <run_id> <account_id>`    | Show recorded actions |
| `/watch <run_id>`                  | Live progress board, edited in place |
| `/templates`                       | List templates |
| `/template_save <name> <text>`     | Save a template (NL) |
| `/template_get <name>`             | Show one |
| `/template_delete <name>`          | Delete one |
| `/accounts`                        | Account totals + last 10 |
| `/accounts_reload`                 | Reload accounts file |
| `/config`                          | Current config (secrets masked) |
| `/config_set <key> <value>`        | Update non-secret config; persists |
| `/memory [domain]`                 | Self-learning memory summary or per-site detail |
| `/templates_exec [domain]`         | Execution templates the AdaptiveExecutor records and replays (no LLM) |
| `/relearn <domain> [workflow]`     | Wipe replay templates so the next run re-engages the AI |
| `/workers`                         | Active browser sessions |
| `/help`                            | Full help |


## Configuration

**Two files. That's it.**

```
.env                          # secrets only
config/master_config.json     # everything else (browser, parallelism, …)
```

**`.env` (single source of truth for secrets):**

```bash
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321
GROQ_API_KEY=
AUTOMATION_API_TOKEN=
HEADLESS=true
MAX_PARALLEL_WORKERS=5
DEFAULT_PLANNER_MODEL=qwen/qwen3-32b
DEFAULT_REASONING_MODEL=qwen-qwq-32b
# …see .env.example for the full list
```

**`config/master_config.json`** holds non-secret defaults: viewport, runs
path, screenshot policy, verification threshold, AI provider, etc.
Everything in there can also be overridden by an `AUTOMATION__SECTION__KEY`
environment variable (e.g. `AUTOMATION__EXECUTION__MAX_RETRIES=5`).

The framework reads them in this precedence (later wins):
`master_config.json` → `.env` → `os.environ` → `AUTOMATION__…` overrides.

`python -m automation doctor` prints a startup report:

```
============================================================
 AUTOMATION FRAMEWORK STARTUP
============================================================
  [OK  ] Telegram       configured (chats=2)
  [OK  ] Groq           model=qwen/qwen3-32b
  [OK  ] OpenRouter     missing (optional)
  [OK  ] API auth       bearer token set
  [OK  ] Storage        runs at data/runs
  [OK  ] Browser        engine=chromium headless=True
  [OK  ] Templates      data/templates
  [OK  ] Site memory    data/learning/sites
============================================================
```


## Architecture

```
Telegram (chat)
    ↓ (long-poll, urllib)
CommandCenter             ← controllers/telegram_command_center.py
    ↓
NLPlanner (Groq Qwen)     ← agent/nl_planner.py + ai/llm/groq.py
    ↓
BrowserAgent              ← agent/agent.py  (the autonomous outer loop)
    ↓                       observe → plan → execute → verify → checkpoint
Browser workers           ← browser/manager.py  (Playwright, per-account profiles)
    ↓
Run folder + Checkpoints  ← data/runs/<run_id>/...
    ↓
Site memory               ← data/learning/sites/<domain>.json
```


## Project layout

```
Web_Automation/
├── .env.example                              ← all secrets live here
├── config/
│   ├── master_config.json                    ← all non-secret config
│   ├── accounts.example.json                 (account format reference)
│   └── workflows/                            (legacy declarative workflows)
├── src/automation/
│   ├── __main__.py                           ← server / bot / doctor / cli
│   ├── config/
│   │   ├── settings.py                       ← Settings + secret manager (NEW)
│   │   └── manager.py                        (legacy, still used)
│   ├── controllers/
│   │   ├── telegram_command_center.py        ← rich Telegram bot (NEW)
│   │   ├── telegram_bot.py                   (legacy read-only forwarder)
│   │   ├── workflow_engine.py
│   │   └── cli.py
│   ├── ai/
│   │   ├── brain.py                          (heuristic perception/plan/heal)
│   │   ├── llm/                              ← LLM clients (NEW)
│   │   │   ├── base.py                       (LLMBackend protocol + prompts)
│   │   │   ├── groq.py                       (Qwen on Groq, OpenAI-compatible)
│   │   │   └── factory.py                    (build_llm_from_settings)
│   │   ├── perception.py / planner.py / executor.py / healer.py / memory.py
│   ├── agent/
│   │   ├── agent.py                          (BrowserAgent — autonomous loop)
│   │   ├── waiter.py / verifier.py / recovery.py
│   │   ├── recorder.py / checkpoints.py / run.py
│   │   ├── nl_planner.py / data_factory.py / goals.py / events.py
│   │   ├── templates.py                      ← reusable templates (NEW)
│   │   ├── site_memory.py                    ← per-site self-learning (NEW)
│   │   ├── execution_template.py             ← deterministic replay templates (NEW)
│   │   ├── template_recorder.py              ← ledger → template (NEW)
│   │   ├── template_replayer.py              ← deterministic Page executor (NEW)
│   │   └── adaptive_executor.py              ← replay-first / AI-fallback (NEW)
│   ├── browser/manager.py
│   ├── accounts/manager.py + store.py
│   ├── api/ (FastAPI control plane — still served alongside Telegram)
│   └── core/, distributed/, plugins/, logging_system/, utils/
├── data/
│   ├── runs/<run_id>/...                     (per-run memory folder)
│   ├── templates/<name>.json                 (saved templates)
│   ├── execution_templates/<domain>/<wf>_vN.json  (replay templates)
│   ├── learning/sites/<domain>.json          (per-site memory)
│   ├── learning/memory.sqlite                (selector memory)
│   ├── state/, profiles/, screenshots/, downloads/, logs/
├── scripts/
│   ├── test_telegram_first.py                (regression suite for new layer)
│   ├── test_agent.py
│   └── test_smoke_subset.py
├── plugins_external/
├── deploy/  (docker, systemd, nginx, install scripts)
└── tests/
```


## Documentation

| Guide | What |
|---|---|
| [`docs/operator-guide.md`](docs/operator-guide.md) | EC2 setup, daily ops, monitoring |
| [`docs/architecture.md`](docs/architecture.md) | Internal design |
| [`docs/ai-brain.md`](docs/ai-brain.md) | AI internals & memory |
| [`docs/accounts.md`](docs/accounts.md) | Account format, validation, locking |
| [`docs/identity-testing.md`](docs/identity-testing.md) | Per-account proxy + fingerprint |
| [`docs/workflows.md`](docs/workflows.md) | Legacy workflow grammar (still supported) |
| [`docs/plugins.md`](docs/plugins.md) | Writing plugins |


## Tests

```bash
PYTHONPATH=src python3.12 scripts/test_adaptive_execution.py   # 69 checks (replay layer)
PYTHONPATH=src python3.12 scripts/test_telegram_first.py       # 59 checks
PYTHONPATH=src python3.12 scripts/test_smoke_subset.py         # 6 checks
PYTHONPATH=src python3.12 scripts/test_agent.py                # 20 checks
```


## License

MIT.
