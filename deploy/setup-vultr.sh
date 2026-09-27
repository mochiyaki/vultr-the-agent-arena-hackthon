#!/usr/bin/env bash
# One-shot bootstrap for a fresh Ubuntu 24.04 Vultr Cloud Compute instance.
# Installs Docker + gVisor (runsc), builds images and starts the control plane on port 80.
#
#   ssh root@<vultr-ip> 'bash -s' < deploy/setup-vultr.sh
#   (then copy .env with your VULTR_INFERENCE_API_KEY and run `docker compose up -d`)
set -euo pipefail

REPO_URL="${REPO_URL:-}"
APP_DIR="${APP_DIR:-/opt/blast-radius-zero}"

echo "==> Installing Docker"
apt-get update -y
apt-get install -y ca-certificates curl gnupg git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

echo "==> Installing gVisor (runsc)"
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" > /etc/apt/sources.list.d/gvisor.list
apt-get update -y && apt-get install -y runsc
runsc install            # registers the "runsc" runtime in /etc/docker/daemon.json
systemctl restart docker
docker info --format '{{json .Runtimes}}' | grep -q runsc && echo "gVisor runtime registered" || echo "WARNING: runsc not registered"

echo "==> Fetching application"
if [ -n "$REPO_URL" ]; then
  rm -rf "$APP_DIR" && git clone "$REPO_URL" "$APP_DIR"
elif [ ! -d "$APP_DIR" ]; then
  echo "Set REPO_URL or copy the repo to $APP_DIR first"; exit 1
fi
cd "$APP_DIR"
[ -f .env ] || cp .env.example .env

echo "==> Building images"
docker build -t brz-sandbox:latest sandbox
docker compose build

echo "==> Basic firewall (allow ssh + http)"
if command -v ufw >/dev/null; then ufw allow 22/tcp && ufw allow 80/tcp && ufw --force enable; fi

echo
echo "Done. Next:"
echo "  1. nano $APP_DIR/.env   # set VULTR_INFERENCE_API_KEY"
echo "  2. cd $APP_DIR && docker compose up -d"
echo "  3. open http://$(curl -s ifconfig.me || hostname -I | awk '{print $1}')/"
