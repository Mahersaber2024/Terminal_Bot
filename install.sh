#!/usr/bin/env bash
# =============================================================
# Terminal Bot - Installer
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/Terminal_Bot/main/install.sh)
# =============================================================
set -euo pipefail

REPO_URL="https://github.com/Mahersaber2024/Terminal_Bot.git"
SERVICE_NAME="terminal-bot"
DEFAULT_INSTALL_DIR="/opt/terminal-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
STATE_FILE="/etc/${SERVICE_NAME}.install_dir"

REQUIRED_FILES=(
  "main.py"
  "config.py"
  "crypto_utils.py"
  "sponsor_gate.py"
  "bot_settings.py"
  "subscription.py"
  "proxy_utils.py"
  "requirements.txt"
)

# admin/ package (main.py does "from admin import admin") - no __init__.py
# required either, same as db/ and ServerManager/ below: Python treats it
# as an implicit namespace package (3.3+).
ADMIN_REQUIRED_FILES=(
  "admin.py"
)

# db/ package - main.py/admin.py/subscription.py do "from db.database
# import get_db" (get_db() lives directly in database.py, next to the
# Database class). db/setup_db.py inserts the repo root onto sys.path
# itself before "import config" (see its own docstring), so it works
# whether run as "python3 db/setup_db.py" or "python3 -m db.setup_db".
# config.py used to be duplicated as its own db/config.py - that file is
# gone now, merged into the single top-level config.py (checked via
# REQUIRED_FILES above).
# db/ has no __init__.py by design - Python treats it as an implicit
# namespace package (3.3+), so no package-init file is needed at all.
DB_REQUIRED_FILES=(
  "database.py"
  "setup_db.py"
)

# ServerManager/ package - names match main.py's actual imports
# ("from ServerManager import handlers as svm / health as svm_health /
# automation as svm_auto"), plus the internal-only modules (engine,
# settings, maintenance) those depend on. No __init__.py required - same
# implicit namespace package as admin/ and db/ above.
SERVERMANAGER_REQUIRED_FILES=(
  "handlers.py"
  "engine.py"
  "settings.py"
  "health.py"
  "automation.py"
  "maintenance.py"
)

# ------------------ Colors ------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info(){ echo -e "${CYAN}ℹ️  $1${NC}"; }
ok(){ echo -e "${GREEN}✅ $1${NC}"; }
warn(){ echo -e "${YELLOW}⚠️  $1${NC}"; }
err(){ echo -e "${RED}❌ $1${NC}"; }
press_enter(){ read -rp "Press Enter to continue..." _ || true; }

require_root(){
  if [[ $EUID -ne 0 ]]; then
    err "This script must be run with root privileges (using sudo or as root user)."
    exit 1
  fi
}

save_install_dir(){ echo "${INSTALL_DIR}" > "${STATE_FILE}"; }
load_install_dir(){
  if [[ -f "${STATE_FILE}" ]]; then
    INSTALL_DIR=$(cat "${STATE_FILE}")
  else
    INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
  fi
}

# ------------------ Steps ------------------
install_dir_prompt(){
  read -rp "Installation path [${DEFAULT_INSTALL_DIR}]: " INSTALL_DIR
  INSTALL_DIR=${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}
}

detect_python(){
  if command -v python3 &>/dev/null; then
    PY_BIN=python3
  else
    err "Python 3 not found on the server."
    exit 1
  fi
}

install_system_packages(){
  info "Installing system dependencies..."
  # iputils-ping: ServerManager/engine.py's quick-ping feature shells out
  # to "ping" directly, without SSH credentials.
  # libpq-dev: needed to build psycopg2 from source if no prebuilt wheel
  # is available for this server's architecture/Python version.
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip python3-dev git curl libpq-dev iputils-ping
  ok "System dependencies installed."
}

install_postgresql(){
  info "Installing PostgreSQL..."
  apt-get install -y postgresql postgresql-contrib
  systemctl enable postgresql
  systemctl start postgresql
  ok "PostgreSQL installed and started."
}

