# Operator Guide

Everything an operator needs to set up, run, monitor, and recover the
Automation Framework on AWS EC2. Read this end-to-end the first time;
keep it open as a runbook after that.

> Use only on systems you own or are explicitly authorized to test.
> No website-specific logic ships with the framework — accounts,
> targets, and workflows all come from the JSON files you provide.

---

## Cheat sheet

```bash
# State
sudo systemctl status automation
automation-cli status
automation-cli health

# Engine control
automation-cli start
automation-cli stop
automation-cli restart
automation-cli reload                       # apply config + plugin changes

# Accounts (after editing /opt/automation/config/accounts.json)
automation-cli accounts reload
automation-cli accounts status
automation-cli accounts completed --limit 50
automation-cli accounts failed --limit 50

# Run work
automation-cli workflow list
automation-cli workflow run example_login.json --account user001
automation-cli workflow batch identity_test_register.yaml \
    --accounts probe-a,probe-b,probe-c \
    --inputs '{"target_url":"https://your-domain.duckdns.org/signup"}'

# Logs
automation-cli logs activity --lines 200
automation-cli logs error --lines 100
sudo journalctl -u automation -f --no-pager

# Update framework after `git pull`
sudo systemctl restart automation
```

---

## 1. Before you start — checklist

You should have:

- An AWS account with permission to launch EC2 instances and configure
  security groups.
- A domain (DuckDNS works) you'll point at the EC2 instance for HTTPS.
- The repo URL for your fork of this framework
  (e.g. `https://github.com/<you>/Web_Automation.git`).
- (Optional) A Telegram bot token + your chat ID, if you want phone
  control. Talk to `@BotFather` on Telegram to create the bot, and
  `@userinfobot` to find your chat ID.
- (Optional) HTTP/SOCKS proxies you legitimately own or have paid for,
  if you'll run identity-testing workflows.
- An accounts JSON file ready to upload (see §6).

---

## 2. Provision the EC2 instance

