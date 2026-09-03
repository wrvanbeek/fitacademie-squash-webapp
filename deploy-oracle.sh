#!/bin/bash
# deploy-oracle.sh — Deploy FitAcademie Squash to Oracle Cloud Always Free VM
# Run on the VM: ssh ubuntu@<IP> 'bash -s' < deploy-oracle.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────
APP_NAME="fa-squash"
GHCR_IMAGE="ghcr.io/wrvanbeek/fa-squash:latest"
APP_PORT=8080
DATA_DIR="/home/app/data"

# Secrets — PAS DEZE AAN VOOR JOUW SETUP
JWT_SECRET="${JWT_SECRET:-fb38tvGG08z83d-3J-QY34PRW-zuEstBA-5DCwTgDAs}"
ENCRYPTION_KEY="${ENCRYPTION_KEY:-2p9rQ-Js9Qv4dTh9t2Oqupc4NjmSAtN4Y32BopYP9uE=}"

# ── Helpers ───────────────────────────────────────────────────────────
log() { echo -e "\033[1;32m[$(date '+%H:%M:%S')]\033[0m $*"; }
err() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; }

# ── 1. System update & Docker install ────────────────────────────────
log "Updating system & installing Docker..."
sudo apt-get update -qq
sudo apt-get install -y -qq docker.io docker-compose-plugin curl gnupg2
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
log "Docker installed: $(docker --version)"

# ── 2. Login to GHCR (if private) ────────────────────────────────────
if [[ -n "${GHCR_TOKEN:-}" ]]; then
    log "Logging into GHCR..."
    echo "$GHCR_TOKEN" | docker login ghcr.io -u wrvanbeek --password-stdin
else
    log "GHCR_TOKEN not set — assuming public image or already logged in"
fi

# ── 3. Pull latest image ─────────────────────────────────────────────
log "Pulling $GHCR_IMAGE..."
docker pull "$GHCR_IMAGE"

# ── 4. Prepare data directory ────────────────────────────────────────
log "Creating data directory..."
sudo mkdir -p "$DATA_DIR"
sudo chown -R 1000:1000 "$DATA_DIR"  # app user in container

# ── 5. Stop & remove old container ───────────────────────────────────
log "Stopping old container (if any)..."
docker stop "$APP_NAME" 2>/dev/null || true
docker rm "$APP_NAME" 2>/dev/null || true

# ── 6. Run new container ─────────────────────────────────────────────
log "Starting container on port $APP_PORT..."
docker run -d \
  --name "$APP_NAME" \
  --restart unless-stopped \
  -p "$APP_PORT:$APP_PORT" \
  -e JWT_SECRET="$JWT_SECRET" \
  -e ENCRYPTION_KEY="$ENCRYPTION_KEY" \
  -v "$DATA_DIR:/app/data" \
  "$GHCR_IMAGE"

# ── 7. Health check ──────────────────────────────────────────────────
log "Waiting for app to start..."
for i in {1..30}; do
    if curl -sf "http://localhost:$APP_PORT/health" >/dev/null 2>&1; then
        log "✅ App is healthy!"
        break
    fi
    sleep 2
    [[ $i -eq 30 ]] && { err "App failed to start"; docker logs "$APP_NAME" --tail 50; exit 1; }
done

# ── 8. Create systemd service (auto-restart on reboot) ───────────────
log "Creating systemd service..."
sudo tee /etc/systemd/system/fa-squash.service >/dev/null <<EOF
[Unit]
Description=FitAcademie Squash Webapp
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker start -a $APP_NAME
ExecStop=/usr/bin/docker stop -t 10 $APP_NAME
TimeoutStartSec=60
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable fa-squash.service
log "Systemd service enabled (auto-starts on boot)"

# ── 9. Optional: Caddy reverse proxy for HTTPS ───────────────────────
if command -v caddy >/dev/null 2>&1; then
    log "Caddy already installed, skipping..."
else
    read -p "Setup Caddy for HTTPS? (needs domain pointing to this IP) [y/N]: " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log "Installing Caddy..."
        sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
        sudo apt-get update -qq && sudo apt-get install -y -qq caddy
        
        read -p "Domain name (e.g. squash.jouw.domein.nl): " DOMAIN
        sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
    reverse_proxy localhost:$APP_PORT
    encode gzip
}
EOF
        sudo systemctl reload caddy
        log "✅ Caddy configured for $DOMAIN (HTTPS auto via Let's Encrypt)"
    fi
fi

# ── 10. Final status ─────────────────────────────────────────────────
PUBLIC_IP=$(curl -s ifconfig.me || curl -s icanhazip.com)
log "🎉 Deploy complete!"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "App URL (HTTP):  http://$PUBLIC_IP:$APP_PORT"
if [[ -n "${DOMAIN:-}" ]]; then
    echo "App URL (HTTPS): https://$DOMAIN"
fi
echo "Health check:    http://$PUBLIC_IP:$APP_PORT/health"
echo "Container logs:  docker logs -f $APP_NAME"
echo "Service status:  sudo systemctl status fa-squash"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"