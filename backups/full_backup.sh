#!/usr/bin/env bash
# =============================================================
# Terminal Bot - Full One-Click Backup
# -------------------------------------------------------------
# Backs up everything needed to move the bot to a new server:
#   - PostgreSQL database (users, plans, subscriptions,
#     wallet_transactions, payment_requests)
#   - .env (BOT_TOKEN, ADMIN_IDS, sponsor channels, DB creds,
#     and CRYPTO_SECRET - the key that decrypts saved SSH
#     passwords/private keys)
#   - bot_settings.json (sponsor channel list, payment card info,
#     health-monitor thresholds)
#   - ServerManager/ local data: server_manager_settings.json
#     (encrypted SSH server credentials), server_manager_automation.json
#     (automation rules), known_hosts.json
# -------------------------------------------------------------
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info(){ echo -e "${CYAN}ℹ️ $1${NC}"; }
ok(){ echo -e "${GREEN}✅ $1${NC}"; }
warn(){ echo -e "${YELLOW}⚠️ $1${NC}"; }
err(){ echo -e "${RED}❌ $1${NC}"; }

SERVICE_NAME="terminal-bot"
INSTALL_DIR="${1:-/opt/terminal-bot}"
OUT_DIR="${2:-/opt/backups}"

if [[ $EUID -ne 0 ]]; then
  err "This script must be run with root privileges (sudo)."
  exit 1
fi

if [[ ! -d "$INSTALL_DIR" ]]; then
  err "Install path not found: $INSTALL_DIR"
  echo "Usage: sudo bash full_backup.sh /opt/terminal-bot /opt/backups"
  exit 1
fi

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  err ".env not found in $INSTALL_DIR. Is the install path correct?"
  exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
WORK_DIR=$(mktemp -d)
STAGE_DIR="${WORK_DIR}/terminalbot_backup_${TIMESTAMP}"
mkdir -p "$STAGE_DIR"
mkdir -p "$OUT_DIR"

info "Starting full backup of: $INSTALL_DIR"
echo "-------------------------------------------------------------"

# ---------- 1) Read DB credentials (and check CRYPTO_SECRET) from .env ----------
info "Reading configuration from .env ..."
ENV_INFO=$(python3 - "$INSTALL_DIR/.env" <<'PYEOF'
import sys
vals = {}
with open(sys.argv[1], encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        vals[k.strip()] = v.strip()
for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "CRYPTO_SECRET"):
    print(vals.get(key, ""))
PYEOF
)
DB_HOST=$(sed -n '1p' <<< "$ENV_INFO")
DB_PORT=$(sed -n '2p' <<< "$ENV_INFO")
DB_NAME=$(sed -n '3p' <<< "$ENV_INFO")
DB_USER=$(sed -n '4p' <<< "$ENV_INFO")
DB_PASS=$(sed -n '5p' <<< "$ENV_INFO")
CRYPTO_SECRET_PRESENT=$(sed -n '6p' <<< "$ENV_INFO")

DB_HOST=${DB_HOST:-localhost}
DB_PORT=${DB_PORT:-5432}

if [[ -z "$DB_NAME" || -z "$DB_USER" ]]; then
  err "Database info in .env is incomplete (DB_NAME/DB_USER missing)."
  rm -rf "$WORK_DIR"
  exit 1
fi

if [[ -z "$CRYPTO_SECRET_PRESENT" ]]; then
  warn "CRYPTO_SECRET is empty in .env - encrypted SSH passwords/keys (if any) cannot be decrypted anyway."
fi

# ---------- 2) Full database dump (custom format -> restored with pg_restore) ----------
info "Dumping full database «$DB_NAME» ..."
DB_DUMP_FILE="${STAGE_DIR}/database.dump"
if PGPASSWORD="$DB_PASS" pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
      -Fc -f "$DB_DUMP_FILE" "$DB_NAME"; then
  ok "Database dump created ($(du -h "$DB_DUMP_FILE" | cut -f1))"
else
  err "pg_dump failed. Backup aborted."
  rm -rf "$WORK_DIR"
  exit 1
fi

