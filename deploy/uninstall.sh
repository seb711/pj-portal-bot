#!/usr/bin/env bash
#
# Remove everything the PJ-Portal bot installer created.
# Safe to re-run — each step is idempotent.
#
# Usage:  sudo bash /opt/pjportal/deploy/uninstall.sh
# or:     curl -fsSL https://raw.githubusercontent.com/seb711/pj-portal-bot/main/deploy/uninstall.sh | sudo bash

set -euo pipefail

INSTALL_DIR=${INSTALL_DIR:-/opt/pjportal}
STATE_DIR=${STATE_DIR:-/var/lib/pjportal}
ENV_FILE=${ENV_FILE:-/etc/pjportal.env}
CRED_DIR=${CRED_DIR:-/etc/pjportal}
SVC_USER=${SVC_USER:-pjportal}

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

echo "==> Stopping and disabling systemd units"
systemctl disable --now pjportal.timer 2>/dev/null || true
systemctl stop pjportal.service 2>/dev/null || true

echo "==> Removing systemd unit files and drop-ins"
rm -f /etc/systemd/system/pjportal.timer
rm -f /etc/systemd/system/pjportal.service
rm -rf /etc/systemd/system/pjportal.service.d
systemctl daemon-reload
systemctl reset-failed pjportal.service 2>/dev/null || true

echo "==> Removing encrypted credentials at $CRED_DIR"
rm -rf "$CRED_DIR"

echo "==> Removing env file $ENV_FILE"
rm -f "$ENV_FILE"

echo "==> Removing state dir $STATE_DIR"
rm -rf "$STATE_DIR"

echo "==> Removing source at $INSTALL_DIR"
rm -rf "$INSTALL_DIR"

echo "==> Removing service user $SVC_USER"
if id "$SVC_USER" >/dev/null 2>&1; then
  userdel "$SVC_USER" 2>/dev/null || true
fi

echo
echo "Done. To re-install from scratch:"
echo "  curl -fsSL https://raw.githubusercontent.com/seb711/pj-portal-bot/main/deploy/install.sh | sudo REPO=https://github.com/seb711/pj-portal-bot.git bash"
