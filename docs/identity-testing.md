# Identity Testing

How to test whether your site detects repeat registrations, same-device
abuse, or shared-network signups — without spoofing anything at the
network layer.

> **Use this only on systems you own or are authorized to test.** This
> guide and the framework are designed for testing a target you control.
> The framework will not forge IP packets, evade rate limits, or defeat
> production fraud-detection on third-party sites.

## What is — and isn't — supported

| Feature | Supported | Why / Why not |
|---|---|---|
| Per-account HTTP / SOCKS proxy *you* control | Yes | Standard Playwright proxy config. You bring proxies you legitimately own or have paid for. |
| Per-account browser fingerprint variation (UA, viewport, locale, timezone, geolocation, headers) | Yes | These are normal Playwright context options. Real users vary too. |
| Shared browser profile across accounts (same-device test) | Yes | Multiple accounts can target the same persistent profile dir. |
| Sequential matrix runner | Yes | Required for shared-profile tests because Chromium locks the profile dir. |
| **IP-packet source-address spoofing** | **No** | Forges traffic on networks you don't own. Affects upstream NAT/ISPs regardless of intent. |
| Anti-detect / canvas-fingerprint randomizers aimed at evading fraud detection | No | Tooling whose stated purpose is to defeat detection crosses into abuse infrastructure. |
| Bypassing rate limits, captchas, geo-blocks | No | Same reason. |

## The 4-cell matrix

When a site says "we detected the same user," it's relying on at least
one of: **device fingerprint** (browser profile + UA + viewport + …) and
**network identity** (IP / ASN / proxy). Run the matrix below and watch
which cells the site flags:

|                       | **same proxy**         | **different proxy**    |
|-----------------------|------------------------|------------------------|
| **same profile**      | should be detected     | tells you whether device fp alone is enough |
| **different profile** | tells you whether IP alone is enough | should succeed (clean baseline) |

Five accounts cover the matrix plus a baseline.

## Setup

### 1. Provide proxies you control

Edit `config/accounts.detection-matrix.example.json`, replace the
placeholder URLs (`http://user:pass@proxy-Y.example:8080`) with proxies
you own or have paid for, then save as `config/accounts.json` (or point
`accounts_file` at a custom path in `config.json`).

The proxy URL goes into `metadata.proxy`. Both `http://` and `socks5://`
schemes are supported. Embedded credentials are parsed and passed to
Playwright as separate `username` / `password` fields.

```json
{
  "number": "5550000001",
  "password": "...",
  "metadata": {
    "profile_id": "device-X",
    "proxy": "http://user:pass@proxy-Y.example:8080",
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64) ...",
    "viewport": {"width": 1366, "height": 768},
    "locale": "en-US",
    "timezone_id": "America/New_York"
  }
}
```

### 2. Recognized `metadata` fields

All optional. Anything missing falls back to the framework defaults from
`config.json`'s `playwright` block.

| Key | Effect |
|---|---|
| `profile_id` | Persistent profile directory key. Two accounts with the same value share cookies, storage, cache, IndexedDB, and the Chromium-level fingerprint that comes from a real profile. Defaults to `account.id`. |
| `proxy` | Proxy URL or Playwright proxy dict. Applied to all traffic from that session. |
| `user_agent` | Override `User-Agent`. |
| `viewport` | `{"width": N, "height": N}` or `[N, N]`. |
| `locale` | e.g. `"en-US"`, `"fr-FR"`. Affects `Accept-Language` and `navigator.language`. |
| `timezone_id` | IANA zone, e.g. `"America/New_York"`. |
| `geolocation` | `{"latitude": ..., "longitude": ..., "accuracy": ...}` (requires the `permissions` entry below). |
| `permissions` | List of context permissions, e.g. `["geolocation"]`. |
| `extra_http_headers` | Dict of headers added to every request. |
| `color_scheme` | `"light"` or `"dark"`. |
| `device_scale_factor` | Float, e.g. `2.0` for HiDPI. |

Unknown keys are ignored, so plugins / workflows can store other data in
the same dict without confusing the browser layer.

### 3. Run the probe

The probe workflow `config/workflows/identity_test_register.yaml`
navigates to your target, lets the AI brain perform the registration,
then captures screenshots and (optionally) checks for known
duplicate-detection signals.

