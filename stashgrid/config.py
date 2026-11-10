"""Process-wide configuration, paths and logging (moved from app.py, SG-06)."""
import logging
import os
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
# Absolute path (SG-07). Defaults to the repo root, which is where the old
# relative "inventory.db" resolved because the service runs from there.
DB = os.environ.get("STASHGRID_DB_PATH") or os.path.join(BASE_DIR, "inventory.db")
# Automatic copies taken before schema migrations run (see migrations.py).
BACKUP_DIR = os.environ.get("STASHGRID_BACKUP_DIR") or os.path.join(BASE_DIR, "backups")

# ── Legacy single-household secrets (imported once by migration 5, SG-35) ────
# Before SG-35 there was one global shopping link (shopping_token.txt) and one
# scanner key (STASHGRID_SCANNER_KEY). They are imported into the default
# household the first time migration 5 runs; after that, share links and
# scanner keys are per household and managed on the Settings page. Nothing
# reads these at request time any more.
SHOPPING_TOKEN_FILE = os.path.join(BASE_DIR, "shopping_token.txt")


def _read_legacy_token():
    try:
        with open(SHOPPING_TOKEN_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


LEGACY_SHOPPING_TOKEN = _read_legacy_token()
LEGACY_SCANNER_KEY = os.environ.get("STASHGRID_SCANNER_KEY", "")

SYSTEM_USER_ID = 1  # Reserved ID for hardware scanner audit entries

# Ensure photo storage directory exists alongside the static folder
PHOTOS_DIR = os.path.join(BASE_DIR, "static", "photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

SCANNER_STATE_FILE = os.path.join(BASE_DIR, "scanner_state.json")


# ── Email accounts (SG-33) ───────────────────────────────────────────────────
def _env_flag(name, default=False):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# Public sign-up creates a new account + household. Off by default so a
# self-hosted Pi isn't open to registration; the hosted service turns it on.
SIGNUP_ENABLED = _env_flag("STASHGRID_SIGNUP_ENABLED", False)

# Base URL used in emailed links, e.g. https://app.example.com. Falls back to
# the URL of the request that triggered the email.
PUBLIC_BASE_URL = (os.environ.get("STASHGRID_PUBLIC_BASE_URL") or "").rstrip("/")

# Mail: "console" logs emails (development / self-hosting without SMTP);
# "smtp" sends through any SMTP provider (Postmark, SES, Resend, ...).
MAIL_BACKEND = (os.environ.get("STASHGRID_MAIL_BACKEND") or "console").strip().lower()
MAIL_FROM = os.environ.get("STASHGRID_MAIL_FROM") or "StashGrid <no-reply@localhost>"
SMTP_HOST = os.environ.get("STASHGRID_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("STASHGRID_SMTP_PORT") or 587)
SMTP_USER = os.environ.get("STASHGRID_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("STASHGRID_SMTP_PASSWORD", "")
SMTP_SECURITY = (os.environ.get("STASHGRID_SMTP_SECURITY") or "starttls").strip().lower()  # starttls | ssl | none
