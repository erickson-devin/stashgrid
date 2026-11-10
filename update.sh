#!/usr/bin/env bash
# =============================================================
#  StashGrid update (SG-24)
#  Run from anywhere:  bash /path/to/stashgrid/update.sh
#
#  1. Back up the database
#  2. Pull the latest code (refuses if you have local edits)
#  3. Install any new Python packages
#  4. Restart every installed StashGrid service
#  5. Check the app answers
#  Schema migrations run automatically when the app restarts.
# =============================================================
set -euo pipefail

# Everything runs inside main(), so bash has read the whole script before
# `git pull` replaces this file.
main() {
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
fail()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALL_DIR"
PY="$INSTALL_DIR/venv/bin/python"
ENV_FILE="/etc/stashgrid/stashgrid.env"
SERVICES=(stashgrid stashgrid-scanner stashgrid-display)

[ -x "$PY" ] || fail "No venv at $INSTALL_DIR/venv. Run setup.sh first."

# Check before touching anything.
if ! git diff --quiet || ! git diff --cached --quiet; then
    git status --short
    fail "You have local changes in $INSTALL_DIR. Commit or discard them, then re-run."
fi

# Settings the service uses (database location). Root-only file; read without printing.
DB_PATH="$INSTALL_DIR/inventory.db"
DATABASE_URL=""
if sudo test -f "$ENV_FILE"; then
    DB_PATH="$(sudo sed -n 's/^STASHGRID_DB_PATH=//p' "$ENV_FILE" | tail -1)"
    DATABASE_URL="$(sudo sed -n 's/^STASHGRID_DATABASE_URL=//p' "$ENV_FILE" | tail -1)"
    [ -n "$DB_PATH" ] || DB_PATH="$INSTALL_DIR/inventory.db"
fi

# ── 1. Backup ────────────────────────────────────────────────
BACKUP_DIR="$INSTALL_DIR/backups"
if [[ "$DATABASE_URL" == postgres* ]]; then
    warn "Postgres database: skipping file backup (use your provider's backups or pg_dump)."
elif [ -f "$DB_PATH" ]; then
    mkdir -p "$BACKUP_DIR"
    BACKUP="$BACKUP_DIR/inventory-$(date +%Y%m%d-%H%M%S)-pre-update.db"
    # The SQLite backup API gives a consistent copy while the app is running.
    "$PY" - "$DB_PATH" "$BACKUP" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
EOF
    info "Database backed up to $BACKUP"
    # Keep the 10 newest pre-update backups.
    ls -1t "$BACKUP_DIR"/inventory-*-pre-update.db 2>/dev/null | tail -n +11 | xargs -r rm -f
else
    warn "No database at $DB_PATH yet; nothing to back up."
fi

# ── 2. Pull ──────────────────────────────────────────────────
OLD_REV="$(git rev-parse --short HEAD)"
git pull --ff-only || fail "git pull failed (diverged history?). Nothing was restarted."
NEW_REV="$(git rev-parse --short HEAD)"
if [ "$OLD_REV" = "$NEW_REV" ]; then
    info "Already up to date ($NEW_REV)"
else
    info "Updated $OLD_REV -> $NEW_REV"
    git log --oneline "$OLD_REV..$NEW_REV" | head -20
fi

# ── 3. Packages ──────────────────────────────────────────────
"$PY" -m pip install --quiet -r requirements.txt
if [[ "$DATABASE_URL" == postgres* ]] && [ -f requirements-postgres.txt ]; then
    "$PY" -m pip install --quiet -r requirements-postgres.txt
fi
info "Python packages up to date"

# ── 4. Restart ───────────────────────────────────────────────
# The kiosk display must restart too, or it keeps showing the old page (SG-24).
sudo systemctl daemon-reload
for svc in "${SERVICES[@]}"; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        sudo systemctl restart "$svc" && info "Restarted $svc" || warn "$svc failed to restart: journalctl -u $svc -n 50"
    fi
done

# ── 5. Health check ──────────────────────────────────────────
for _ in $(seq 1 30); do
    if curl -fsk -o /dev/null https://127.0.0.1:5000/login || curl -fs -o /dev/null http://127.0.0.1:5000/login; then
        info "StashGrid is up ($NEW_REV)"
        exit 0
    fi
    sleep 2
done
fail "StashGrid didn't answer on port 5000 within 60s. Check: journalctl -u stashgrid -n 50"
}

main "$@"
