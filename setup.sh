#!/usr/bin/env bash
# =============================================================
#  StashGrid setup (SG-24)
#  Run after cloning, and again any time to add a part or repair:
#      bash setup.sh
#  Safe to re-run: it keeps your settings, secret key and data.
#
#  Options (otherwise it asks):
#      --scanner / --no-scanner     USB barcode scanner service
#      --display / --no-display     full-screen kiosk on an attached screen
#      --tunnel HOSTNAME / --no-tunnel
#                                   Cloudflare tunnel for the shopping link
#      --start / --no-start         start the services at the end
#      -y, --yes                    don't ask; anything not given is "no"
#                                   (services are still started)
#  Requires: Raspberry Pi OS / Debian.
# =============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
RUN_USER="${SUDO_USER:-$USER}"
[ "$RUN_USER" != "root" ] || error "Run this as your normal user (it uses sudo where needed), not as root."
RUN_UID="$(id -u "$RUN_USER")"

SETUP_SCANNER=""; SETUP_DISPLAY=""; TUNNEL_HOST=""; SETUP_TUNNEL=""; START_NOW=""; ASSUME_YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --scanner) SETUP_SCANNER=y ;;     --no-scanner) SETUP_SCANNER=n ;;
        --display) SETUP_DISPLAY=y ;;     --no-display) SETUP_DISPLAY=n ;;
        --tunnel) SETUP_TUNNEL=y; TUNNEL_HOST="${2:-}"; shift
                  [ -n "$TUNNEL_HOST" ] || error "--tunnel needs a hostname, e.g. --tunnel stash.example.com" ;;
        --no-tunnel) SETUP_TUNNEL=n ;;
        --start) START_NOW=y ;;           --no-start) START_NOW=n ;;
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) error "Unknown option: $1 (see bash setup.sh --help)" ;;
    esac
    shift
done

# ask VAR "question" default(y/n): fills VAR unless an option already did.
ask() {
    local var="$1" question="$2" default="$3" reply
    [ -n "${!var}" ] && return
    if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then
        printf -v "$var" '%s' "$default"; return
    fi
    read -rp "[?] $question [$( [ "$default" = y ] && echo Y/n || echo y/N )]: " reply
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]] && printf -v "$var" y || printf -v "$var" n
}

ENV_DIR="/etc/stashgrid"
ENV_FILE="$ENV_DIR/stashgrid.env"
VENV_DIR="$SCRIPT_DIR/venv"
UNIT_DIR="/etc/systemd/system"

echo ""
echo "=================================================="
echo "   StashGrid setup  ($SCRIPT_DIR, user $RUN_USER)"
echo "=================================================="
echo ""

