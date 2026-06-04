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

# ── 5. Web app systemd service ───────────────────────────────
SERVICE_FILE="$SCRIPT_DIR/stashgrid.service"
SYSTEMD_PATH="/etc/systemd/system/stashgrid.service"

if [ -f "$SERVICE_FILE" ]; then
    info "Installing web app systemd service..."

    # Generate a cryptographically secure secret key for Flask session signing.
    # A fresh key is created on every install — sessions from previous installs
    # will be invalidated (users will need to log in again), which is intentional.
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    info "Generated new STASHGRID_SECRET_KEY (64-char hex)"
    warn "Record this key if you need to restore it manually:"
    warn "  STASHGRID_SECRET_KEY=$SECRET_KEY"

    # Patch the service file with the actual install paths before copying
    sed \
        -e "s|__INSTALL_DIR__|$SCRIPT_DIR|g" \
        -e "s|__VENV_DIR__|$VENV_DIR|g" \
        -e "s|__USER__|$USER|g" \
        -e "s|__SECRET_KEY__|$SECRET_KEY|g" \
        "$SERVICE_FILE" | sudo tee "$SYSTEMD_PATH" > /dev/null

    sudo systemctl daemon-reload
    sudo systemctl enable stashgrid.service
    info "Web app service installed and enabled (starts on boot)"
else
    warn "stashgrid.service template not found — skipping web service setup"
fi

# ── 6. USB barcode scanner service (optional) ─────────────────
SCANNER_SERVICE_FILE="$SCRIPT_DIR/stashgrid-scanner.service"
SCANNER_SYSTEMD_PATH="/etc/systemd/system/stashgrid-scanner.service"

echo ""
read -rp $'[?] Set up the USB hardware barcode scanner service? [y/N]: ' SETUP_SCANNER
SETUP_SCANNER="${SETUP_SCANNER:-N}"

if [[ "$SETUP_SCANNER" =~ ^[Yy]$ ]]; then
    # Verify scanner.py exists
    if [ ! -f "$SCRIPT_DIR/scanner.py" ]; then
        warn "scanner.py not found — skipping scanner service"
    else
        # The user needs to be in the 'input' group to read /dev/input/ without sudo
        if ! groups "$USER" | grep -qw input; then
            info "Adding $USER to the 'input' group (required for /dev/input/ access)..."
            sudo usermod -aG input "$USER"
            warn "Group change takes effect after you LOG OUT and back in (or reboot)."
            warn "The scanner service will fail until then — run: sudo systemctl start stashgrid-scanner"
        else
            info "User $USER is already in the 'input' group"
        fi

        # Check / warn about the hardcoded device path in scanner.py
        SCANNER_DEVICE=$(grep 'SCANNER_DEVICE' "$SCRIPT_DIR/scanner.py" | head -1 | cut -d'"' -f2)
        warn "Scanner device is set to: $SCANNER_DEVICE"
        warn "If your scanner appears at a different path, edit SCANNER_DEVICE in scanner.py"
        warn "List available devices with: ls /dev/input/by-id/"

        if [ -f "$SCANNER_SERVICE_FILE" ]; then
            info "Installing USB scanner systemd service..."
            sed \
                -e "s|__INSTALL_DIR__|$SCRIPT_DIR|g" \
                -e "s|__VENV_DIR__|$VENV_DIR|g" \
                -e "s|__USER__|$USER|g" \
                "$SCANNER_SERVICE_FILE" | sudo tee "$SCANNER_SYSTEMD_PATH" > /dev/null

            sudo systemctl daemon-reload
            sudo systemctl enable stashgrid-scanner.service
            info "Scanner service installed and enabled (starts on boot)"
        else
            warn "stashgrid-scanner.service template not found — skipping"
        fi
    fi
else
    info "Skipping scanner service setup (you can re-run setup.sh to add it later)"
fi

# ── 7. Cloudflare Tunnel (optional) ──────────────────────────
echo ""
read -rp $'[?] Set up the Cloudflare tunnel for public shopping list access? [y/N]: ' SETUP_CF
SETUP_CF="${SETUP_CF:-N}"

SHOPPING_URL=""

