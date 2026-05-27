# EC2 Deployment

Deploy on a fresh Ubuntu 22.04+ EC2 instance behind Nginx.

## 1. Launch the instance

- AMI: Ubuntu 22.04 LTS (or newer)
- Type: `t3.small` (2 GB RAM) or larger if you'll run browsers
- Security group: open `22/tcp` (SSH), `80/tcp`, `443/tcp`
- Attach an Elastic IP and (optionally) point a DuckDNS subdomain at it

## 2. Install

```bash
ssh ubuntu@your-ec2-host
sudo apt-get update
sudo apt-get install -y git
sudo git clone <your-fork> /opt/automation
sudo REPO_URL="" bash /opt/automation/deploy/scripts/install_ec2.sh
```

The installer:

- Creates `automation` system user
- Builds a virtualenv at `/opt/automation/venv`
- Installs the systemd unit and starts the service
- Generates a random API token in `/etc/automation/automation.env`
- Installs Playwright + Chromium

Confirm with:

```bash
sudo systemctl status automation
curl http://127.0.0.1:8080/health
```

## 3. Nginx + HTTPS

Edit `/etc/nginx/sites-available/automation.conf` and replace
`your-domain.duckdns.org` with your actual hostname, then:

```bash
sudo certbot --nginx -d your-domain.duckdns.org
sudo systemctl reload nginx
```

Visit `https://your-domain.duckdns.org/dashboard`.

## 4. DuckDNS auto-update (optional)

```bash
sudo bash -c 'echo "DUCKDNS_DOMAIN=your-subdomain" >> /etc/automation/automation.env'
sudo bash -c 'echo "DUCKDNS_TOKEN=xxxxxxxx" >> /etc/automation/automation.env'
echo '*/5 * * * * root . /etc/automation/automation.env && /opt/automation/deploy/scripts/duckdns_update.sh' | sudo tee /etc/cron.d/duckdns
```

## 5. Docker alternative

```bash
cp .env.example .env && $EDITOR .env
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

## 6. Multi-node

Run `automation-worker` on additional EC2 instances pointing at the
leader's API:

```bash
sudo systemctl edit automation-worker.service   # set AUTOMATION_API_URL
sudo systemctl enable --now automation-worker
```

The leader's `/distributed/status` endpoint shows registered workers and
their assignments.

## 7. Operations

- `journalctl -u automation -f` — live logs
- `automation-cli logs activity --lines 200` — structured activity logs
- `automation-cli status` — current state
- `automation-cli reload` — apply config changes without downtime
- `systemctl restart automation` — full restart (also auto-runs on crash)
