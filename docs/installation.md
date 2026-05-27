# Installation

## Requirements

- Python 3.12 or newer
- Linux (production) or macOS / Windows (development)
- ~500 MB disk for Playwright browsers (optional)
- 1 GB RAM minimum (2 GB recommended for browser usage)

## Local development

```bash
git clone <your-fork>
cd Web_Automation
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
playwright install chromium    # optional, only for browser workflows

cp .env.example .env
# generate a token: openssl rand -hex 32
echo "AUTOMATION_API_TOKEN=$(openssl rand -hex 32)" > .env

python -m automation server --config config/config.json
```

Open `http://127.0.0.1:8080/dashboard`. Paste the token from `.env` into
the dashboard token field (it will be stored in your browser only).

## Configuration

Copy and edit:

```bash
cp config/accounts.example.json config/accounts.json
$EDITOR config/config.json
```

The framework looks for environment overrides of any config key:

```bash
export AUTOMATION__SCHEDULER__MAX_WORKERS=20
export AUTOMATION__AI__ENABLED=false
```

The config file is hot-reloaded — change it on disk, the engine re-applies
without a restart (within ~2 seconds by default).

## CLI

The CLI is installed as `automation-cli`:

```bash
automation-cli status
automation-cli plugins list
automation-cli logs activity --lines 50
automation-cli workflow run example_login.json --account user001
```

It reads `AUTOMATION_API_URL` (default `http://127.0.0.1:8080`) and
`AUTOMATION_API_TOKEN` from the environment.

## Smoke test

```bash
python -c "from automation.core.engine import Engine; print('ok')"
python -m automation cli status   # after server is running
```