if [[ "$SETUP_CF" =~ ^[Yy]$ ]]; then
    # ── Install cloudflared if not present ──
    if ! command -v cloudflared &>/dev/null; then
        info "Installing cloudflared from Cloudflare's package repo..."
        curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
            | sudo tee /usr/share/keyrings/cloudflare-archive-keyring.gpg >/dev/null
        echo "deb [signed-by=/usr/share/keyrings/cloudflare-archive-keyring.gpg] \
https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
            | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
        sudo apt-get update -qq
        sudo apt-get install -y cloudflared || error "Failed to install cloudflared"
        info "cloudflared installed: $(cloudflared --version)"
    else
        info "cloudflared already installed: $(cloudflared --version)"
    fi

    # ── Authenticate ──
    warn "A browser window will open — log in to Cloudflare and authorize the tunnel."
    warn "If running headless (SSH), copy the URL that appears and open it on another device."
    echo ""
    cloudflared tunnel login || error "Cloudflare authentication failed"

    # ── Create tunnel ──
    TUNNEL_NAME="stashgrid"
    info "Creating tunnel '$TUNNEL_NAME'..."
    CF_OUTPUT=$(cloudflared tunnel create "$TUNNEL_NAME" 2>&1)
    echo "$CF_OUTPUT"

    # Extract the tunnel UUID from the output line: "Created tunnel stashgrid with id <uuid>"
    TUNNEL_ID=$(echo "$CF_OUTPUT" | grep -oP '(?<=with id )[a-f0-9\-]+' | head -1)
    if [ -z "$TUNNEL_ID" ]; then
        warn "Could not auto-detect tunnel ID from output above."
        read -rp "[?] Paste the tunnel UUID manually: " TUNNEL_ID
    fi
    info "Tunnel ID: $TUNNEL_ID"

    # ── Deploy config file ──
    CF_CONFIG_DIR="$HOME/.cloudflared"
    mkdir -p "$CF_CONFIG_DIR"

    CF_CONFIG_DEST="$CF_CONFIG_DIR/config.yml"
    CF_CONFIG_SRC="$SCRIPT_DIR/cloudflared_config.yml"

    if [ -f "$CF_CONFIG_SRC" ]; then
        sed \
            -e "s|<your-tunnel-id>|$TUNNEL_ID|g" \
            "$CF_CONFIG_SRC" > "$CF_CONFIG_DEST"
        info "Config written to $CF_CONFIG_DEST"
    else
        warn "cloudflared_config.yml not found in $SCRIPT_DIR — writing minimal config"
        cat > "$CF_CONFIG_DEST" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CF_CONFIG_DIR/$TUNNEL_ID.json

ingress:
  - hostname: stashgrid.devinerickson.com
    path: "^/shop/[a-f0-9]{32}$"
    service: http://localhost:5000
  - hostname: stashgrid.devinerickson.com
    path: "^/api/public/shopping-lists/[a-f0-9]{32}$"
    service: http://localhost:5000
  - hostname: stashgrid.devinerickson.com
    path: "^/api/public/shopping-lists/toggle/[a-f0-9]{32}/[0-9]+$"
    service: http://localhost:5000
  - service: http_status:404
EOF
    fi

    # ── DNS route ──
    info "Creating DNS CNAME route for stashgrid.devinerickson.com..."
    cloudflared tunnel route dns "$TUNNEL_NAME" stashgrid.devinerickson.com \
        || warn "DNS route may already exist — continuing"

    # ── Install cloudflared as a systemd service ──
    info "Installing cloudflared as a systemd service..."
    sudo cloudflared service install || warn "Service install may have partially failed — check manually"
    sudo systemctl enable cloudflared
    info "Cloudflare tunnel service installed and enabled (starts on boot)"

    # ── Read the shopping token so we can print the URL ──
    TOKEN_FILE="$SCRIPT_DIR/shopping_token.txt"
    if [ -f "$TOKEN_FILE" ]; then
        SHOPPING_TOKEN=$(cat "$TOKEN_FILE")
        SHOPPING_URL="https://stashgrid.devinerickson.com/shop/$SHOPPING_TOKEN"
        info "Public shopping list URL: $SHOPPING_URL"
    else
        warn "shopping_token.txt not found yet — start StashGrid once to generate it, then check the log for the URL."
    fi
else
    info "Skipping Cloudflare tunnel setup (you can re-run setup.sh to add it later)"
fi

# ── 8. Start services ─────────────────────────────────────────
echo ""
read -rp $'[?] Start StashGrid services now? [Y/n]: ' START_NOW
START_NOW="${START_NOW:-Y}"
if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
    if [ -f "$SYSTEMD_PATH" ]; then
        sudo systemctl start stashgrid.service
        sleep 2
        sudo systemctl status stashgrid.service --no-pager || true
    fi
    if [ -f "$SCANNER_SYSTEMD_PATH" ] && [[ "$SETUP_SCANNER" =~ ^[Yy]$ ]]; then
        sudo systemctl start stashgrid-scanner.service || warn "Scanner service failed to start (check device path / group membership)"
    fi
fi

echo ""
echo "=================================================="
echo "   Setup complete!"
echo ""
echo "   Web App"
echo "   Start:   sudo systemctl start stashgrid"
echo "   Stop:    sudo systemctl stop stashgrid"
echo "   Restart: sudo systemctl restart stashgrid"
echo "   Logs:    journalctl -u stashgrid -f"
echo "            tail -f /var/log/stashgrid/stashgrid.log"
echo ""
echo "   USB Scanner"
echo "   Start:   sudo systemctl start stashgrid-scanner"
echo "   Stop:    sudo systemctl stop stashgrid-scanner"
echo "   Logs:    journalctl -u stashgrid-scanner -f"
echo ""
echo "   Device paths:  ls /dev/input/by-id/"
if [[ "$SETUP_CF" =~ ^[Yy]$ ]]; then
echo ""
echo "   Cloudflare Tunnel"
echo "   Start:   sudo systemctl start cloudflared"
echo "   Stop:    sudo systemctl stop cloudflared"
echo "   Status:  sudo systemctl status cloudflared"
echo "   Logs:    journalctl -u cloudflared -f"
if [ -n "$SHOPPING_URL" ]; then
echo ""
echo "   ★ Public Shopping List URL (bookmark this):"
echo "   $SHOPPING_URL"
fi
fi
echo "=================================================="
echo ""
