#!/usr/bin/env bash
# install_ec2.sh — one-shot installer for Ubuntu 22.04+ on AWS EC2.
#
# Run as root or via sudo. Idempotent: re-running upgrades the deployment.
#
#   curl -fsSL https://example.com/install_ec2.sh | sudo bash
# or
#   sudo bash deploy/scripts/install_ec2.sh
set -euo pipefail

APP_USER="${APP_USER:-automation}"
APP_DIR="${APP_DIR:-/opt/automation}"
CFG_DIR="${CFG_DIR:-/etc/automation}"
PYTHON="${PYTHON:-python3.12}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2; exit 1
fi

apt-get update
apt-get install -y --no-install-recommends \
    "${PYTHON}" "${PYTHON}-venv" "${PYTHON}-dev" \
    build-essential git nginx curl ca-certificates \
    nodejs npm \
    fonts-liberation libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    certbot python3-certbot-nginx

id -u "${APP_USER}" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "${APP_USER}"
mkdir -p "${APP_DIR}" "${CFG_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

if [[ -d "${APP_DIR}/.git" ]]; then
  sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only
elif [[ -n "${REPO_URL:-}" ]]; then
  sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
else
  echo "Note: copy your project tree to ${APP_DIR} before running, or set REPO_URL." >&2
fi

sudo -u "${APP_USER}" "${PYTHON}" -m venv "${APP_DIR}/venv"
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade pip wheel
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install -e "${APP_DIR}"
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/playwright" install chromium || true

# Build the modern dashboard (React + Vite). The build outputs into
# src/automation/dashboard/ where the FastAPI app already mounts it.
if [[ -d "${APP_DIR}/dashboard-ui" ]]; then
  if command -v npm >/dev/null 2>&1; then
    sudo -u "${APP_USER}" bash -c "cd ${APP_DIR}/dashboard-ui && npm ci --no-audit --no-fund && npm run build"
  else
    echo "warn: npm not available; skipping dashboard build (basic HTML fallback served)" >&2
  fi
fi

mkdir -p "${APP_DIR}/data"/{logs,state,profiles,screenshots,learning,downloads}
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/data"

# Environment file
if [[ ! -f "${CFG_DIR}/automation.env" ]]; then
  TOKEN="$(openssl rand -hex 32)"
  cat > "${CFG_DIR}/automation.env" <<ENV
AUTOMATION_API_TOKEN=${TOKEN}
AUTOMATION_API_URL=http://127.0.0.1:8080
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_ALLOWED_CHAT_IDS=
ENV
  chmod 600 "${CFG_DIR}/automation.env"
  echo "Generated API token at ${CFG_DIR}/automation.env"
fi

# systemd unit
install -m 0644 "${APP_DIR}/deploy/systemd/automation.service" /etc/systemd/system/automation.service
systemctl daemon-reload
systemctl enable --now automation.service

# Nginx site
if [[ -f "${APP_DIR}/deploy/nginx/automation.conf" ]]; then
  install -m 0644 "${APP_DIR}/deploy/nginx/automation.conf" /etc/nginx/sites-available/automation.conf
  ln -sf /etc/nginx/sites-available/automation.conf /etc/nginx/sites-enabled/automation.conf
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
fi

echo
echo "Installed. Service status: systemctl status automation"
echo "API token: $(grep AUTOMATION_API_TOKEN ${CFG_DIR}/automation.env | cut -d= -f2)"
echo "Update Nginx config (your-domain.duckdns.org) and run: certbot --nginx"
