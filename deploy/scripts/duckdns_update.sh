#!/usr/bin/env bash
# Update DuckDNS A-record. Add to root cron, e.g.:
#   */5 * * * * /opt/automation/deploy/scripts/duckdns_update.sh
set -euo pipefail
: "${DUCKDNS_DOMAIN:?Set DUCKDNS_DOMAIN}"
: "${DUCKDNS_TOKEN:?Set DUCKDNS_TOKEN}"
LOG="/var/log/automation/duckdns.log"
mkdir -p "$(dirname "${LOG}")"
echo "$(date -u +%FT%TZ) update" >> "${LOG}"
curl -fsS "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip=" \
  >> "${LOG}" 2>&1 || echo "$(date -u +%FT%TZ) failed" >> "${LOG}"
