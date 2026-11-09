"""Process-wide configuration, paths and logging (moved from app.py, SG-06)."""
import logging
import os
import secrets
from logging.handlers import RotatingFileHandler

# Repo root: templates/, static/, shopping_token.txt and scanner_state.json
# stay where they were before the package split.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOG_DIR = os.environ.get("STASHGRID_LOG_DIR", "/var/log/stashgrid")
LOG_FILE = os.path.join(LOG_DIR, "stashgrid.log")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("stashgrid")
logger.setLevel(logging.INFO)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

logger.info("StashGrid app started with Spatial Architecture")

SECRET_KEY = os.environ.get("STASHGRID_SECRET_KEY", "stashgrid-dev-secret-change-in-prod-!")
DB = "inventory.db"

# Shared key that lets a headless client (the hardware scanner, see SG-10) call
# /api/scan without a browser session. Sent as the X-StashGrid-Key header.
# Key auth is disabled when this is unset.
SCANNER_API_KEY = os.environ.get("STASHGRID_SCANNER_KEY", "")
# ── Public Shopping List Token ────────────────────────────────────────────────
# A 32-char hex token stored on disk. The public shopping URL is:
#   https://stashgrid.devinerickson.com/shop/<token>
# Rotate by deleting shopping_token.txt and restarting.
SHOPPING_TOKEN_FILE = os.path.join(BASE_DIR, "shopping_token.txt")

def _load_or_create_token():
    if os.path.exists(SHOPPING_TOKEN_FILE):
        return open(SHOPPING_TOKEN_FILE).read().strip()
    token = secrets.token_hex(16)  # 32-char hex string
    with open(SHOPPING_TOKEN_FILE, "w") as f:
        f.write(token)
    return token

SHOPPING_TOKEN = _load_or_create_token()

SYSTEM_USER_ID = 1  # Reserved ID for hardware scanner audit entries

# Ensure photo storage directory exists alongside the static folder
PHOTOS_DIR = os.path.join(BASE_DIR, "static", "photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

SCANNER_STATE_FILE = os.path.join(BASE_DIR, "scanner_state.json")
