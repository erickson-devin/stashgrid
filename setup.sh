#!/usr/bin/env bash
# =============================================================
#  StashGrid Setup Script
#  Run once after cloning: bash setup.sh
#  Requires: Raspberry Pi OS (or any Debian-based Linux)
# =============================================================
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[+]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ── Resolve script location (works even if called from elsewhere) ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "=================================================="
echo "   StashGrid — Automated Setup"
echo "   $(date)"
echo "=================================================="
echo ""

# ── 1. System prerequisites ───────────────────────────────────
info "Checking system prerequisites..."
if ! command -v python3 &>/dev/null; then
    error "python3 not found. Install it with: sudo apt install python3"
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Found Python $PYTHON_VERSION"

# python3-venv is a separate package on Debian/Pi OS
if ! python3 -c "import venv" &>/dev/null; then
    warn "python3-venv not found. Installing..."
    sudo apt-get install -y python3-venv python3-dev || error "Could not install python3-venv"
fi

# evdev needs kernel headers to build
if ! dpkg -l python3-dev &>/dev/null 2>&1; then
    warn "python3-dev not found. Installing (needed by evdev)..."
    sudo apt-get install -y python3-dev || warn "Could not install python3-dev — evdev may fail to build"
fi

# ── 2. Virtual environment ────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
    warn "venv already exists at $VENV_DIR — skipping creation"
else
    info "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

info "Activating venv..."
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 3. Install Python dependencies ───────────────────────────
info "Installing Python dependencies from requirements.txt..."
pip install --upgrade pip --quiet
pip install -r "$SCRIPT_DIR/requirements.txt" || error "pip install failed"
info "All Python packages installed successfully"

# ── 4. Log directory ──────────────────────────────────────────
LOG_DIR="/var/log/stashgrid"
if [ ! -d "$LOG_DIR" ]; then
    info "Creating log directory at $LOG_DIR..."
    sudo mkdir -p "$LOG_DIR"
    sudo chown "$USER":"$USER" "$LOG_DIR"
else
    info "Log directory $LOG_DIR already exists"
fi

# ── 5. Systemd service ────────────────────────────────────────
SERVICE_FILE="$SCRIPT_DIR/stashgrid.service"
SYSTEMD_PATH="/etc/systemd/system/stashgrid.service"

if [ -f "$SERVICE_FILE" ]; then
    info "Installing systemd service..."
    # Patch the service file with the actual install paths before copying
    sed \
        -e "s|__INSTALL_DIR__|$SCRIPT_DIR|g" \
        -e "s|__VENV_DIR__|$VENV_DIR|g" \
        -e "s|__USER__|$USER|g" \
        "$SERVICE_FILE" | sudo tee "$SYSTEMD_PATH" > /dev/null

    sudo systemctl daemon-reload
    sudo systemctl enable stashgrid.service
    info "Service installed and enabled (starts on boot)"

    read -rp $'\n[?] Start StashGrid now? [Y/n]: ' START_NOW
    START_NOW="${START_NOW:-Y}"
    if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
        sudo systemctl start stashgrid.service
        sleep 2
        sudo systemctl status stashgrid.service --no-pager || true
    fi
else
    warn "stashgrid.service template not found — skipping systemd setup"
    warn "You can start manually with: source venv/bin/activate && python app.py"
fi

echo ""
echo "=================================================="
echo "   Setup complete!"
echo ""
echo "   Start:   sudo systemctl start stashgrid"
echo "   Stop:    sudo systemctl stop stashgrid"
echo "   Restart: sudo systemctl restart stashgrid"
echo "   Logs:    journalctl -u stashgrid -f"
echo "            tail -f /var/log/stashgrid/stashgrid.log"
echo "=================================================="
echo ""
