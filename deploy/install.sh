#!/usr/bin/env bash
#
# Install / update the PJ-Portal bot on an Ubuntu/Debian VM.
# Idempotent — safe to re-run after a git pull.
#
# Usage (on the Oracle VM, as any sudo-capable user):
#   curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/install.sh | sudo bash
# or, if the repo is already cloned to /opt/pjportal:
#   sudo bash /opt/pjportal/deploy/install.sh

set -euo pipefail

REPO=${REPO:-"https://github.com/seb711/pj-portal-bot.git"}
INSTALL_DIR=${INSTALL_DIR:-/opt/pjportal}
STATE_DIR=${STATE_DIR:-/var/lib/pjportal}
ENV_FILE=${ENV_FILE:-/etc/pjportal.env}
CRED_DIR=${CRED_DIR:-/etc/pjportal}
PWD_CRED=${PWD_CRED:-$CRED_DIR/pjportal_pwd.cred}
SVC_USER=${SVC_USER:-pjportal}

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

echo "==> Installing system dependencies"
apt-get update -qq
apt-get install -y --no-install-recommends git python3 python3-venv python3-pip libxml2 libxslt1.1

echo "==> Ensuring service user '$SVC_USER'"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi

echo "==> Fetching source into $INSTALL_DIR"
# The repo is owned by $SVC_USER after the first install, so root's git would
# refuse to operate on it (dubious-ownership guard). Mark it safe.
git config --global --add safe.directory "$INSTALL_DIR" >/dev/null 2>&1 || true
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  git clone "$REPO" "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" pull --ff-only
fi

echo "==> Building Python venv"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

echo "==> Preparing state directory $STATE_DIR"
mkdir -p "$STATE_DIR"
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR" "$STATE_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> Seeding $ENV_FILE from example (edit before enabling)"
  cp "$INSTALL_DIR/deploy/pjportal.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  chown root:"$SVC_USER" "$ENV_FILE"
  cat <<EOF

  Next step:
    sudo nano $ENV_FILE   # fill in non-secret config (user, hospital, ...)
    sudo bash $INSTALL_DIR/deploy/install.sh   # re-run to encrypt password + enable timer

EOF
  exit 0
fi
chmod 640 "$ENV_FILE"
chown root:"$SVC_USER" "$ENV_FILE"

echo "==> Verifying encrypted password credential"
if ! command -v systemd-creds >/dev/null 2>&1; then
  echo "ERROR: systemd-creds not found (needs systemd >= 250)." >&2
  echo "       This VM's systemd is too old — use Ubuntu 24.04 for the encrypted-credential setup." >&2
  exit 1
fi
mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR"
if [[ ! -f "$PWD_CRED" ]]; then
  echo
  echo "Encrypting your pj-portal.de password with the machine's host key."
  echo "Nothing is written to disk in plaintext — you can't recover this on another machine."
  echo
  # Read from /dev/tty so this works when the installer is invoked via `curl | sudo bash`
  # (in which case stdin is the pipe, not the terminal).
  if [[ ! -r /dev/tty ]]; then
    echo "ERROR: no controlling terminal — can't prompt for password." >&2
    echo "       Re-run the installer directly (not via curl pipe):" >&2
    echo "         sudo bash /opt/pjportal/deploy/install.sh" >&2
    exit 1
  fi
  PJP_PWD=""
  while [[ -z "$PJP_PWD" ]]; do
    read -rsp "pj-portal.de password: " PJP_PWD </dev/tty
    echo
    if [[ -z "$PJP_PWD" ]]; then
      echo "Password cannot be empty. Try again." >&2
    fi
  done
  # Pipe via stdin so the plaintext never touches a temp file
  printf '%s' "$PJP_PWD" | systemd-creds encrypt --name=pjportal_pwd - "$PWD_CRED"
  unset PJP_PWD
  chmod 600 "$PWD_CRED"
  chown root:root "$PWD_CRED"
  echo "==> Encrypted password written to $PWD_CRED"
else
  echo "==> Existing encrypted password at $PWD_CRED (delete it to re-encrypt)"
fi

echo "==> Installing systemd units"
install -m 644 "$INSTALL_DIR/deploy/pjportal.service" /etc/systemd/system/pjportal.service
install -m 644 "$INSTALL_DIR/deploy/pjportal.timer"   /etc/systemd/system/pjportal.timer
systemctl daemon-reload
systemctl enable --now pjportal.timer

# Force a "bot armed" confirmation push on the very next run. The bot writes
# this stamp itself after the first push; deleting it here re-arms the ping
# so you can visually confirm the pipeline after every deploy.
rm -f "$STATE_DIR/started.stamp"

echo "==> Kicking one check now to trigger the start-ping and warm the cookie"
systemctl start pjportal.service || true

echo
echo "Done. Useful commands:"
echo "  systemctl status pjportal.timer"
echo "  systemctl list-timers pjportal.timer"
echo "  journalctl -u pjportal -f"
echo "  sudo systemctl start pjportal.service   # run one check right now"
