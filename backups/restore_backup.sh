#!/usr/bin/env bash
# =============================================================
# Terminal Bot - Restore From Full Backup (on the new server)
# -------------------------------------------------------------
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info(){ echo -e "${CYAN}ℹ️ $1${NC}"; }
ok(){ echo -e "${GREEN}✅ $1${NC}"; }
warn(){ echo -e "${YELLOW}⚠️ $1${NC}"; }
err(){ echo -e "${RED}❌ $1${NC}"; }

SERVICE_NAME="terminal-bot"
ARCHIVE="${1:-}"
INSTALL_DIR="${2:-/opt/terminal-bot}"

if [[ $EUID -ne 0 ]]; then
  err "This script must be run with root privileges (sudo)."
  exit 1
fi

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  err "Backup file not found. Usage: sudo bash restore_backup.sh backup.tar.gz /opt/terminal-bot"
  exit 1
fi

if [[ ! -d "$INSTALL_DIR" ]]; then
  err "Install path not found: $INSTALL_DIR — install the bot on this server first (install.sh), then re-run this script."
  exit 1
fi

WORK_DIR=$(mktemp -d)
info "Extracting backup file ..."
tar -xzf "$ARCHIVE" -C "$WORK_DIR"
STAGE_DIR=$(find "$WORK_DIR" -maxdepth 1 -type d -name "terminalbot_backup_*" | head -n1)
if [[ -z "$STAGE_DIR" ]]; then
  err "Invalid backup archive structure."
  rm -rf "$WORK_DIR"
  exit 1
fi

[[ -f "${STAGE_DIR}/BACKUP_INFO.txt" ]] && cat "${STAGE_DIR}/BACKUP_INFO.txt" && echo ""

# ---------- 1) Stop the service before restoring ----------
info "Stopping bot service temporarily ..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

# ---------- 2) Restore .env / bot_settings.json / requirements.txt ----------
if [[ -d "${STAGE_DIR}/root_files" ]]; then
  info "Restoring root-level configuration files ..."
  cp -p "${STAGE_DIR}/root_files/"* "${INSTALL_DIR}/" 2>/dev/null || true
  chmod 600 "${INSTALL_DIR}/.env" 2>/dev/null || true
  ok "Configuration files restored (.env, bot_settings.json)."
else
  err "root_files/ not found in the backup archive - nothing to restore."
  rm -rf "$WORK_DIR"
  exit 1
fi

# ---------- 3) Restore ServerManager/ local data (encrypted SSH creds, automation, known hosts) ----------
if [[ -d "${STAGE_DIR}/ServerManager" ]]; then
  info "Restoring ServerManager/ local data ..."
  mkdir -p "${INSTALL_DIR}/ServerManager"
  cp -p "${STAGE_DIR}/ServerManager/"* "${INSTALL_DIR}/ServerManager/" 2>/dev/null || true
  ok "ServerManager/ data restored."
  warn "Saved SSH server passwords/private keys only decrypt correctly if CRYPTO_SECRET in the restored .env is unchanged from the original server."
fi

# ---------- 4) Read target DB credentials from the freshly restored .env ----------
info "Reading target database credentials from .env ..."
ENV_INFO=$(python3 - "${INSTALL_DIR}/.env" <<'PYEOF'
import sys
vals = {}
with open(sys.argv[1], encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        vals[k.strip()] = v.strip()
for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
    print(vals.get(key, ""))
PYEOF
)
DB_HOST=$(sed -n '1p' <<< "$ENV_INFO")
DB_PORT=$(sed -n '2p' <<< "$ENV_INFO")
DB_NAME=$(sed -n '3p' <<< "$ENV_INFO")
DB_USER=$(sed -n '4p' <<< "$ENV_INFO")
DB_PASS=$(sed -n '5p' <<< "$ENV_INFO")

DB_HOST=${DB_HOST:-localhost}
DB_PORT=${DB_PORT:-5432}

if [[ -z "$DB_NAME" || -z "$DB_USER" ]]; then
  err "Database info in the restored .env is incomplete (DB_NAME/DB_USER missing)."
  rm -rf "$WORK_DIR"
  exit 1
fi

# ---------- 5) Create target DB/role if missing ----------
ROLE_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" || true)
if [[ "$ROLE_EXISTS" != "1" ]]; then
  info "Creating database user «${DB_USER}» ..."
  sudo -u postgres psql -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';"
else
  sudo -u postgres psql -c "ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASS}';" >/dev/null
fi

DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" || true)
if [[ "$DB_EXISTS" != "1" ]]; then
  info "Creating database «${DB_NAME}» ..."
  sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
else
  warn "Database «${DB_NAME}» already exists on this server."
  read -rp "Overwrite its current contents with the backup? (yes/no): " CONFIRM
  if [[ "$CONFIRM" != "yes" ]]; then
    err "Database restore cancelled. Other files were still restored."
    rm -rf "$WORK_DIR"
    exit 1
  fi
fi
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" >/dev/null

# ---------- 6) Restore the database itself from the dump ----------
info "Restoring database from dump (this may take a moment) ..."
DUMP_FILE="${STAGE_DIR}/database.dump"
if [[ ! -f "$DUMP_FILE" ]]; then
  err "database.dump not found inside the backup."
  rm -rf "$WORK_DIR"
  exit 1
fi

PGPASSWORD="$DB_PASS" pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
  -d "$DB_NAME" --clean --if-exists --no-owner --no-privileges "$DUMP_FILE" \
  || warn "pg_restore reported some warnings (normal if some objects didn't previously exist); continuing."

ok "Database restored."

rm -rf "$WORK_DIR"

# ---------- 7) Restart the service ----------
info "Restarting service ..."
systemctl start "${SERVICE_NAME}" 2>/dev/null || warn "${SERVICE_NAME} service not found/started — check it manually."

echo "-------------------------------------------------------------"
ok "Restore completed successfully! 🎉"
echo ""
info "Check service status:"
echo "   systemctl status ${SERVICE_NAME}"
echo "   journalctl -u ${SERVICE_NAME} -f"
