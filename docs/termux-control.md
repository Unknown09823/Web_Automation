# Termux Control

Control the framework from an Android phone.

## Setup

In Termux:

```bash
pkg update && pkg install -y python openssh git
pip install --upgrade pip
git clone <your-fork>
cd Web_Automation
pip install -e .
```

Set environment variables (or put in `~/.bashrc`):

```bash
export AUTOMATION_API_URL="https://your-domain.duckdns.org"
export AUTOMATION_API_TOKEN="your-token-from-/etc/automation/automation.env"
```

## Commands

```bash
automation-cli status
automation-cli health
automation-cli start
automation-cli stop
automation-cli restart
automation-cli reload
automation-cli logs activity --lines 50
automation-cli logs error --lines 50
automation-cli plugins list
automation-cli plugins disable example_task
automation-cli plugins enable heartbeat
automation-cli accounts list
automation-cli accounts list --status pending
automation-cli accounts reset user001
automation-cli workers
automation-cli queue
automation-cli workflow list
automation-cli workflow run example_login.json --account user001
automation-cli ai status
automation-cli metrics
```

## Without the CLI

`automation-cli` is just a thin HTTP wrapper. From any tool that can issue
HTTPS requests, hit the API directly:

```bash
curl -H "Authorization: Bearer $AUTOMATION_API_TOKEN" \
     https://your-domain.duckdns.org/status
```

## Telegram

A more phone-friendly option: run the framework with Telegram enabled.
Set in `/etc/automation/automation.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Then message your bot:

```
/status
/start
/stop
/logs activity
/plugins
/accounts
/ai
```

Only chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS` are accepted. Restart the
service after editing the env file.
