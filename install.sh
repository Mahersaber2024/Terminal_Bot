#!/usr/bin/env bash

set -euo pipefail
SERVICE_NAME="terminal-bot"
DEFAULT_INSTALL_DIR="/opt/terminal-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
STATE_FILE="/etc/${SERVICE_NAME}.install_dir"

REPO_URL=""
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Files/dirs that must exist in the project root before we install it.
REQUIRED_FILES=(
  "main.py"
  "handlers.py"
  "config.py"
  "crypto_utils.py"
  "sponsor_gate.py"
  "requirements.txt"
)
SERVERMANAGER_REQUIRED_FILES=(
  "__init__.py"
  "server_manager_handlers.py"
  "server_manager_engine.py"
  "server_manager_settings.py"
)

# ------------------ Colors / helpers ------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info(){ echo -e "${CYAN}i  $1${NC}"; }
ok(){ echo -e "${GREEN}OK $1${NC}"; }
warn(){ echo -e "${YELLOW}!  $1${NC}"; }
err(){ echo -e "${RED}X  $1${NC}"; }
press_enter(){ read -rp "Press Enter to continue..." _ || true; }

usage(){
  cat <<EOF
Usage: sudo bash install.sh [-r|--repo GIT_URL] [-d|--dir INSTALL_DIR]

  -r, --repo GIT_URL     Clone the project from this git URL instead of
                          using the local directory this script lives in.
  -d, --dir INSTALL_DIR  Where to install (default: ${DEFAULT_INSTALL_DIR}).
  -h, --help             Show this help.
EOF
}

INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -r|--repo) REPO_URL="$2"; shift 2 ;;
    -d|--dir) INSTALL_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unknown option: $1"; usage; exit 1 ;;
  esac
done

require_root(){
  if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (use sudo)."
    exit 1
  fi
}

detect_python(){
  if command -v python3 &>/dev/null; then
    PY_BIN=python3
  else
    err "Python 3 not found on this server."
    exit 1
  fi
}

install_system_packages(){
  info "Installing system dependencies (python3-venv, pip, iputils-ping for local ping checks)..."
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip iputils-ping
  ok "System dependencies installed."
}

fetch_source(){
  if [[ -n "$REPO_URL" ]]; then
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
      info "Existing git checkout found at ${INSTALL_DIR}, pulling latest..."
      git -C "${INSTALL_DIR}" pull
    else
      info "Cloning project from ${REPO_URL}..."
      mkdir -p "$(dirname "${INSTALL_DIR}")"
      git clone "${REPO_URL}" "${INSTALL_DIR}"
    fi
  else
    if [[ "${SOURCE_DIR}" == "${INSTALL_DIR}" ]]; then
      info "Already running from ${INSTALL_DIR}, nothing to copy."
    else
      info "Copying project files from ${SOURCE_DIR} to ${INSTALL_DIR}..."
      mkdir -p "${INSTALL_DIR}"
      rsync -a --exclude 'venv' --exclude '__pycache__' --exclude '.git' \
        "${SOURCE_DIR}/" "${INSTALL_DIR}/"
    fi
  fi
  ok "Project files are in place at ${INSTALL_DIR}."
}

verify_required_files(){
  info "Verifying required source files..."
  local missing=()

  for f in "${REQUIRED_FILES[@]}"; do
    [[ -f "${INSTALL_DIR}/${f}" ]] || missing+=("$f")
  done
  for f in "${SERVERMANAGER_REQUIRED_FILES[@]}"; do
    [[ -f "${INSTALL_DIR}/ServerManager/${f}" ]] || missing+=("ServerManager/$f")
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    err "Missing required files:"
    for f in "${missing[@]}"; do echo "   - $f"; done
    exit 1
  fi
  ok "All required source files are present."
}

setup_venv(){
  info "Creating virtual environment and installing Python packages..."
  cd "${INSTALL_DIR}"
  ${PY_BIN} -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  deactivate
  ok "Python packages installed."
}

collect_config(){
  echo
  echo "======================================"
  echo " Terminal Bot Configuration"
  echo "======================================"

  BOT_TOKEN=""
  while [[ -z "$BOT_TOKEN" ]]; do
    read -rp "Bot token (from @BotFather): " BOT_TOKEN
  done

  read -rp "Admin numeric Telegram user IDs (comma separated, optional): " ADMIN_IDS

  echo
  echo "Sponsor channel gate: require users to join channel(s) before using the bot."
  echo "Leave blank to disable this gate."
  read -rp "Sponsor channels, format 'id:title,id:title' (optional): " SPONSOR_CHANNELS
  read -rp "Sponsor channel invite links, format 'id:https://t.me/...' (optional): " SPONSOR_CHANNEL_LINKS

  echo
  CRYPTO_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  info "Generated a random CRYPTO_SECRET to encrypt stored SSH passwords at rest."
  warn "This secret is written to ${INSTALL_DIR}/.env - back it up. If it's lost or changed, previously saved server passwords can no longer be decrypted."
}

write_env_file(){
  local env_file="${INSTALL_DIR}/.env"
  if [[ -f "$env_file" ]]; then
    warn ".env already exists at ${env_file}, leaving it untouched."
    warn "Delete it first if you want this installer to regenerate it."
    return
  fi

  cat > "${env_file}" <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
SPONSOR_CHANNELS=${SPONSOR_CHANNELS}
SPONSOR_CHANNEL_LINKS=${SPONSOR_CHANNEL_LINKS}
CRYPTO_SECRET=${CRYPTO_SECRET}
EOF
  chmod 600 "${env_file}"
  ok ".env file created (restricted to root)."
}

create_systemd_service(){
  info "Creating systemd service..."
  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Terminal Bot (Telegram SSH terminal bot)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" >/dev/null
  systemctl restart "${SERVICE_NAME}"
  ok "Service created, enabled, and started."
}

save_install_dir(){ echo "${INSTALL_DIR}" > "${STATE_FILE}"; }

show_summary(){
  echo
  echo "============================================================"
  echo " Terminal Bot installed"
  echo "============================================================"
  echo " Install directory : ${INSTALL_DIR}"
  echo " Service name       : ${SERVICE_NAME}"
  echo
  echo " Check status : systemctl status ${SERVICE_NAME}"
  echo " View logs    : journalctl -u ${SERVICE_NAME} -f"
  echo " Restart      : systemctl restart ${SERVICE_NAME}"
  echo
  echo " Config file (.env) : ${INSTALL_DIR}/.env"
  echo "============================================================"
}

main(){
  require_root
  detect_python
  install_system_packages
  fetch_source
  verify_required_files
  setup_venv
  collect_config
  write_env_file
  create_systemd_service
  save_install_dir
  ok "Installation complete!"
  show_summary
}

main
