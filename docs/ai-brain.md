# The AI Brain

A single coordinator, not a multi-agent system. Lives in
`src/automation/ai/`.

```
                    +---------------------+
                    |      AIBrain        |
                    |   single instance   |
                    +----------+----------+
                               |
        +----------------------+----------------------+
        |          |           |          |          |
   +----v---+ +----v---+  +----v---+ +----v----+ +---v----+
   |Percept-|| Planner |  |Executor| | Healer  | | Memory |
   | ion    ||         |  |        | |         | |(SQLite)|
   +--------+ +---------+  +--------+ +---------+ +--------+
```

## Pipeline

`brain.run(page, goal, inputs)` is the public entry point and runs:

1. **Perception** — `page.evaluate` extracts every interactive element with
   tag, role, text, ARIA, placeholder, name, bounding box. The
   `IntentMatcher` scores each element against the intent dictionary
   (`register`, `login`, `password_field`, ...).
2. **Planning** — Goal templates map to ordered slots
   (e.g. `login = [username|email, password, submit|login]`). For each
   slot, the highest-scoring matching element is picked and turned into an
   `ActionStep`. Memory hints can override selectors when confidence ≥ 0.7.
3. **Execution** — Steps run against the page with isolation. A failed
   non-optional step halts the plan; optional steps are best-effort.
4. **Healing** — On failure: reinspection, memory recovery strategies,
   role-based fallbacks, and alternate selector retries. The healer is
   bounded by `max_heal_attempts`.
5. **Learning** — Successful selectors and recovery strategies are
   persisted to `data/learning/memory.sqlite`. Every workflow run is
   recorded with success/failure and duration.

## Memory schema

| Table | What |
|---|---|
| `learned_selectors` | (page_sig, intent) -> best selectors with confidence |
| `workflow_runs` | per-run success / duration |
| `recovery_strategies` | (page_sig, failure_kind) -> what worked |
| `page_patterns` | seen page signatures, titles, URL patterns |

The `page_signature` is a hash of normalized URL + title + role/text
fingerprint. It's stable across cosmetic changes but changes when the
page structure changes — exactly the right granularity for selector
memory.

## Optional / pluggable

- Set `ai.enabled = false` in config to disable the brain entirely. The
  framework still boots and the workflow engine still runs non-AI steps.
- Set `ai.dry_run = true` to plan without acting (useful for review).
- `ai.memory_enabled = false` runs the brain stateless.

## Inspecting decisions

```bash
automation-cli ai status      # last decision summary
automation-cli ai pages       # known page signatures
automation-cli ai stats       # workflow success rates
```

Or in the dashboard: the **AI Brain** card shows the last decision and
overall success rate.

## Extending

- Add intents in `automation/ai/intents.py` (or override at runtime by
  passing your own `IntentMatcher` to `AIBrain`).
- Add goal templates in `automation/ai/planner.py`.
- Replace `AIMemory` with a Postgres-backed implementation by subclassing
  and providing the same async surface.