| Setting | Recommended |
|---|---|
| OS | Ubuntu 22.04 LTS (or newer LTS) |
| Type | `t3.small` (2 vCPU, 2 GB RAM) for non-browser plugins; `t3.medium` (2 vCPU, 4 GB RAM) for typical browser workloads; bigger if you run many concurrent browser sessions |
| Storage | 20 GB gp3 minimum (Chromium + Playwright + browser profiles take space) |
| Public IP | Yes (Elastic IP recommended so DNS doesn't break on restart) |

**Security group inbound rules:**

| Port | Source | Purpose |
|---|---|---|
| 22/tcp | Your office / home IP **only** | SSH |
| 80/tcp | `0.0.0.0/0` | HTTP (only used for `certbot` and HTTP→HTTPS redirect) |
| 443/tcp | `0.0.0.0/0` | HTTPS (dashboard + API) |

Outbound rules: keep the default (allow all) unless your security team
says otherwise.

**SSH in**

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@<EC2-public-IP>
```

---

## 3. One-shot install

Either copy the project tree to `/opt/automation` and run the
installer, or set `REPO_URL` and let the installer clone for you.

```bash
# --- Option A: clone via REPO_URL ---
sudo apt-get update && sudo apt-get install -y git
sudo REPO_URL="https://github.com/<your-fork>/Web_Automation.git" \
    bash <(curl -fsSL https://raw.githubusercontent.com/<your-fork>/Web_Automation/main/deploy/scripts/install_ec2.sh)

# --- Option B: rsync from your laptop, then install ---
rsync -avz --exclude data --exclude .venv ./Web_Automation/ \
    ubuntu@<EC2-public-IP>:/tmp/automation/
ssh ubuntu@<EC2-public-IP>
sudo mkdir -p /opt/automation
sudo cp -a /tmp/automation/. /opt/automation/
sudo bash /opt/automation/deploy/scripts/install_ec2.sh
```

The installer:

- Creates the `automation` system user (no shell login needed).
- Installs Python 3.12, build tools, Nginx, certbot, and Chromium runtime libs.
- Builds a virtualenv at `/opt/automation/venv` and installs the framework + Playwright Chromium.
- Generates `/etc/automation/automation.env` with a random API token.
- Installs and starts the systemd unit `automation.service`.
- Installs the Nginx site (you still need to swap in your real domain and run certbot).

**Verify**

```bash
sudo systemctl status automation
curl http://127.0.0.1:8080/health
# {"status":"ok"}
```

---

## 4. HTTPS, domain, and DuckDNS

### 4.1 Point your domain at the instance

If using DuckDNS:

```bash
# On any machine that can reach DuckDNS
curl "https://www.duckdns.org/update?domains=<your-subdomain>&token=<your-token>&ip="
```

Or set up the cron updater that ships with the framework:

```bash
sudo bash -c 'cat >> /etc/automation/automation.env <<EOF
DUCKDNS_DOMAIN=<your-subdomain>
DUCKDNS_TOKEN=<your-duckdns-token>
EOF'
echo '*/5 * * * * root . /etc/automation/automation.env && /opt/automation/deploy/scripts/duckdns_update.sh' \
  | sudo tee /etc/cron.d/duckdns
```

### 4.2 Edit Nginx and run certbot

```bash
sudo sed -i 's/your-domain\.duckdns\.org/<your-subdomain>.duckdns.org/g' \
    /etc/nginx/sites-available/automation.conf
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d <your-subdomain>.duckdns.org \
    --non-interactive --agree-tos -m <your-email>
```

Visit `https://<your-subdomain>.duckdns.org/dashboard`. Paste the API
token (from `/etc/automation/automation.env`) into the dashboard's token
field — it's stored in your browser only.

### 4.3 Optional Telegram control

```bash
sudo bash -c 'cat >> /etc/automation/automation.env <<EOF
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321
EOF'
sudo systemctl restart automation
```

Then in Telegram: send `/help` to your bot. Only chat IDs in
`TELEGRAM_ALLOWED_CHAT_IDS` are accepted.

---

## 5. The directory layout you'll work with

```
/opt/automation/                 # owned by user 'automation'
├── config/
│   ├── config.json              # framework config (hot-reloaded)
│   ├── accounts.json            # YOUR accounts (hot-reloaded)
│   └── workflows/               # YOUR workflows (.yaml / .json)
├── plugins_external/            # drop-in plugins
├── data/                        # runtime state (back this up)
│   ├── state/accounts.sqlite    # account status + result history
│   ├── learning/memory.sqlite   # AI-learned selectors
│   ├── logs/                    # rotating activity / error / debug
│   ├── profiles/                # per-account browser profile dirs
│   ├── screenshots/             # workflow screenshots
│   └── downloads/               # browser downloads
└── venv/                        # python virtualenv

/etc/automation/automation.env   # API token + telegram + duckdns
/etc/systemd/system/automation.service
/etc/nginx/sites-available/automation.conf
```

**Editing config files:** become the `automation` user (or `sudo` as
root), edit, then either let the watcher pick it up or trigger a
reload. The watcher polls every ~2 seconds; CLI reload is instant.

---

## 6. Adding accounts — the operator's daily job

### 6.1 Create or edit `accounts.json`

```bash
sudo -u automation $EDITOR /opt/automation/config/accounts.json
```

**Recommended envelope** (`number` is the primary id):

```json
{
  "accounts": [
    {"number": "9999999999", "password": "your-password-1"},
    {"number": "8888888888", "password": "your-password-2"}
  ]
}
```

**With per-account browser overrides** (proxy, fingerprint, profile sharing):

```json
{
  "accounts": [
    {
      "number": "5550000001",
      "password": "...",
      "metadata": {
        "group": "alpha",
        "profile_id": "device-X",
        "proxy": "http://user:pass@your-proxy.example:8080",
        "user_agent": "Mozilla/5.0 ...",
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-US",
        "timezone_id": "America/New_York"
      }
    }
  ]
}
```

See [`docs/identity-testing.md`](identity-testing.md) for every metadata
field and the 4-cell detection matrix.

### 6.2 Apply changes

```bash
automation-cli accounts reload
# {"loaded": 17, "rejected": 0, "stats": {...}}
```

You can also rely on the file watcher (changes pick up within a few
seconds) — `reload` just makes it instant.

### 6.3 Verify

```bash
automation-cli accounts status
# total / pending / running / completed / failed counts
# + processing speed and last_loaded_at

automation-cli accounts list --status pending --limit 5
```

### 6.4 What gets rejected, and why

Validation is strict on purpose. Rows that fail:

- not an object
- no usable identifier (`id` / `number` / `username` / `email`)
- empty password (when `accounts_require_password: true`)
- invalid phone-number format
- duplicate identifier in the same file

go to the rejected table:

```bash
automation-cli accounts rejected
# [{"source_index": 3, "reason": "duplicate identifier: 9999999999", ...}]
```

Fix the source file, run `accounts reload` again. The rejected list
always reflects the *current* file.

### 6.5 Removing accounts

Drop the row from `accounts.json` and `reload`. Already-completed
accounts stay in the SQLite state (they're not re-run) but they no
longer appear in the source list. To delete the runtime state too:

```bash
# Reset just the runtime state for one account back to 'pending'
automation-cli accounts reset <account_id>

# Wipe everything and start over (DESTRUCTIVE)
sudo systemctl stop automation
sudo rm -f /opt/automation/data/state/accounts.sqlite
sudo systemctl start automation
automation-cli accounts reload
```

---

## 7. Editing the framework config

```bash
sudo -u automation $EDITOR /opt/automation/config/config.json
automation-cli reload         # apply now (or wait ~2s for the watcher)
```

Common settings you'll touch:

| Key | Purpose |
|---|---|
| `targets[0].url` | Default site URL (used by your workflows) |
| `accounts_max_attempts` | Retries before an account is permanently `failed` |
| `accounts_lease_seconds` | How long a worker can hold an account before its lock expires (default 600) |
| `playwright.headless` | `true` on EC2 always; `false` only on a desktop |
| `playwright.timeout` | Default per-step browser timeout (ms) |
| `scheduler.max_workers` | Bound on concurrent scheduled jobs |
| `ai.enabled` | Disable the AI brain entirely if you only run static workflows |
| `ai.dry_run` | Plan without acting (useful for review) |
| `plugins.enabled.<name>` | Enable / disable a plugin |
| `logging.level` | `INFO`, `DEBUG`, `WARNING` |

You can also override anything via env (in `/etc/automation/automation.env`):

```bash
AUTOMATION__SCHEDULER__MAX_WORKERS=20
AUTOMATION__AI__DRY_RUN=true
```

After editing the env file: `sudo systemctl restart automation`.

---

## 8. Running workflows

### 8.1 Single account

```bash
automation-cli workflow run example_login.json --account user001 \
    --inputs '{"target_url":"https://<your-subdomain>.duckdns.org/login"}'
```

`account.metadata` is automatically projected into the workflow as
`${account.metadata.<key>}` — proxies, viewport, etc., are wired
through `BrowserManager`.

### 8.2 Batch — many accounts

```bash
# sequential (the default; required if accounts share a profile_id)
automation-cli workflow batch example_login.json \
    --accounts user001,user002,user003 \
    --inputs '{"target_url":"https://<your-subdomain>.duckdns.org/login"}'

# parallel — only when every account has its OWN profile_id
automation-cli workflow batch example_login.json \
    --accounts user001,user002,user003 \
    --parallel --max-parallel 4

# stop the sequential batch on the first failure
automation-cli workflow batch example_login.json \
    --accounts user001,user002 --stop-on-failure
```

### 8.3 Check results

```bash
automation-cli workflow results
automation-cli accounts results --limit 50
automation-cli accounts results <account_id> --limit 20
```

Or the dashboard: `https://<your-subdomain>.duckdns.org/dashboard`.

### 8.4 Identity / repeat-registration matrix

See [`docs/identity-testing.md`](identity-testing.md) for the full guide.
Short version:

```bash
sudo -u automation cp \
    /opt/automation/config/accounts.detection-matrix.example.json \
    /opt/automation/config/accounts.json
sudo -u automation $EDITOR /opt/automation/config/accounts.json   # set real proxies
automation-cli accounts reload

automation-cli workflow batch identity_test_register.yaml \
    --accounts probe-a,probe-b,probe-c,probe-d,probe-e \
    --inputs '{
      "target_url": "https://<your-subdomain>.duckdns.org/signup",
      "success_url_part": "/welcome",
      "duplicate_selector": ".error-banner.duplicate"
    }'
```

---

## 9. Monitoring

### 9.1 Dashboard

`https://<your-subdomain>.duckdns.org/dashboard` shows:

- System CPU / memory / disk / uptime
- Engine running flag, scheduler workers, active jobs
- Plugins (enable/disable inline)
- Account stats: total / pending / running / completed / failed / skipped + speed/min
- Queue sizes
- AI brain status + last decision summary
- Recent workflow runs (status, duration, step count)
- Tail of activity log

### 9.2 CLI

```bash
automation-cli status        # full snapshot
automation-cli health        # liveness check (no auth needed)
automation-cli metrics       # JSON metrics
automation-cli metrics --prom    # Prometheus text format
automation-cli accounts status
automation-cli ai status     # last AI decision
```

### 9.3 Logs

```bash
# Structured rotating files (kept under data/logs/)
automation-cli logs activity --lines 500
automation-cli logs error --lines 200

# systemd journal (everything stdout/stderr)
sudo journalctl -u automation -f --no-pager
sudo journalctl -u automation --since "1 hour ago"
sudo journalctl -u automation -p err

# Nginx access / error
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

### 9.4 Telegram (if enabled)

```
/status                    engine state
/health                    liveness
/accounts                  status snapshot + speed
/completed 50              last 50 completed
/failed 20                 last 20 failed with errors
/rejected                  validation rejects
/reload_accounts           hot reload accounts.json
/logs activity             recent log lines
/plugins                   list plugins
/ai                        AI brain status
```

---

## 10. Restart, reload, and recovery

| Goal | Command |
|---|---|
| Apply config / accounts changes (no downtime) | `automation-cli reload` |
| Restart the engine in place (keeps state) | `automation-cli restart` |
| Restart the systemd service (full reboot) | `sudo systemctl restart automation` |
| Stop everything | `sudo systemctl stop automation` |
| Free expired account locks (after a worker crash) | `automation-cli accounts reap` |
| Free a specific account's lock | `automation-cli accounts release <id>` |
| Reset a single account back to `pending` | `automation-cli accounts reset <id>` |
| Reset a whole batch (DESTRUCTIVE) | stop service → delete `data/state/accounts.sqlite` → start service → `accounts reload` |
| Wipe browser profile for an account | `automation-cli accounts reset <id>` (auto-removes profile) |

### 10.1 EC2 reboot — what survives

Everything important. `data/state/accounts.sqlite` keeps account
statuses; `data/learning/memory.sqlite` keeps AI selectors;
`data/profiles/` keeps cookies and storage; the systemd unit
auto-starts on boot. After reboot:

```bash
sudo systemctl status automation
automation-cli accounts reap     # release any locks held by the dead process
automation-cli status
```

### 10.2 The framework crashed mid-batch

```bash
sudo systemctl restart automation
automation-cli accounts reap                      # release expired locks
automation-cli accounts list --status running     # should be empty after reap
# Pending accounts will be picked up automatically by the next batch / scheduler.
```

### 10.3 The framework is unresponsive

```bash
sudo systemctl status automation
sudo journalctl -u automation -n 200 --no-pager     # last 200 lines
sudo systemctl restart automation
```

If `restart` doesn't recover it:

```bash
sudo systemctl stop automation
sudo systemctl reset-failed automation
ps -ef | grep automation                    # any orphans
sudo systemctl start automation
```

---

## 11. Updating the framework

```bash
ssh ubuntu@<EC2-public-IP>
cd /opt/automation
sudo -u automation git pull --ff-only

# If requirements.txt changed:
sudo -u automation /opt/automation/venv/bin/pip install -r requirements.txt
sudo -u automation /opt/automation/venv/bin/pip install -e .

# If new Playwright version:
sudo -u automation /opt/automation/venv/bin/playwright install chromium

sudo systemctl restart automation
automation-cli status
```

The installer is idempotent — re-running `install_ec2.sh` does the same
work plus reinstalls the systemd unit and Nginx site if they changed.

---

## 12. Backup and restore

What to back up (in priority order):

| Path | What | Critical? |
|---|---|---|
| `/opt/automation/config/` | Your accounts + workflows + framework config | **Yes** |
| `/etc/automation/automation.env` | API token, Telegram secrets | **Yes** |
| `/opt/automation/data/state/` | Account status + result history (SQLite) | High |
| `/opt/automation/data/learning/` | AI memory (SQLite) | Medium — re-learnable |
| `/opt/automation/data/profiles/` | Browser cookies / storage per account | Medium |
| `/opt/automation/data/screenshots/` | Run artifacts | Low |
| `/opt/automation/data/logs/` | Logs (rotated) | Low |

Quick backup script (run from your laptop):

```bash
ssh ubuntu@<EC2-public-IP> 'sudo tar czf /tmp/auto-backup.tar.gz \
    /opt/automation/config /opt/automation/data/state \
    /opt/automation/data/learning /etc/automation/automation.env'
scp ubuntu@<EC2-public-IP>:/tmp/auto-backup.tar.gz ./backups/auto-$(date +%F).tar.gz
```

Restore:

```bash
sudo systemctl stop automation
sudo tar xzf /tmp/auto-backup.tar.gz -C /
sudo chown -R automation:automation /opt/automation
sudo systemctl start automation
```

---

## 13. Security — operator responsibilities

- **Rotate the API token** if leaked:
  `openssl rand -hex 32` → replace `AUTOMATION_API_TOKEN` in
  `/etc/automation/automation.env` → `sudo systemctl restart automation`.
  Update the dashboard token field too (clear it from `localStorage` first).
- **Lock down SSH:** SG inbound 22 to your IP only, disable password
  auth, use a strong key.
- **Telegram allow-list:** `TELEGRAM_ALLOWED_CHAT_IDS` must be set;
  anything not in the list is silently dropped.
- **Don't expose** `127.0.0.1:8080` directly. Always reach it through
  Nginx + HTTPS. The systemd unit binds to `127.0.0.1`, so the SG
  protects you only if you don't add a port-forward by mistake.
- **Don't run with `AUTOMATION_API_TOKEN` empty.** The startup banner
  warns about this; operators should fix it immediately.
- **Audit log:** every control action is in `/control/audit`, mirrored
  to the activity log file.

---

## 14. Cost & sizing

- **Idle:** the framework idles around ~150–250 MB RAM and minimal CPU.
- **Active (1 browser session):** 300–600 MB RAM extra per session.
- **Active (10 browser sessions):** plan for ~3–5 GB RAM. Use `t3.large`
  or larger.
- **Storage:** screenshots and browser profiles grow. Set a cron to prune:

  ```bash
  echo '0 3 * * * automation find /opt/automation/data/screenshots -mtime +14 -delete' \
    | sudo tee /etc/cron.d/automation-prune
  ```

- **Stop the instance** when not in use (Elastic IP keeps your DNS).
  After start, the systemd unit auto-runs and DuckDNS cron resyncs the IP.

---

## 15. Troubleshooting

### "Connection refused" from `automation-cli`

```bash
sudo systemctl status automation
# inactive? -> sudo systemctl start automation
# active but failing? -> sudo journalctl -u automation -n 100
```

### Dashboard returns 401

Token mismatch. Open the dashboard, paste the *current* token from
`/etc/automation/automation.env` into the token field, refresh.

### Accounts won't load

```bash
automation-cli accounts rejected   # see why each row failed
# Common causes:
#   - "not an object" -> a row is a string instead of {...}
#   - "missing identifier" -> no id, number, username, or email
#   - "missing or empty password"
#   - "invalid number format" -> phone has letters or is too short
#   - "duplicate identifier" -> same number/id twice
sudo -u automation $EDITOR /opt/automation/config/accounts.json
automation-cli accounts reload
```

### A workflow can't find a form field

The AI brain learns selectors over time. To inspect:

```bash
automation-cli ai status
automation-cli ai pages           # known page signatures
automation-cli ai stats           # workflow success rates
```

For one-off debugging, run with `ai.dry_run: true` to see the planned
steps without acting.

### Browser sessions pile up

```bash
automation-cli status              # how many sessions are open
# Reset one
automation-cli accounts reset <id>
# Or full reset
sudo systemctl restart automation
```

### `Chromium failed to launch`

Missing system libs or low memory. Check:

```bash
free -h                            # at least 1 GB free recommended
sudo journalctl -u automation -p err -n 50
# Reinstall Playwright libs if needed:
sudo -u automation /opt/automation/venv/bin/playwright install --with-deps chromium
```

### certbot fails

Make sure DNS for `<your-subdomain>.duckdns.org` actually points at
your EC2 IP, then re-run:

```bash
dig +short <your-subdomain>.duckdns.org
sudo certbot --nginx -d <your-subdomain>.duckdns.org
```

### Telegram doesn't respond

```bash
grep TELEGRAM /etc/automation/automation.env
# TOKEN set? chat ID in the allow-list?
sudo journalctl -u automation -g telegram -n 50
```

If the bot was removed from a chat, re-add it and re-send `/start`.

---

## 16. Multi-EC2 (optional)

You can run additional worker nodes that consume work from the leader.

**On the leader** (already done by the installer): the API at port 8080
behind Nginx is the coordinator.

**On each worker node:**

```bash
# Same install
sudo bash /opt/automation/deploy/scripts/install_ec2.sh
# Stop the API service — workers don't need it
sudo systemctl disable --now automation
# Point the worker at the leader and start it
sudo bash -c 'cat > /etc/automation/automation.env <<EOF
AUTOMATION_API_URL=https://<leader-domain>
AUTOMATION_API_TOKEN=<same-token-as-leader>
EOF'
sudo cp /opt/automation/deploy/systemd/automation-worker.service /etc/systemd/system/
sudo systemctl enable --now automation-worker
```

On the leader: `automation-cli` will show the worker in
`/distributed/status`, and you can submit assignments via
`POST /distributed/submit`.

---

## 17. Where to go next

- [`docs/architecture.md`](architecture.md) — design overview
- [`docs/accounts.md`](accounts.md) — account model in detail
- [`docs/workflows.md`](workflows.md) — the full workflow DSL
- [`docs/identity-testing.md`](identity-testing.md) — proxy + fingerprint matrix
- [`docs/plugins.md`](plugins.md) — write a plugin to add custom logic
- [`docs/ai-brain.md`](ai-brain.md) — how the AI brain decides
- [`docs/termux-control.md`](termux-control.md) — operate from a phone

If something in this guide is wrong or missing, file an issue or PR
against the repo — operator runbooks are living documents.
