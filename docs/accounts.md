# Accounts

The framework loads test accounts from a JSON file you control. The AI
Brain **never generates accounts** — it only reads, validates, locks,
runs, and records results.

> Looking to test repeat-registration / same-device detection on your
> own site? See [`identity-testing.md`](identity-testing.md). It
> describes how to use `account.metadata` to pin a proxy or share a
> browser profile across accounts.

## Source format

Both shapes are accepted. The recommended envelope is:

```json
{
  "accounts": [
    {"number": "9999999999", "password": "pass1"},
    {"number": "8888888888", "password": "pass2"}
  ]
}
```

A bare list (legacy) also works:

```json
[
  {"id": "u1", "username": "tester1", "password": "pass1"},
  {"id": "u2", "email": "t2@example.test", "password": "pass2"}
]
```

Per-row fields:

| Field | Required | Description |
|---|---|---|
| `id` | optional | Stable identifier. If omitted, derived from `number`/`username`/`email`. |
| `number` | recommended | Phone number; primary identifier in the new envelope. |
| `username` / `email` | optional | Alternative identifiers. |
| `password` | yes (default) | Disabled rows without a password are rejected. Toggle via `accounts_require_password`. |
| `metadata` | optional | Free-form tags / groups for filtering / reporting. |

## Validation

Each row is validated. Rejections go to a `rejected` table (visible at
`GET /accounts/rejected`) and never enter the work queue:

- not an object
- no usable identifier (`id` / `number` / `username` / `email`)
- missing or empty password (when required)
- invalid phone-number format
- duplicate identifier inside the same source file

## Lifecycle and statuses

```
                 +----------+
   load -->      | pending  |  --- claim_next() --->  running
                 +----------+                          |
                      ^                                v
                      |                              success?
                  retry (< max_attempts)             /     \
                      |                            yes      no
                      |                             |        |
              mark_failed (attempts < max)          v        v
                      |                        completed   pending (retry)
                      |                                       or
                      |                                     failed (when
                      +-------------------------------------- attempts >= max)
```

Statuses: `pending`, `running`, `completed`, `failed`, `skipped`,
`paused`. Counts are exposed at `GET /accounts` and `GET /accounts/status`.

## Locking

Workers call `claim_next(worker_id)`. The store atomically picks one
`pending` row, sets `status='running'`, increments `attempts`, and
records `locked_by` + `locked_at`. Two concurrent calls **never** receive
the same account — verified by the `test_account_manager_concurrent_claims`
test.

Locks are **lease-based**. If a worker crashes without releasing, the
account becomes claimable again after `accounts_lease_seconds` (default
600s). You can also call `POST /accounts/locks/reap` to clear expired
locks immediately.

## Crash recovery

State lives in SQLite (`accounts_state`, default
`data/state/accounts.sqlite`). After an EC2 reboot, the manager:

1. Opens the SQLite database — every `completed` / `failed` row is
   already there.
2. Re-reads the source JSON. Existing rows are upserted *preserving
   runtime state* (status, attempts, checkpoint).
3. Calls `reset_stuck_locks()` so any account locked by a dead worker
   becomes claimable again.

No completed account is ever re-run.

## Hot reload

Changing `accounts.json` while the framework is running is detected
within a few seconds. The watcher calls `AccountManager.reload()`, which
re-validates and re-upserts. No restart needed; in-flight workers are
unaffected.

You can disable the watcher in config (`accounts_watch: false`) and
trigger reloads manually:

```bash
automation-cli accounts reload
# or
curl -X POST -H "Authorization: Bearer $AUTOMATION_API_TOKEN" \
     http://127.0.0.1:8080/accounts/reload
```

## Result history

Every workflow run is recorded in `account_results` with start time, end
time, duration, success flag, and error message. View it via:

```bash
automation-cli accounts results --limit 50
automation-cli accounts results <account_id> --limit 20
```

The dashboard derives `processing_speed` (accounts/min over the last 60
seconds) directly from this table.

## REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/accounts` | List accounts with optional `?status_filter=` |
| GET | `/accounts/status` | Aggregate snapshot for monitoring |
| GET | `/accounts/completed` | List completed accounts |
| GET | `/accounts/failed` | List failed accounts |
| GET | `/accounts/rejected` | Validation rejections |
| GET | `/accounts/results` | Per-run history (optional `?account_id=`) |
| GET | `/accounts/{id}` | Single account |
| POST | `/accounts/reload` | Re-read source JSON |
| POST | `/accounts/locks/reap` | Force-clear expired locks |
| POST | `/accounts/{id}/reset` | Reset to `pending` |
| POST | `/accounts/{id}/pause` | Move to `paused` |
| POST | `/accounts/{id}/resume` | Move back to `pending` |
| POST | `/accounts/{id}/release` | Drop lock without changing status |

## CLI

```bash
automation-cli accounts status
automation-cli accounts list --status pending
automation-cli accounts completed --limit 100
automation-cli accounts failed --limit 100
automation-cli accounts rejected
automation-cli accounts results <account_id>
automation-cli accounts reload
automation-cli accounts reap
automation-cli accounts release <account_id>
```

## Telegram

```
/accounts             status snapshot
/completed [N]        last N completed
/failed [N]           last N failed
/rejected             validation failures
/reload_accounts      hot reload accounts.json
```