# Collects/creates the PostgreSQL role + database used by config.py
# (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD in .env), mirroring the
# nomone.py sample installer's setup_database() step.
setup_database(){
  echo
  echo "======================================"
  echo " Database Setup"
  echo "======================================"
  read -rp "Do you want to install and configure PostgreSQL on this server? (y/n) [y]: " INSTALL_DB
  INSTALL_DB=${INSTALL_DB:-y}

  if [[ "$INSTALL_DB" =~ ^[Yy]$ ]]; then
    if ! command -v psql &>/dev/null; then
      install_postgresql
    else
      info "PostgreSQL is already installed on the server."
      systemctl enable postgresql &>/dev/null || true
      systemctl start postgresql &>/dev/null || true
    fi

    read -rp "Database name [terminal_bot]: " DB_NAME
    DB_NAME=${DB_NAME:-terminal_bot}
    read -rp "Database username [terminal_bot]: " DB_USER
    DB_USER=${DB_USER:-terminal_bot}
    DB_HOST="localhost"
    DB_PORT="5432"

    # ---- Check for existing role/database ----
    ROLE_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'")
    DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'")

    if [[ "$ROLE_EXISTS" == "1" || "$DB_EXISTS" == "1" ]]; then
      warn "User «${DB_USER}» or database «${DB_NAME}» already exists on this server."
      echo " 1) Use the existing database/user (no password change)"
      echo " 2) Reset password for the existing user"
      echo " 3) Enter a new username/database (create a new set)"
      read -rp "Your choice [1]: " DB_EXIST_CHOICE
      DB_EXIST_CHOICE=${DB_EXIST_CHOICE:-1}
      case "$DB_EXIST_CHOICE" in
        1)
          DB_PASS=""
          while [[ -z "$DB_PASS" ]]; do
            read -rsp "Enter the current password for «${DB_USER}» (only for saving in .env, nothing will change in the database): " DB_PASS
            echo
          done
          ok "Using existing database/user; no password was changed."
          return
          ;;
        2)
          DB_PASS=""
          while [[ -z "$DB_PASS" ]]; do
            read -rsp "New password for user «${DB_USER}»: " DB_PASS
            echo
          done
          sudo -u postgres psql -c "ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASS}';" >/dev/null
          [[ "$DB_EXISTS" != "1" ]] && sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
          sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" >/dev/null
          ok "Password has been updated."
          return
          ;;
        3)
          setup_database # Start over with new names
          return
          ;;
      esac
    fi

    # ---- Normal path: nothing exists yet ----
    DB_PASS=""
    while [[ -z "$DB_PASS" ]]; do
      read -rsp "Password for database user (required): " DB_PASS
      echo
    done
    info "Creating user and database in PostgreSQL..."
    sudo -u postgres psql -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';"
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" >/dev/null
    ok "Database «${DB_NAME}» and user «${DB_USER}» have been created."
  else
    info "Please enter existing database connection details:"
    read -rp "Database host: " DB_HOST
    read -rp "Database port [5432]: " DB_PORT
    DB_PORT=${DB_PORT:-5432}
    read -rp "Database name: " DB_NAME
    read -rp "Database username: " DB_USER
    read -rsp "Database password: " DB_PASS
    echo
  fi
}