# ── 1. System packages ───────────────────────────────────────
info "Checking system packages..."
command -v python3 >/dev/null || error "python3 not found. Install it with: sudo apt install python3"
NEED=()
python3 -c "import venv, ensurepip" 2>/dev/null || NEED+=(python3-venv)
dpkg -s python3-dev >/dev/null 2>&1 || NEED+=(python3-dev)      # evdev builds against it
command -v curl >/dev/null || NEED+=(curl)
if [ ${#NEED[@]} -gt 0 ]; then
    info "Installing ${NEED[*]}..."
    sudo apt-get install -y "${NEED[@]}" || error "Could not install ${NEED[*]}"
fi

# ── 2. Python environment ────────────────────────────────────
if [ ! -x "$VENV_DIR/bin/python" ]; then
    info "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip --quiet
"$VENV_DIR/bin/python" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt" || error "pip install failed"
info "Python packages installed"

# ── 3. Log folder ────────────────────────────────────────────
sudo mkdir -p /var/log/stashgrid
sudo chown "$RUN_USER":"$RUN_USER" /var/log/stashgrid

# ── 4. Settings file (secret key lives here, never on screen) ─
# One root-only file the services read. Edit it, then:
#   sudo systemctl restart stashgrid
set_setting() {   # set_setting NAME VALUE: add or replace NAME=VALUE in the settings file
    local name="$1" value="$2"
    if sudo grep -q "^$name=" "$ENV_FILE"; then
        sudo sed -i "s|^$name=.*|$name=$value|" "$ENV_FILE"
    else
        echo "$name=$value" | sudo tee -a "$ENV_FILE" >/dev/null
    fi
}
sudo mkdir -p "$ENV_DIR"
if ! sudo test -f "$ENV_FILE"; then
    # Keep the key from an older install (it was written into the unit file),
    # so nobody gets logged out; otherwise make a new one.
    OLD_KEY="$(sudo sed -n 's/^Environment=STASHGRID_SECRET_KEY=//p' "$UNIT_DIR/stashgrid.service" 2>/dev/null | tail -1 || true)"
    if [ -n "$OLD_KEY" ] && [ "$OLD_KEY" != "__SECRET_KEY__" ]; then
        KEY="$OLD_KEY"; info "Kept the secret key from the existing service"
    else
        KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"; info "Generated a new secret key"
    fi
    {
        echo "# StashGrid settings. See README.md > Configuration. Restart after editing:"
        echo "#   sudo systemctl restart stashgrid"
        echo "STASHGRID_SECRET_KEY=$KEY"
        echo "STASHGRID_LOG_DIR=/var/log/stashgrid"
    } | sudo tee "$ENV_FILE" >/dev/null
    unset KEY OLD_KEY
    info "Settings written to $ENV_FILE"
else
    info "Keeping existing settings in $ENV_FILE"
fi
sudo chown root:root "$ENV_FILE"
sudo chmod 600 "$ENV_FILE"

# install_unit TEMPLATE: fill in paths/user and install to systemd
install_unit() {
    local name="$1"
    sed -e "s|__INSTALL_DIR__|$SCRIPT_DIR|g" \
        -e "s|__VENV_DIR__|$VENV_DIR|g" \
        -e "s|__USER__|$RUN_USER|g" \
        -e "s|__UID__|$RUN_UID|g" \
        -e "s|__ENV_FILE__|$ENV_FILE|g" \
        "$SCRIPT_DIR/$name" | sudo tee "$UNIT_DIR/$name" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable "$name" >/dev/null 2>&1
    info "Installed and enabled $name"
}

# ── 5. Web app service ───────────────────────────────────────
install_unit stashgrid.service

# ── 6. USB scanner (optional) ────────────────────────────────
ask SETUP_SCANNER "Set up the USB barcode scanner service?" n
if [ "$SETUP_SCANNER" = y ]; then
    if ! id -nG "$RUN_USER" | grep -qw input; then
        sudo usermod -aG input "$RUN_USER"
        warn "Added $RUN_USER to the 'input' group. Reboot before the scanner will work."
    fi
    DEVICE="$(sed -n 's/^SCANNER_DEVICE = "\(.*\)"/\1/p' "$SCRIPT_DIR/scanner.py" | head -1)"
    [ -e "$DEVICE" ] || warn "Scanner device $DEVICE not found. Plug it in, or edit SCANNER_DEVICE in scanner.py (ls /dev/input/by-id/)."
    install_unit stashgrid-scanner.service
elif [ -f "$UNIT_DIR/stashgrid-scanner.service" ]; then
    info "Scanner service already installed; leaving it as is"
fi

# ── 7. Kiosk display (optional) ──────────────────────────────
ask SETUP_DISPLAY "Show StashGrid full-screen on a screen attached to this Pi?" n
if [ "$SETUP_DISPLAY" = y ]; then
    command -v cage >/dev/null && command -v chromium >/dev/null || {
        info "Installing cage and chromium..."
        sudo apt-get install -y cage chromium || error "Could not install cage/chromium"
    }
    install_unit stashgrid-display.service
    set_setting STASHGRID_KIOSK 0     # the display service replaces the app's own launcher
    # The display runs on tty1; stop the login prompt there.
    sudo systemctl disable --now getty@tty1.service >/dev/null 2>&1 || true
elif [ -f "$UNIT_DIR/stashgrid-display.service" ]; then
    set_setting STASHGRID_KIOSK 0
    info "Display service already installed; leaving it as is"
fi

# ── 8. Cloudflare tunnel (optional) ──────────────────────────
if [ -z "$SETUP_TUNNEL" ] && [ "$ASSUME_YES" = 0 ] && [ -t 0 ]; then
    read -rp "[?] Cloudflare tunnel hostname for the shopping link (blank to skip): " TUNNEL_HOST
    [ -n "$TUNNEL_HOST" ] && SETUP_TUNNEL=y || SETUP_TUNNEL=n
fi
if [ "$SETUP_TUNNEL" = y ]; then
    if ! command -v cloudflared >/dev/null; then
        info "Installing cloudflared from Cloudflare's apt repository..."
        sudo mkdir -p --mode=0755 /usr/share/keyrings
        curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg | sudo tee /usr/share/keyrings/cloudflare-public-v2.gpg >/dev/null
        echo 'deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] https://pkg.cloudflare.com/cloudflared any main' \
            | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
        sudo apt-get update && sudo apt-get install -y cloudflared || error "Could not install cloudflared"
    fi
    if sudo test -f /etc/cloudflared/config.yml; then
        info "Tunnel already configured (/etc/cloudflared/config.yml); leaving it as is"
    else
        warn "A browser login to Cloudflare comes next. Over SSH, open the printed URL on another device."
        cloudflared tunnel login
        CF_OUTPUT="$(cloudflared tunnel create stashgrid 2>&1)" || true
        echo "$CF_OUTPUT"
        TUNNEL_ID="$(echo "$CF_OUTPUT" | grep -oE '[0-9a-f]{8}-[0-9a-f-]{27}' | head -1)"
        [ -n "$TUNNEL_ID" ] || read -rp "[?] Paste the tunnel ID: " TUNNEL_ID
        mkdir -p "$HOME/.cloudflared"
        sed -e "s|<your-tunnel-id>|$TUNNEL_ID|g" \
            -e "s|/home/pi/|$HOME/|g" \
            -e "s|stashgrid\.devinerickson\.com|$TUNNEL_HOST|g" \
            "$SCRIPT_DIR/cloudflared_config.yml" > "$HOME/.cloudflared/config.yml"
        sudo mkdir -p /etc/cloudflared
        sudo cp "$HOME/.cloudflared/config.yml" /etc/cloudflared/config.yml
        cloudflared tunnel route dns stashgrid "$TUNNEL_HOST" || warn "DNS route may already exist"
        sudo cloudflared service install || error "cloudflared service install failed"
        sudo systemctl enable --now cloudflared
        info "Tunnel running for $TUNNEL_HOST"
    fi
    # Links on the Settings page use this address.
    set_setting STASHGRID_PUBLIC_BASE_URL "https://$TUNNEL_HOST"
fi

# ── 9. Start ─────────────────────────────────────────────────
ask START_NOW "Start StashGrid now?" y
if [ "$START_NOW" = y ]; then
    for svc in stashgrid stashgrid-scanner stashgrid-display; do
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            sudo systemctl restart "$svc" || warn "$svc didn't start: journalctl -u $svc -n 50"
        fi
    done
    UP=0
    for _ in $(seq 1 30); do
        if curl -fsk -o /dev/null https://127.0.0.1:5000/login || curl -fs -o /dev/null http://127.0.0.1:5000/login; then
            UP=1; break
        fi
        sleep 2
    done
    [ "$UP" = 1 ] && info "StashGrid is up" || warn "StashGrid isn't answering yet: journalctl -u stashgrid -n 50"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "=================================================="
echo "   Setup complete"
echo ""
echo "   Open:      https://${IP:-<pi-ip>}:5000   (new install: log in as admin / admin)"
echo "   Settings:  $ENV_FILE"
echo "   Logs:      journalctl -u stashgrid -f"
echo "   Update:    bash $SCRIPT_DIR/update.sh"
echo ""
echo "   Shopping link: log in, open SETTINGS, create a link."
echo "=================================================="
