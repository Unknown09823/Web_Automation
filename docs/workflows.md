# Workflows

A workflow is a list of typed steps in JSON or YAML. The engine runs them
sequentially against a Playwright page (and an `AIBrain` if available).

## Step types

| Type | Purpose |
|---|---|
| `navigate` | Go to a URL |
| `wait` | Wait for selector / load state / fixed seconds |
| `analyze` | Have the brain perceive the page (stores `_snapshot`) |
| `find` | Locate an element by intent (stores under `_found.<key>`) |
| `act` | `click` / `fill` / `select` / `check` / `press` / `type` |
| `ai_goal` | Hand the brain a high-level goal to plan + execute |
| `screenshot` | Capture the page |
| `verify` | Assert URL / title / element visibility |
| `branch` | Conditional jump within the workflow |
| `set` | Bind variables in the workflow context |
| `log` | Emit a log line |

## Variables

Use `${var}` or `${nested.key}` to interpolate context values:

```yaml
- type: navigate
  params:
    url: "${target_url}"
```

Common context keys:

- Inputs you pass to `/workflows/<name>/run`
- Account fields under `${account.username}` etc. (when `account_id` is
  supplied)
- `_snapshot` set by `analyze`
- `_found.<key>` set by `find`

## Conditions

`if:` on a step or `branch.if` accepts a tiny safe DSL:

```yaml
- type: branch
  params:
    if: "_snapshot.title == 'Welcome'"
    goto: 5
```

Supported: `==`, `!=`, and bare-truth checks.

## Errors and retries

```yaml
- type: act
  params: {action: click, selector: "#go"}
  retries: 2
  on_error: continue   # fail | continue | retry
  timeout_ms: 15000
```

## Example: AI-driven login

```json
{
  "name": "example_login",
  "steps": [
    {"type": "navigate", "params": {"url": "${target_url}"}},
    {"type": "wait", "params": {"load_state": "domcontentloaded"}},
    {
      "type": "ai_goal",
      "params": {
        "goal": "login",
        "inputs": {
          "username": "${account.username}",
          "password": "${account.password}"
        }
      }
    },
    {"type": "screenshot", "params": {"path": "data/screenshots/login-${ts}.png"}}
  ]
}
```

## Running

```bash
# from a file
automation-cli workflow run example_login.json \
    --account user001 \
    --inputs '{"target_url":"https://example.test"}'

# inline via API
curl -X POST -H "Authorization: Bearer $AUTOMATION_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name":"adhoc","steps":[{"type":"log","params":{"message":"hi"}}]}' \
     http://127.0.0.1:8080/workflows/run_inline
```

## What the brain does in `ai_goal`

1. Perceives the current page (DOM + ARIA + text + role).
2. Maps each visible element to **intents** (`login`, `register`,
   `password_field`, etc.) using the semantic matcher (no CSS selectors
   required).
3. Picks elements for the goal's slots (templates in
   `automation/ai/planner.py`).
4. Executes the resulting `ActionPlan`.
5. On failure, the healer reinspects, tries memory selectors, role-based
   queries, and alternate matches.
6. Records every outcome to `data/learning/memory.sqlite` so future runs
   are faster.