clone_or_update_repo(){
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Project already exists, updating..."
    git -C "${INSTALL_DIR}" pull
    ok "Project code is ready."
    return
  fi

  local env_backup=""
  if [[ -d "${INSTALL_DIR}" ]] && [[ -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null)" ]]; then
    warn "${INSTALL_DIR} already exists (from a previous run) but isn't a git checkout - clearing it first."
    if [[ -f "${INSTALL_DIR}/.env" ]]; then
      env_backup=$(mktemp)
      cp "${INSTALL_DIR}/.env" "$env_backup"
    fi
    # Fully removed (not recreated) - git clone needs a nonexistent or
    # empty target directory, so we let git clone create it fresh below.
    rm -rf "${INSTALL_DIR:?}"
  fi

  info "Cloning project from GitHub..."
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  git clone "${REPO_URL}" "${INSTALL_DIR}"

  if [[ -n "$env_backup" ]]; then
    mv "$env_backup" "${INSTALL_DIR}/.env"
    info "Restored previous .env file."
  fi
  ok "Project code is ready."
}

verify_required_files(){
  info "Verifying required source files..."
  local missing=()

  for f in "${REQUIRED_FILES[@]}"; do
    [[ -f "${INSTALL_DIR}/${f}" ]] || missing+=("$f")
  done
  for f in "${ADMIN_REQUIRED_FILES[@]}"; do
    [[ -f "${INSTALL_DIR}/admin/${f}" ]] || missing+=("admin/$f")
  done
  for f in "${DB_REQUIRED_FILES[@]}"; do
    [[ -f "${INSTALL_DIR}/db/${f}" ]] || missing+=("db/$f")
  done
  for f in "${SERVERMANAGER_REQUIRED_FILES[@]}"; do
    [[ -f "${INSTALL_DIR}/ServerManager/${f}" ]] || missing+=("ServerManager/$f")
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    err "The following required files are missing from the repository:"
    for f in "${missing[@]}"; do echo " - $f"; done
    err "Please make sure these files are committed and pushed to: ${REPO_URL}"
    err "Then run this installer again (option 2: Update bot)."
    exit 1
  fi

  ok "All required source files are present."
}

setup_venv(){
  info "Creating Python virtual environment and installing packages..."
  cd "${INSTALL_DIR}"
  ${PY_BIN} -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  # proxy_utils.py (direct-connect / multi-proxy fallback support) needs
  # httpx directly. python-telegram-bot already depends on it, so this is
  # normally a no-op, but pin it explicitly so a proxy_utils import never
  # fails on a requirements.txt that hasn't been updated yet.
  pip install "httpx>=0.24" -q
  deactivate
  ok "Python packages installed."
}

run_db_setup_script(){
  # db/setup_db.py reads DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD from
  # .env via the repo-root config.py, so this must run after
  # write_env_file() and after the venv (with psycopg2) is ready. It
  # inserts the repo root onto sys.path itself (see its own docstring), so
  # running it as "python3 db/setup_db.py" from the repo root works fine.
  if [[ -f "${INSTALL_DIR}/db/setup_db.py" ]]; then
    info "Creating database tables..."
    cd "${INSTALL_DIR}"
    source venv/bin/activate
    python3 db/setup_db.py --auto || warn "Automatic table creation failed; you can run 'python3 db/setup_db.py' manually later"
    deactivate
  else
    warn "db/setup_db.py not found; you need to create tables manually."
  fi
}

collect_bot_config(){
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
  echo "Proxy support: the bot always tries connecting to Telegram directly first"
  echo "(3 attempts) - proxies are only used as a fallback if that keeps failing."
  echo "Format: comma-separated, e.g. http://user:pass@host1:8080,socks5://host2:1080"
  read -rp "Proxy URLs (optional, leave blank to skip): " TELEGRAM_PROXY_URLS

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
DB_HOST=${DB_HOST}
DB_PORT=${DB_PORT}
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASS}
TELEGRAM_PROXY_URLS=${TELEGRAM_PROXY_URLS}
EOF
  chmod 600 "${env_file}"
  ok ".env file created (restricted access)."
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

show_summary(){
  echo
  echo "============================================================"
  echo " ✅ Terminal Bot installed"
  echo "============================================================"
  echo " Install directory : ${INSTALL_DIR}"
  echo " Service name       : ${SERVICE_NAME}"
  echo
  echo " Check status : systemctl status ${SERVICE_NAME}"
  echo " View logs    : journalctl -u ${SERVICE_NAME} -f"
  echo " Restart      : systemctl restart ${SERVICE_NAME}"
  echo
  echo " Config file (.env) : ${INSTALL_DIR}/.env"
  echo
  echo " Database : ${DB_NAME:-N/A} @ ${DB_HOST:-N/A}:${DB_PORT:-N/A} (user: ${DB_USER:-N/A})"
  echo "============================================================"
}

# ============================================================
# ========== Main Functions ==========
# ============================================================
full_install(){
  require_root
  detect_python
  install_dir_prompt
  install_system_packages
  setup_database
  clone_or_update_repo
  verify_required_files
  setup_venv
  collect_bot_config
  write_env_file
  run_db_setup_script
  create_systemd_service
  save_install_dir
  ok "✅ Installation completed successfully! 🎉"
  show_summary
  press_enter
}

update_bot(){
  require_root
  load_install_dir
  if [[ ! -d "${INSTALL_DIR}" ]]; then
    read -rp "Enter current installation path: " INSTALL_DIR
  fi
  detect_python
  clone_or_update_repo
  verify_required_files
  setup_venv
  run_db_setup_script
  systemctl restart "${SERVICE_NAME}"
  save_install_dir
  ok "Update completed and service restarted."
}

restart_service(){
  require_root
  systemctl restart "${SERVICE_NAME}"
  ok "Service restarted."
}

view_logs(){
  journalctl -u "${SERVICE_NAME}" -f --no-pager -n 100
}

show_status(){
  systemctl status "${SERVICE_NAME}" --no-pager || true
  press_enter
}

uninstall_bot(){
  require_root
  warn "This will remove the service and bot files."
  read -rp "Are you sure? (yes/no): " CONFIRM
  if [[ "$CONFIRM" != "yes" ]]; then
    info "Cancelled."
    return
  fi

  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "${SERVICE_FILE}"
  systemctl daemon-reload

  load_install_dir
  read -rp "Installation path to remove [${INSTALL_DIR}]: " DEL_DIR
  DEL_DIR=${DEL_DIR:-$INSTALL_DIR}

  read -rp "Also drop the PostgreSQL database? (y/n) [n]: " DROP_DB
  DROP_DB=${DROP_DB:-n}
  if [[ "$DROP_DB" =~ ^[Yy]$ ]] && command -v psql &>/dev/null; then
    read -rp "Database name to delete: " DB_NAME_DEL
    read -rp "Database username to delete: " DB_USER_DEL
    sudo -u postgres psql -c "DROP DATABASE IF EXISTS ${DB_NAME_DEL};" || true
    sudo -u postgres psql -c "DROP ROLE IF EXISTS ${DB_USER_DEL};" || true
    ok "Database deleted."
  fi

  if [[ -d "$DEL_DIR" ]]; then
    rm -rf "$DEL_DIR"
    ok "Installation files removed."
  fi

  rm -f "${STATE_FILE}"
  ok "Bot uninstallation completed."
}

main_menu(){
  while true; do
    echo
    echo "======================================"
    echo " 🚀 Terminal Bot - Installer"
    echo "======================================"
    echo "1) Full installation"
    echo "2) Update bot"
    echo "3) Restart service"
    echo "4) View logs"
    echo "5) Service status"
    echo "6) Complete uninstall"
    echo "0) Exit"
    echo "======================================"
    read -rp "Enter option number: " CHOICE
    case "$CHOICE" in
      1) full_install ;;
      2) update_bot; press_enter ;;
      3) restart_service; press_enter ;;
      4) view_logs ;;
      5) show_status ;;
      6) uninstall_bot; press_enter ;;
      0) exit 0 ;;
      *) warn "Invalid option." ;;
    esac
  done
}

main_menu