# Also keep a plain-text SQL copy for manual review; the .dump file above is
# what restore_backup.sh actually uses.
PGPASSWORD="$DB_PASS" pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
      "$DB_NAME" > "${STAGE_DIR}/database.sql" 2>/dev/null || \
  warn "Could not create plain-text SQL dump; the binary .dump file is sufficient for restore."

# ---------- 3) Root-level config files ----------
info "Copying configuration files ..."
mkdir -p "${STAGE_DIR}/root_files"
for f in .env bot_settings.json requirements.txt; do
  if [[ -f "${INSTALL_DIR}/${f}" ]]; then
    cp -p "${INSTALL_DIR}/${f}" "${STAGE_DIR}/root_files/"
    ok "Copied: $f"
  fi
done

# ---------- 4) ServerManager/ local data (encrypted SSH creds, automation rules, known hosts) ----------
if [[ -d "${INSTALL_DIR}/ServerManager" ]]; then
  info "Copying ServerManager/ local data ..."
  mkdir -p "${STAGE_DIR}/ServerManager"
  found_any=0
  for f in server_manager_settings.json server_manager_automation.json known_hosts.json; do
    if [[ -f "${INSTALL_DIR}/ServerManager/${f}" ]]; then
      cp -p "${INSTALL_DIR}/ServerManager/${f}" "${STAGE_DIR}/ServerManager/"
      ok "Copied: ServerManager/$f"
      found_any=1
    fi
  done
  [[ "$found_any" -eq 0 ]] && warn "No ServerManager/*.json data files found yet (nothing saved there)."
else
  warn "ServerManager/ directory not found."
fi

# ---------- 5) Backup metadata ----------
cat > "${STAGE_DIR}/BACKUP_INFO.txt" <<EOF
Terminal Bot - Full Backup
Date: $(date '+%Y-%m-%d %H:%M:%S')
Source install path: ${INSTALL_DIR}
Database name: ${DB_NAME}
Database host: ${DB_HOST}:${DB_PORT}
Database user: ${DB_USER}

IMPORTANT: this archive includes .env, which contains CRYPTO_SECRET - the
key that decrypts SSH passwords/private keys stored in
ServerManager/server_manager_settings.json. Restoring on a new server
WITHOUT this exact .env makes any previously saved SSH credentials
permanently unreadable (they will NOT be re-encrypted automatically).

To restore on a new server:
  1) Install the project there first (this creates backups/restore_backup.sh):
       bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/Terminal_Bot/main/install.sh)
  2) Copy this single file (terminalbot_backup_*.tar.gz) to the new server,
     e.g. into /root/.
  3) cd into the backups folder and run restore_backup.sh with the FULL
     path to wherever you copied the archive:
       cd ${INSTALL_DIR}/backups
       sudo bash restore_backup.sh /root/terminalbot_backup_*.tar.gz ${INSTALL_DIR}
EOF

# ---------- 6) Compress everything into one final archive ----------
FINAL_ARCHIVE="${OUT_DIR}/terminalbot_backup_${TIMESTAMP}.tar.gz"
info "Building final compressed archive ..."
tar -czf "$FINAL_ARCHIVE" -C "$WORK_DIR" "terminalbot_backup_${TIMESTAMP}"

# Restricted permissions since it contains secrets and CRYPTO_SECRET
chmod 600 "$FINAL_ARCHIVE"

rm -rf "$WORK_DIR"

echo "-------------------------------------------------------------"
ok "Full backup created successfully:"
echo "   📦 ${FINAL_ARCHIVE} ($(du -h "$FINAL_ARCHIVE" | cut -f1))"
echo ""
warn "This file contains the bot token, DB password, and CRYPTO_SECRET — transfer it only over a secure channel (scp/sftp)."
echo ""
info "To restore on a new server:"
echo "   1) Install the project there first (this creates backups/restore_backup.sh):"
echo "        bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/Terminal_Bot/main/install.sh)"
echo "   2) Copy this archive over:"
echo "        scp ${FINAL_ARCHIVE} root@NEW_SERVER_IP:/root/"
echo "   3) On the new server, cd into the backups folder and run restore_backup.sh"
echo "      with the FULL path to the archive you copied:"
echo "        cd ${INSTALL_DIR}/backups"
echo "        sudo bash restore_backup.sh /root/$(basename "$FINAL_ARCHIVE") ${INSTALL_DIR}"