```bash
automation-cli workflow batch identity_test_register.yaml \
    --accounts probe-a,probe-b,probe-c,probe-d,probe-e \
    --inputs '{"target_url": "https://your-domain.duckdns.org/signup"}'
```

Sequential is the default (and required for shared-profile cells).

To customize the success / failure detection for your site, pass these
inputs:

| Input | Meaning |
|---|---|
| `target_url` | Where to navigate first. |
| `success_url_part` | Substring of the URL after a successful signup (e.g. `"/welcome"`). Skipped if empty. |
| `duplicate_selector` | A CSS selector unique to your site's "already registered" / "blocked" UI. Skipped if empty. The probe waits up to 4s for it. |

Example:

```bash
automation-cli workflow batch identity_test_register.yaml \
    --accounts probe-a,probe-b,probe-c,probe-d,probe-e \
    --inputs '{
      "target_url": "https://your-domain.duckdns.org/signup",
      "success_url_part": "/welcome",
      "duplicate_selector": ".error-banner.duplicate"
    }'
```

### 4. Read the results

```bash
automation-cli accounts results --limit 20
automation-cli workflow results
```

The dashboard (`/dashboard`) shows the same data; the **Recent Workflow
Runs** card lists each probe with its status and step count, and each
account's row in **Accounts** carries `last_error` for failed cells.

The screenshots live under `data/screenshots/probe-<account_id>-pre.png`
and `-post.png` so you can eyeball each cell.

## Interpreting the matrix

Read the five outcomes together, not in isolation:

- **probe-a** is the baseline — it should succeed.
- **probe-b** is the unambiguous repeat: same device, same network,
  different account identifier. If your site **doesn't** detect this,
  you have a gap in repeat-registration defenses.
- **probe-c** isolates the **device** signal. If c is detected but d
  isn't, the site relies on device fingerprint (profile cookies, UA,
  fingerprintable surface).
- **probe-d** isolates the **network** signal. If d is detected but c
  isn't, the site relies on IP / ASN.
- **probe-e** is a clean baseline — it should succeed. If it doesn't,
  something in your funnel is rejecting valid signups.

A common, healthy result is: **a, e succeed; b is blocked; c and d are
both blocked or rate-limited**. That means your site uses both signals.

## Sequential vs parallel

The matrix runner is sequential by default. Switch to parallel only when
**every** account in the batch has a distinct `profile_id` — Chromium
will refuse to open the same persistent profile dir twice at the same
time.

```bash
# Safe to parallelize — every account has its own profile_id
automation-cli workflow batch identity_test_register.yaml \
    --accounts probe-a,probe-e \
    --parallel --max-parallel 2 \
    --inputs '{"target_url": "https://..."}'
```

The framework auto-evicts a previous session that holds a profile dir
when a new account requests the same dir, so sequential matrix runs work
without manual cleanup.

## API reference

```http
POST /workflows/{name}/run_for_accounts
Content-Type: application/json
Authorization: Bearer $AUTOMATION_API_TOKEN

{
  "account_ids": ["probe-a", "probe-b", "probe-c", "probe-d", "probe-e"],
  "inputs": {"target_url": "https://your-domain.duckdns.org/signup"},
  "parallel": false,
  "max_parallel": 1,
  "stop_on_failure": false
}
```

Response:

```json
{
  "status": "started",
  "workflow": "identity_test_register.yaml",
  "accounts": ["probe-a", "probe-b", ...],
  "parallel": false,
  "max_parallel": 1,
  "stop_on_failure": false
}
```

The batch runs in the background. A `kind: "batch"` summary is appended
to `/workflows/results/recent` when it finishes, with per-account
`status`, `profile_id`, `proxy`, `duration_ms`, and `error`.

## Limitations

- **No IP spoofing.** Use real proxies. The framework will not forge
  packet source addresses.
- **No CAPTCHA solving.** If your registration flow has a CAPTCHA, the
  probe will fail at that step. Disable the CAPTCHA on a staging
  environment for the duration of the test.
- **Site-specific success criteria.** The probe ships with generic
  hooks (`success_url_part`, `duplicate_selector`). Real sites vary —
  customize via inputs or fork the workflow.
- **Profile hashing isn't bulletproof.** Sharing a `profile_id` makes
  cookies, storage, and the fingerprint of a *real Chromium profile*
  shared. It does **not** force every JS-detected fingerprint surface
  to match — sites that fingerprint canvas/audio at the JS layer will
  see the same Chromium build but small variations are normal.
