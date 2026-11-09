from flask import Flask, request, render_template, jsonify, redirect, session, url_for, abort, g, flash
from flask_wtf.csrf import CSRFProtect, CSRFError
from functools import wraps
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
import sqlite3
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import time
import threading
import subprocess
import shutil
from datetime import datetime, timedelta
import requests as http_client
from PIL import Image

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

app = Flask(__name__)
# Secret key signs session cookies — override via env var in production
app.secret_key = os.environ.get("STASHGRID_SECRET_KEY", "stashgrid-dev-secret-change-in-prod-!")
# Keep the kiosk and phones logged in across browser restarts.
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # CSRF (SG-04): checked explicitly in _csrf_check so public token routes
    # and scanner-key requests can be skipped. Tokens live as long as the
    # session, so a kiosk left open for hours can still submit forms.
    WTF_CSRF_CHECK_DEFAULT=False,
    WTF_CSRF_TIME_LIMIT=None,
)
csrf = CSRFProtect(app)
DB = "inventory.db"

# Shared key that lets a headless client (the hardware scanner, see SG-10) call
# /api/scan without a browser session. Sent as the X-StashGrid-Key header.
# Key auth is disabled when this is unset.
SCANNER_API_KEY = os.environ.get("STASHGRID_SCANNER_KEY", "")

# ── Public Shopping List Token ────────────────────────────────────────────────
# A 32-char hex token stored on disk. The public shopping URL is:
#   https://stashgrid.devinerickson.com/shop/<token>
# Rotate by deleting shopping_token.txt and restarting.
SHOPPING_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopping_token.txt")

def _load_or_create_token():
    if os.path.exists(SHOPPING_TOKEN_FILE):
        return open(SHOPPING_TOKEN_FILE).read().strip()
    token = secrets.token_hex(16)  # 32-char hex string
    with open(SHOPPING_TOKEN_FILE, "w") as f:
        f.write(token)
    return token

SHOPPING_TOKEN = _load_or_create_token()

# ── Argon2id hasher — Pi-tuned parameters ────────────────────────────────────
# 64 MB memory cost + 3 time iterations: ~200-400 ms on Pi 4, memory-hard
# enough to cripple GPU/ASIC offline attacks.
_ph = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)
SYSTEM_USER_ID = 1  # Reserved ID for hardware scanner audit entries

# Ensure photo storage directory exists alongside the static folder
PHOTOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)


def init_db():
    with sqlite3.connect(DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                is_default INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shelves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                shelf_number INTEGER NOT NULL,
                FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode TEXT NOT NULL,
                name TEXT,
                room_id INTEGER,
                shelf_id INTEGER,
                quantity INTEGER DEFAULT 1,
                notes TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1,
                removed_at TEXT,
                removed_reason TEXT,
                FOREIGN KEY(room_id) REFERENCES rooms(id),
                FOREIGN KEY(shelf_id) REFERENCES shelves(id)
            )
        """)
        # Idempotent migration: add photo_path column if not yet present
        try:
            conn.execute("ALTER TABLE inventory ADD COLUMN photo_path TEXT")
        except Exception:
            pass  # Column already exists
        # Idempotent migration: add low_stock_threshold column
        try:
            conn.execute("ALTER TABLE inventory ADD COLUMN low_stock_threshold INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
        # Idempotent migration: add preferred_store column
        try:
            conn.execute("ALTER TABLE inventory ADD COLUMN preferred_store TEXT DEFAULT 'Costco'")
        except Exception:
            pass  # Column already exists
        # Shopping list tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shopping_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                store_name TEXT,
                is_default INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shopping_list_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER,
                barcode TEXT,
                name TEXT,
                requested_qty INTEGER DEFAULT 1,
                is_completed INTEGER DEFAULT 0,
                FOREIGN KEY(list_id) REFERENCES shopping_lists(id) ON DELETE CASCADE
            )
        """)
        # ── Auth: users table ─────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user'
            )
        """)
        # ── Auth: audit log table ─────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                action      TEXT,
                target_type TEXT,
                target_id   INTEGER,
                timestamp   TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        # ── Idempotent migration: add created_by to inventory ─────────────────
        try:
            conn.execute("ALTER TABLE inventory ADD COLUMN created_by INTEGER")
        except Exception:
            pass  # Column already exists
        # ── Idempotent migrations: password-change flag + session version ─────
        # must_change_password: user is sent to the change-password page until
        #   they set a new one (seeded admin, new users, admin resets).
        # session_version: bumped on password/role changes so existing session
        #   cookies for that user stop working.
        for ddl in (
            "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(ddl)
            except Exception:
                pass  # Column already exists
        # ── Seed system & admin users if the table is brand new ───────────────
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            # id=1 is reserved for the hardware scanner — inserted with explicit id
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role) VALUES (1, '__system__', '', 'system')"
            )
            conn.execute(
                "INSERT INTO users (username, password_hash, role, must_change_password) "
                "VALUES (?, ?, 'admin', 1)",
                ("admin", _ph.hash("admin"))
            )
            logger.info("Auth: seeded __system__ (id=1) and default admin user (must change password)")


# ── Auth helpers & decorators ────────────────────────────────────────────────

def log_audit(user_id, action, target_type, target_id, conn=None):
    """Insert a row into audit_logs. Never raises — a logging failure must not
    crash a route or corrupt a transaction.
    Pass `conn` when the caller holds an open write transaction; opening a
    second connection then fails with 'database is locked'."""
    sql = ("INSERT INTO audit_logs (user_id, action, target_type, target_id) "
           "VALUES (?, ?, ?, ?)")
    params = (user_id, action, target_type, target_id)
    try:
        if conn is not None:
            conn.execute(sql, params)
            return
        with sqlite3.connect(DB) as own_conn:
            own_conn.execute(sql, params)
    except Exception as e:
        logger.warning("log_audit failed: %s", e)


# Endpoints reachable without a session. Everything else requires login.
# The public shopping routes are protected by their own token check instead.
PUBLIC_ENDPOINTS = {
    "login",
    "logout",   # harmless without a session; avoids /login?next=/logout loops
    "static",
    "shopping_view",
    "public_shopping_lists",
    "public_toggle_item",
    "public_create_list",
    "public_add_item",
}

# Endpoints a headless client may call with X-StashGrid-Key instead of a session.
SCANNER_KEY_ENDPOINTS = {"api_scan"}


def _wants_json():
    """True for API/fetch requests, which get 401 JSON instead of a redirect."""
    return (
        request.path.startswith("/api/")
        or request.accept_mimetypes.best == "application/json"
    )


def _unauthenticated_response():
    if _wants_json():
        return jsonify({"error": "AUTHENTICATION REQUIRED"}), 401
    nxt = request.full_path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt.rstrip("?")))


def _valid_scanner_key():
    supplied = request.headers.get("X-StashGrid-Key", "")
    return bool(SCANNER_API_KEY) and secrets.compare_digest(
        supplied.encode(), SCANNER_API_KEY.encode()
    )


# While a password change is pending, only these endpoints are reachable.
PASSWORD_CHANGE_ENDPOINTS = {"change_password", "logout", "static"}


def _load_session_user():
    """Validate the session against the users table. Clears the session if the
    user is gone, can't log in, or their session_version moved on (password or
    role changed). Refreshes the cached role so demotions apply immediately."""
    uid = session.get("user_id")
    if not uid:
        return None
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT username, role, must_change_password, session_version "
            "FROM users WHERE id = ?", (uid,)
        ).fetchone()
    if not row or row[1] == "system" or row[3] != session.get("session_version", 0):
        session.clear()
        return None
    session["username"] = row[0]
    session["role"] = row[1]
    session["must_change_password"] = bool(row[2])
    return uid


@app.before_request
def _require_login():
    """Allow-list auth: only PUBLIC_ENDPOINTS skip the session check (SG-02).
    Users flagged must_change_password are held on the change page (SG-03)."""
    endpoint = request.endpoint
    if endpoint == "static":
        g.user_id = session.get("user_id")
        return None
    g.user_id = _load_session_user()
    if endpoint in PUBLIC_ENDPOINTS:
        return None
    if g.user_id:
        if session.get("must_change_password") and endpoint not in PASSWORD_CHANGE_ENDPOINTS:
            if _wants_json():
                return jsonify({"error": "PASSWORD CHANGE REQUIRED"}), 403
            return redirect(url_for("change_password"))
        return None
    if endpoint in SCANNER_KEY_ENDPOINTS and _valid_scanner_key():
        g.user_id = SYSTEM_USER_ID
        g.auth_via_key = True
        return None
    return _unauthenticated_response()


# Public token routes are reached from the shopping link (possibly another
# device, no session) and are already protected by the unguessable token.
CSRF_EXEMPT_ENDPOINTS = PUBLIC_ENDPOINTS - {"login"}
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@app.before_request
def _csrf_check():
    """SG-04: every state-changing request from a browser session must carry
    a CSRF token (form field csrf_token or X-CSRFToken header). Runs after
    _require_login. The login form is covered too (login CSRF)."""
    if request.method in CSRF_SAFE_METHODS:
        return None
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS or request.endpoint == "static":
        return None
    if g.get("auth_via_key"):
        return None  # headless scanner: no browser, no cookies to abuse
    csrf.protect()
    return None


@app.errorhandler(CSRFError)
def _csrf_error(e):
    logger.warning("CSRF rejected %s %s: %s", request.method, request.path, e.description)
    # Browser form submissions (some post to /api/... paths) send Accept: text/html;
    # fetch() calls don't. Forms get a redirect + message, fetch gets JSON.
    is_page_form = "text/html" in request.headers.get("Accept", "")
    if _wants_json() and not is_page_form:
        return jsonify({"error": "SECURITY TOKEN MISSING OR EXPIRED // RELOAD THE PAGE"}), 400
    if request.endpoint == "login":
        return render_template(
            "login.html", error="SECURITY TOKEN EXPIRED // PLEASE TRY AGAIN"
        ), 400
    flash("ERROR: SECURITY TOKEN EXPIRED // PLEASE TRY AGAIN")
    back = request.referrer or ""
    host_root = request.host_url
    target = back[len(host_root) - 1:] if back.startswith(host_root) else "/"
    return redirect(_safe_next(target))


# ── Password policy & login throttling (SG-03) ───────────────────────────────
MIN_PASSWORD_LENGTH = 8
WEAK_PASSWORDS = {"admin", "password", "password1", "12345678", "123456789", "stashgrid", "changeme"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
ROLES = ("user", "admin")


def _password_problem(password, username):
    """Return an error message if the password is unacceptable, else None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"PASSWORD TOO SHORT // MINIMUM {MIN_PASSWORD_LENGTH} CHARACTERS"
    if password.lower() in WEAK_PASSWORDS or password.lower() == (username or "").lower():
        return "PASSWORD TOO WEAK // CHOOSE SOMETHING LESS GUESSABLE"
    return None


LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300
_login_failures = {}          # (ip, username) -> [timestamps]
_login_failures_lock = threading.Lock()


def _login_key(username):
    return (request.remote_addr or "?", username.lower())


def _login_retry_after(username):
    """Seconds until another attempt is allowed, or 0 if not throttled."""
    now = time.time()
    key = _login_key(username)
    with _login_failures_lock:
        stamps = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        if stamps:
            _login_failures[key] = stamps
        else:
            _login_failures.pop(key, None)
        if len(stamps) >= LOGIN_MAX_FAILURES:
            return int(LOGIN_WINDOW_SECONDS - (now - stamps[0])) + 1
    return 0


def _record_login_failure(username):
    with _login_failures_lock:
        _login_failures.setdefault(_login_key(username), []).append(time.time())
        # Keep memory bounded if someone sprays usernames.
        if len(_login_failures) > 5000:
            _login_failures.clear()


def _clear_login_failures(username):
    with _login_failures_lock:
        _login_failures.pop(_login_key(username), None)


# ── Security headers (SG-05) ─────────────────────────────────────────────────
# Report-only for now: templates still use inline <script> and onclick=
# handlers, so an enforced policy would need 'unsafe-inline' anyway. Violations
# show up in the browser console. Switch to enforcing once inline JS is moved
# into static files (SG-11).
CSP_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy-Report-Only", CSP_POLICY)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    # Keeps the /shop/<token> URL out of Referer headers sent to other sites.
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def login_required(f):
    """Decorator: redirect to /login if there is no active session.
    Redundant with the before_request allow-list; kept for explicitness."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return _unauthenticated_response()
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator: require an authenticated admin session.
    Returns HTTP 403 (not a redirect) so direct API abuse fails loudly."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return _unauthenticated_response()
        if session.get("role") != "admin":
            return jsonify({"error": "CLEARANCE DENIED // ADMIN REQUIRED"}), 403
        return f(*args, **kwargs)
    return decorated


def _safe_next(target):
    """Only allow same-site relative redirects after login."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Secure Terminal Gateway — authenticate and establish a session."""
    if session.get("user_id"):
        return redirect(_safe_next(request.args.get("next")))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        retry_after = _login_retry_after(username)
        if retry_after:
            logger.warning("Auth: throttled login for '%s' from %s", username, request.remote_addr)
            error = f"TOO MANY FAILED ATTEMPTS // TRY AGAIN IN {retry_after}s"
            return render_template("login.html", error=error), 429

        with sqlite3.connect(DB) as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, role, must_change_password, session_version "
                "FROM users WHERE username = ?",
                (username,)
            ).fetchone()
        # System user has no password and cannot log in
        if row and row[2] and row[3] != "system":
            try:
                _ph.verify(row[2], password)
                # ✅ Credentials valid — establish session
                _clear_login_failures(username)
                must_change = bool(row[4]) or _password_problem(password, row[1]) is not None
                if must_change and not row[4]:
                    # Existing installs still on admin/admin (or another weak
                    # password) get flagged the first time they log in.
                    with sqlite3.connect(DB) as conn:
                        conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (row[0],))
                session.clear()
                session.permanent   = True
                session["user_id"]  = row[0]
                session["username"] = row[1]
                session["role"]     = row[3]
                session["session_version"] = row[5]
                session["must_change_password"] = must_change
                logger.info("Auth: user '%s' (role=%s) logged in", row[1], row[3])
                log_audit(row[0], "login", "user", row[0])
                if must_change:
                    return redirect(url_for("change_password", next=request.args.get("next") or None))
                return redirect(_safe_next(request.args.get("next")))
            except (VerifyMismatchError, InvalidHashError):
                pass  # Fall through to generic error
        # Generic message — don't reveal whether the username exists
        _record_login_failure(username)
        error = "AUTHENTICATION FAILED // INVALID CREDENTIALS"
        logger.warning("Auth: failed login attempt for username '%s'", username)

    return render_template("login.html", error=error)


@app.route("/account/password", methods=["GET", "POST"])
def change_password():
    """Change your own password. Mandatory while must_change_password is set."""
    forced = bool(session.get("must_change_password"))
    error = None
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        with sqlite3.connect(DB) as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (g.user_id,)
            ).fetchone()
        try:
            _ph.verify(row[0], current)
            current_ok = True
        except (VerifyMismatchError, InvalidHashError, TypeError):
            current_ok = False

        if not current_ok:
            error = "CURRENT PASSWORD INCORRECT"
        elif new != confirm:
            error = "NEW PASSWORDS DO NOT MATCH"
        elif new == current:
            error = "NEW PASSWORD MUST DIFFER FROM THE CURRENT ONE"
        else:
            error = _password_problem(new, session.get("username"))

        if not error:
            with sqlite3.connect(DB) as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = 0, "
                    "session_version = session_version + 1 WHERE id = ?",
                    (_ph.hash(new), g.user_id)
                )
                version = conn.execute(
                    "SELECT session_version FROM users WHERE id = ?", (g.user_id,)
                ).fetchone()[0]
            # Keep this session valid; every other session for the user is cut off.
            session["session_version"] = version
            session["must_change_password"] = False
            log_audit(g.user_id, "change_password", "user", g.user_id)
            logger.info("Auth: user '%s' changed their password", session.get("username"))
            flash("PASSWORD UPDATED")
            return redirect(_safe_next(request.args.get("next")))

    return render_template("change_password.html", error=error, forced=forced)


# ── User management (admin) ──────────────────────────────────────────────────

def _admin_count(conn):
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


@app.route("/admin/users")
@admin_required
def admin_users():
    with sqlite3.connect(DB) as conn:
        users = conn.execute(
            "SELECT id, username, role, must_change_password FROM users "
            "WHERE role != 'system' ORDER BY username COLLATE NOCASE"
        ).fetchall()
    return render_template("admin_users.html", users=users, roles=ROLES,
                           min_len=MIN_PASSWORD_LENGTH)


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    if not USERNAME_RE.match(username) or username.startswith("__"):
        flash("ERROR: USERNAME MUST BE 3-32 CHARACTERS: LETTERS, NUMBERS, . _ -")
    elif role not in ROLES:
        flash("ERROR: INVALID ROLE")
    elif (problem := _password_problem(password, username)):
        flash("ERROR: " + problem)
    else:
        try:
            with sqlite3.connect(DB) as conn:
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, role, must_change_password) "
                    "VALUES (?, ?, ?, 1)",
                    (username, _ph.hash(password), role)
                )
                new_id = cur.lastrowid
            log_audit(g.user_id, "create_user", "user", new_id)
            logger.info("Auth: admin '%s' created user '%s' (%s)", session.get("username"), username, role)
            flash(f"USER {username} CREATED // THEY MUST CHANGE THE PASSWORD AT FIRST LOGIN")
        except sqlite3.IntegrityError:
            flash("ERROR: USERNAME ALREADY EXISTS")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    password = request.form.get("password", "")
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or row[1] == "system":
            abort(404)
        problem = _password_problem(password, row[0])
        if problem:
            flash("ERROR: " + problem)
            return redirect(url_for("admin_users"))
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1, "
            "session_version = session_version + 1 WHERE id = ?",
            (_ph.hash(password), user_id)
        )
    log_audit(g.user_id, "reset_password", "user", user_id)
    logger.info("Auth: admin '%s' reset password for '%s'", session.get("username"), row[0])
    if user_id == g.user_id:
        # Their own session was just invalidated; send them back through login.
        session.clear()
        return redirect(url_for("login"))
    flash(f"PASSWORD RESET FOR {row[0]} // THEY MUST CHANGE IT AT NEXT LOGIN")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_set_role(user_id):
    role = request.form.get("role", "")
    if role not in ROLES:
        flash("ERROR: INVALID ROLE")
        return redirect(url_for("admin_users"))
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or row[1] == "system":
            abort(404)
        if row[1] == role:
            return redirect(url_for("admin_users"))
        if row[1] == "admin" and _admin_count(conn) <= 1:
            flash("ERROR: CANNOT REMOVE THE LAST ADMIN")
            return redirect(url_for("admin_users"))
        conn.execute(
            "UPDATE users SET role = ?, session_version = session_version + 1 WHERE id = ?",
            (role, user_id)
        )
    log_audit(g.user_id, f"set_role_{role}", "user", user_id)
    logger.info("Auth: admin '%s' set role of '%s' to %s", session.get("username"), row[0], role)
    if user_id == g.user_id:
        session.clear()
        return redirect(url_for("login"))
    flash(f"{row[0]} IS NOW {role.upper()}")
    return redirect(url_for("admin_users"))


@app.route("/logout")
def logout():
    """Terminate the active session and return to the login gateway."""
    username = session.get("username", "unknown")
    user_id  = session.get("user_id")
    if user_id:
        log_audit(user_id, "logout", "user", user_id)
    session.clear()
    logger.info("Auth: user '%s' logged out", username)
    return redirect(url_for("login"))


def _get_rooms_list():

    with sqlite3.connect(DB) as conn:
        return conn.execute("SELECT id, name, is_default FROM rooms ORDER BY name ASC").fetchall()


def _get_room_layout(room_id):
    if not room_id:
        return None, []
    with sqlite3.connect(DB) as conn:
        room = conn.execute("SELECT id, name, is_default FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if not room:
            return None, []
        shelves = conn.execute("SELECT id, shelf_number FROM shelves WHERE room_id = ? ORDER BY shelf_number DESC", (room_id,)).fetchall()
        return room, shelves


def _fetch_items_by_layout(room_id=None, q=""):
    with sqlite3.connect(DB) as conn:
        if q:
            like = f"%{q}%"
            rows = conn.execute("""
                SELECT i.id, i.barcode, i.name, r.name, s.shelf_number, i.quantity, i.notes, i.updated_at, i.room_id, i.shelf_id, i.photo_path, i.low_stock_threshold, i.preferred_store
                FROM inventory i
                LEFT JOIN rooms r ON i.room_id = r.id
                LEFT JOIN shelves s ON i.shelf_id = s.id
                WHERE i.is_active = 1
                  AND (i.barcode LIKE ? OR i.name LIKE ? OR i.notes LIKE ? OR r.name LIKE ?)
                ORDER BY i.updated_at DESC
            """, (like, like, like, like)).fetchall()
            return rows
        elif room_id:
            rows = conn.execute("""
                SELECT i.id, i.barcode, i.name, r.name, s.shelf_number, i.quantity, i.notes, i.updated_at, i.room_id, i.shelf_id, i.photo_path, i.low_stock_threshold, i.preferred_store
                FROM inventory i
                LEFT JOIN rooms r ON i.room_id = r.id
                LEFT JOIN shelves s ON i.shelf_id = s.id
                WHERE i.is_active = 1 AND i.room_id = ?
                ORDER BY s.shelf_number DESC, i.updated_at DESC
            """, (room_id,)).fetchall()
            return rows
        return []


def _resolve_room_id(raw_id, rooms):
    """Safely extracts fallback integers to avoid API data parameter crashes."""
    if not raw_id or str(raw_id).strip() in ("", "None", "undefined"):
        with sqlite3.connect(DB) as conn:
            default_room = conn.execute("SELECT id FROM rooms WHERE is_default = 1 LIMIT 1").fetchone()
            if default_room:
                return int(default_room[0])
            elif rooms:
                return int(rooms[0][0])
            return None
    try:
        return int(raw_id)
    except ValueError:
        return None


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    rooms = _get_rooms_list()
    selected_room_id = _resolve_room_id(request.args.get("room_id"), rooms)

    room_info, shelves = _get_room_layout(selected_room_id)
    items = _fetch_items_by_layout(room_id=selected_room_id, q=q)

    with sqlite3.connect(DB) as conn:
        store_names = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT store_name FROM shopping_lists WHERE store_name IS NOT NULL AND store_name != '' ORDER BY store_name ASC"
            ).fetchall()
        ]

    return render_template(
        "index.html",
        rooms=rooms,
        room_info=room_info,
        shelves=shelves,
        items=items,
        selected_room_id=selected_room_id,
        q=q,
        store_names=store_names
    )


@app.route("/api/rooms", methods=["POST"])
def create_room():
    name = request.form.get("name", "").strip()
    shelf_count = int(request.form.get("shelf_count", 3))
    if not name:
        return redirect("/")

    with sqlite3.connect(DB) as conn:
        existing = conn.execute("SELECT count(*) FROM rooms").fetchone()[0]
        is_default = 1 if existing == 0 else 0

        cursor = conn.execute("INSERT INTO rooms (name, is_default) VALUES (?, ?)", (name, is_default))
        room_id = cursor.lastrowid

        for i in range(1, shelf_count + 1):
            conn.execute("INSERT INTO shelves (room_id, shelf_number) VALUES (?, ?)", (room_id, i))

    log_audit(session.get("user_id"), "create_room", "room", room_id)
    return redirect(f"/?room_id={room_id}")


@app.route("/api/rooms/<int:room_id>/set-default", methods=["POST"])
def set_default_room(room_id):
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE rooms SET is_default = 0")
        conn.execute("UPDATE rooms SET is_default = 1 WHERE id = ?", (room_id,))
    return redirect(f"/?room_id={room_id}")


@app.route("/api/rooms/<int:room_id>/add-shelves", methods=["POST"])
def add_shelves_to_room(room_id):
    """Append additional shelves to an existing room."""
    add_count = int(request.form.get("add_count", 1))
    with sqlite3.connect(DB) as conn:
        room = conn.execute("SELECT id FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if not room:
            return redirect("/")
        # Find the highest existing shelf number so we continue from there
        max_shelf = conn.execute(
            "SELECT COALESCE(MAX(shelf_number), 0) FROM shelves WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        for i in range(1, add_count + 1):
            conn.execute("INSERT INTO shelves (room_id, shelf_number) VALUES (?, ?)", (room_id, max_shelf + i))
    logger.info("Added %d shelf(ves) to room %d", add_count, room_id)
    return redirect(f"/?room_id={room_id}")


@app.route("/api/rooms/<int:room_id>/delete", methods=["POST"])
@admin_required
def delete_room(room_id):
    """Delete a room, its shelves, and soft-delete all its inventory items."""
    with sqlite3.connect(DB) as conn:
        # Soft-delete all inventory belonging to this room
        conn.execute(
            "UPDATE inventory SET is_active = 0, quantity = 0, removed_at = CURRENT_TIMESTAMP, "
            "removed_reason = 'Room deleted via HUD' WHERE room_id = ?",
            (room_id,)
        )
        # Hard-delete shelves and room (FK cascade handles shelves if pragma is on, but explicit is safer)
        conn.execute("DELETE FROM shelves WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
    log_audit(session.get("user_id"), "delete_room", "room", room_id)
    logger.info("Deleted room %d", room_id)
    return redirect("/")


def _lookup_open_food_facts(barcode):
    """Try Open Food Facts API. Returns product name or empty string."""
    try:
        resp = http_client.get(
            f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json",
            timeout=2,
            headers={"User-Agent": "StashGrid/1.0"}
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == 1:
                p = data.get("product", {})
                name = p.get("product_name") or p.get("generic_name") or p.get("brands")
                if name:
                    return name.strip()
    except Exception as e:
        logger.debug("Open Food Facts lookup failed for %s: %s", barcode, e)
    return ""


def _lookup_upcitemdb(barcode):
    """Try UPCitemdb API as a fallback. Returns product title or empty string."""
    try:
        resp = http_client.get(
            f"https://api.upcitemdb.com/prod/trial/lookup?upc={barcode}",
            timeout=2,
            headers={"User-Agent": "StashGrid/1.0"}
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            if items:
                title = items[0].get("title")
                if title:
                    return title.strip()
    except Exception as e:
        logger.debug("UPCitemdb lookup failed for %s: %s", barcode, e)
    return ""


def _web_lookup_name(barcode):
    """Chain Open Food Facts → UPCitemdb, mirroring scanner.py lookup_product_name.
    Only attempted for numeric barcodes (UPC/EAN). Returns empty string on failure.
    """
    if not barcode.isdigit():
        return ""
    return _lookup_open_food_facts(barcode) or _lookup_upcitemdb(barcode) or ""


def _auto_add_to_shopping_list(conn, barcode, name, preferred_store="Costco"):
    """Auto-insert a low-stock item into the best-matching shopping list.
    Resolution order:
      1. A list whose store_name exactly matches preferred_store (case-insensitive)
      2. The list flagged is_default = 1
      3. A newly-created default list if none exists
    Called within an existing DB connection — no new connection needed.
    """
    list_id = None

    # 1. Try to find a list matching the item's preferred store
    if preferred_store:
        match = conn.execute(
            "SELECT id FROM shopping_lists WHERE LOWER(store_name) = LOWER(?) LIMIT 1",
            (preferred_store,)
        ).fetchone()
        if match:
            list_id = match[0]

    # 2. Fall back to the default list
    if list_id is None:
        default_list = conn.execute(
            "SELECT id FROM shopping_lists WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if default_list:
            list_id = default_list[0]

    # 3. No lists exist at all — create one named after the preferred store
    if list_id is None:
        cursor = conn.execute(
            "INSERT INTO shopping_lists (name, store_name, is_default) VALUES (?, ?, 1)",
            (preferred_store or "Shopping List", preferred_store or "")
        )
        list_id = cursor.lastrowid

    existing = conn.execute(
        "SELECT id FROM shopping_list_items WHERE list_id = ? AND barcode = ? AND is_completed = 0",
        (list_id, barcode)
    ).fetchone()

    if not existing:
        conn.execute(
            "INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty, is_completed) "
            "VALUES (?, ?, ?, 1, 0)",
            (list_id, barcode, name)
        )
        logger.info(
            "Auto-added '%s' (%s) to shopping list %d (store: %s, low stock triggered)",
            name, barcode, list_id, preferred_store or "default"
        )


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Receive a barcode from the browser camera or a headless scanner client.
    Requires a logged-in session, or the X-StashGrid-Key header for headless
    clients (attributed to SYSTEM_USER_ID in the audit log)."""
    actor_id = g.user_id or SYSTEM_USER_ID
    barcode = request.form.get("barcode", "").strip().upper()
    shelf_id = request.form.get("shelf_id", type=int)
    room_id = request.form.get("room_id", type=int)

    if not barcode or not shelf_id or not room_id:
        return jsonify({"error": "barcode, shelf_id and room_id are all required"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB) as conn:
        existing = conn.execute("""
            SELECT id, quantity, name, photo_path FROM inventory
            WHERE barcode = ? AND room_id = ? AND shelf_id = ? AND is_active = 1
        """, (barcode, room_id, shelf_id)).fetchone()

        if existing:
            conn.execute(
                "UPDATE inventory SET quantity = quantity + 1, updated_at = ? WHERE id = ?",
                (now, existing[0])
            )
            logger.info("Incremented barcode %s on shelf %d", barcode, shelf_id)
            log_audit(actor_id, "scan_increment", "item", existing[0], conn=conn)
            return jsonify({
                "id": existing[0],
                "name": existing[2] or barcode,
                "quantity": existing[1] + 1,
                "photo_path": existing[3],
                "is_new": False
            })
        else:
            name = _web_lookup_name(barcode) or "[ New Asset ]"
            cursor = conn.execute("""
                INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity, notes, updated_at, is_active)
                VALUES (?, ?, ?, ?, 1, '', ?, 1)
            """, (barcode, name, room_id, shelf_id, now))
            new_id = cursor.lastrowid
            logger.info("New item scanned: %s (%s) on shelf %d", name, barcode, shelf_id)
            log_audit(actor_id, "scan_new", "item", new_id, conn=conn)
            return jsonify({
                "id": new_id,
                "name": name,
                "quantity": 1,
                "photo_path": None,
                "is_new": True
            })


@app.route("/api/items/<int:item_id>/photo", methods=["POST"])
def upload_item_photo(item_id):
    """Accept a photo upload, resize it with Pillow, save to static/photos/, update DB."""
    if "photo" not in request.files or not request.files["photo"].filename:
        return jsonify({"error": "No photo file provided"}), 400

    try:
        photo_file = request.files["photo"]
        img = Image.open(photo_file)
        img.thumbnail((400, 400), Image.LANCZOS)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        filename = f"{item_id}.jpg"
        save_path = os.path.join(PHOTOS_DIR, filename)
        img.save(save_path, "JPEG", quality=85)

        photo_url = f"/static/photos/{filename}"
        with sqlite3.connect(DB) as conn:
            conn.execute("UPDATE inventory SET photo_path = ? WHERE id = ?", (photo_url, item_id))

        logger.info("Photo saved for item %d at %s", item_id, save_path)
        return jsonify({"photo_url": photo_url})

    except Exception as e:
        logger.error("Photo upload failed for item %d: %s", item_id, e)
        return jsonify({"error": str(e)}), 500


SCANNER_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_state.json")


@app.route("/api/scanner-state")
def scanner_state():
    """Return the live state written by scanner.py (mode, shelf, room).
    Returns a default 'offline' payload if scanner.py isn't running yet."""
    try:
        with open(SCANNER_STATE_FILE, "r") as f:
            state = json.load(f)
        state["online"] = True
        return jsonify(state)
    except FileNotFoundError:
        return jsonify({"online": False, "mode": "IN", "shelf_number": None, "shelf_id": None, "room_id": None})
    except Exception as e:
        logger.warning("Failed to read scanner state file: %s", e)
        return jsonify({"online": False, "mode": "IN", "shelf_number": None, "shelf_id": None, "room_id": None})


@app.route("/api/inventory-hash")
def inventory_hash():
    q = request.args.get("q", "").strip()
    rooms = _get_rooms_list()
    selected_room_id = _resolve_room_id(request.args.get("room_id"), rooms)

    items = _fetch_items_by_layout(room_id=selected_room_id, q=q)
    with sqlite3.connect(DB) as conn:
        sl_lists = conn.execute(
            "SELECT id, name, store_name, is_default FROM shopping_lists ORDER BY id"
        ).fetchall()
        sl_items = conn.execute(
            "SELECT id, list_id, barcode, is_completed FROM shopping_list_items ORDER BY id"
        ).fetchall()
    state = {"items": items, "rooms": rooms, "sl_lists": sl_lists, "sl_items": sl_items}
    digest = hashlib.md5(json.dumps(state, default=str).encode()).hexdigest()
    return jsonify({"hash": digest})


@app.route("/api/items")
def api_items():
    q = request.args.get("q", "").strip()
    rooms = _get_rooms_list()
    selected_room_id = _resolve_room_id(request.args.get("room_id"), rooms)

    room_info, shelves = _get_room_layout(selected_room_id)
    items = _fetch_items_by_layout(room_id=selected_room_id, q=q)

    with sqlite3.connect(DB) as conn:
        store_names = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT store_name FROM shopping_lists WHERE store_name IS NOT NULL AND store_name != '' ORDER BY store_name ASC"
            ).fetchall()
        ]

    html = render_template("_item_cards.html", room_info=room_info, items=items, shelves=shelves, q=q, store_names=store_names)
    return jsonify({"html": html, "count": len(items)})


@app.route("/edit/<int:item_id>", methods=["POST"])
def edit_item(item_id):
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    room_id = request.form.get("room_id")
    threshold = int(request.form.get("low_stock_threshold", 0) or 0)
    preferred_store = request.form.get("preferred_store", "Costco").strip() or "Costco"
    new_shelf_id_raw = request.form.get("new_shelf_id", "").strip()

    with sqlite3.connect(DB) as conn:
        if new_shelf_id_raw.isdigit():
            # Relocate asset: update shelf_id alongside the standard metadata fields
            conn.execute("""
                UPDATE inventory
                SET name = ?, notes = ?, low_stock_threshold = ?, preferred_store = ?,
                    shelf_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (name, notes, threshold, preferred_store, int(new_shelf_id_raw), item_id))
            logger.info("Relocated item %d to shelf %s via Web HUD", item_id, new_shelf_id_raw)
        else:
            # No relocation requested — leave shelf_id untouched
            conn.execute("""
                UPDATE inventory
                SET name = ?, notes = ?, low_stock_threshold = ?, preferred_store = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (name, notes, threshold, preferred_store, item_id))

    log_audit(session.get("user_id"), "edit_item", "item", item_id)
    return redirect(f"/?room_id={room_id}" if room_id else "/")


@app.route("/remove/<int:item_id>", methods=["POST"])
def remove_item_quantity(item_id):
    amount = int(request.form.get("amount", 1))
    reason = request.form.get("reason", "Removed via HUD")
    room_id = request.form.get("room_id")

    with sqlite3.connect(DB) as conn:
        item = conn.execute(
            "SELECT quantity, barcode, name, low_stock_threshold, preferred_store FROM inventory WHERE id = ? AND is_active = 1",
            (item_id,)
        ).fetchone()
        if not item:
            return redirect("/")

        current_quantity, barcode, item_name, threshold, preferred_store = item
        if current_quantity > amount:
            conn.execute(
                "UPDATE inventory SET quantity = quantity - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (amount, item_id)
            )
            new_qty = current_quantity - amount
        else:
            conn.execute(
                "UPDATE inventory SET quantity = 0, is_active = 0, removed_at = CURRENT_TIMESTAMP, "
                "removed_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (reason, item_id)
            )
            new_qty = 0

        # Auto-add to the item's preferred store list if stock drops below threshold
        if threshold and threshold > 0 and new_qty < threshold:
            _auto_add_to_shopping_list(
                conn, barcode, item_name or "[ Unknown Item ]",
                preferred_store=preferred_store or "Costco"
            )

    log_audit(session.get("user_id"), "remove_qty", "item", item_id)
    return redirect(f"/?room_id={room_id}" if room_id else "/")


@app.route("/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    reason = request.form.get("reason", "Purged via terminal")
    room_id = request.form.get("room_id")

    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE inventory SET quantity = 0, is_active = 0, removed_at = CURRENT_TIMESTAMP, removed_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (reason, item_id))

    log_audit(session.get("user_id"), "delete_item", "item", item_id)
    return redirect(f"/?room_id={room_id}" if room_id else "/")


# ── Shopping List Routes ──────────────────────────────────────

@app.route("/api/shopping-lists")
def get_shopping_lists():
    with sqlite3.connect(DB) as conn:
        lists = conn.execute(
            "SELECT id, name, store_name, is_default FROM shopping_lists ORDER BY is_default DESC, name ASC"
        ).fetchall()
        items = conn.execute(
            "SELECT id, list_id, barcode, name, requested_qty, is_completed "
            "FROM shopping_list_items ORDER BY is_completed ASC, id ASC"
        ).fetchall()

    lists_data = []
    for lst in lists:
        list_items = [
            {"id": row[0], "barcode": row[2], "name": row[3], "requested_qty": row[4], "is_completed": row[5]}
            for row in items if row[1] == lst[0]
        ]
        lists_data.append({
            "id": lst[0], "name": lst[1], "store_name": lst[2] or "",
            "is_default": lst[3], "items": list_items
        })
    return jsonify({"lists": lists_data})


@app.route("/api/shopping-lists/create", methods=["POST"])
def create_shopping_list():
    data = request.get_json(silent=True) or {}
    name = (request.form.get("name") or data.get("name", "")).strip()
    store_name = (request.form.get("store_name") or data.get("store_name", "")).strip()
    if not name:
        return jsonify({"error": "List name is required"}), 400
    with sqlite3.connect(DB) as conn:
        existing_count = conn.execute("SELECT COUNT(*) FROM shopping_lists").fetchone()[0]
        is_default = 1 if existing_count == 0 else 0
        cursor = conn.execute(
            "INSERT INTO shopping_lists (name, store_name, is_default) VALUES (?, ?, ?)",
            (name, store_name, is_default)
        )
        list_id = cursor.lastrowid
    logger.info("Created shopping list '%s' (id=%d)", name, list_id)
    return jsonify({"ok": True, "list_id": list_id})


@app.route("/api/shopping-lists/add", methods=["POST"])
def add_to_shopping_list():
    data = request.get_json(silent=True) or {}
    list_id = int(request.form.get("list_id") or data.get("list_id") or 0)
    barcode = (request.form.get("barcode") or data.get("barcode", "")).strip().upper()
    name = (request.form.get("name") or data.get("name", "")).strip()
    qty = int(request.form.get("qty") or data.get("qty") or 1)
    if not list_id or not barcode:
        return jsonify({"error": "list_id and barcode are required"}), 400
    with sqlite3.connect(DB) as conn:
        existing = conn.execute(
            "SELECT id FROM shopping_list_items WHERE list_id = ? AND barcode = ? AND is_completed = 0",
            (list_id, barcode)
        ).fetchone()
        if existing:
            return jsonify({"ok": True, "already_exists": True})
        conn.execute(
            "INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty, is_completed) "
            "VALUES (?, ?, ?, ?, 0)",
            (list_id, barcode, name or barcode, qty)
        )
    return jsonify({"ok": True, "already_exists": False})


@app.route("/api/shopping-lists/toggle-complete/<int:item_id>", methods=["POST"])
def toggle_shopping_item(item_id):
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT is_completed FROM shopping_list_items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Item not found"}), 404
        new_state = 0 if row[0] else 1
        conn.execute(
            "UPDATE shopping_list_items SET is_completed = ? WHERE id = ?", (new_state, item_id)
        )
    return jsonify({"ok": True, "is_completed": new_state})


@app.route("/api/shopping-lists/commit", methods=["POST"])
def commit_restock():
    data = request.get_json(silent=True) or {}
    list_id = int(request.form.get("list_id") or data.get("list_id") or 0)
    if not list_id:
        return jsonify({"error": "list_id is required"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    restocked = []
    unknown = []
    with sqlite3.connect(DB) as conn:
        completed = conn.execute(
            "SELECT id, barcode, name, requested_qty FROM shopping_list_items "
            "WHERE list_id = ? AND is_completed = 1",
            (list_id,)
        ).fetchall()
        for sl_id, barcode, sl_name, qty in completed:
            inv = conn.execute(
                "SELECT id FROM inventory WHERE barcode = ? AND is_active = 1 LIMIT 1",
                (barcode,)
            ).fetchone()
            if inv:
                conn.execute(
                    "UPDATE inventory SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                    (qty, now, inv[0])
                )
                conn.execute("DELETE FROM shopping_list_items WHERE id = ?", (sl_id,))
                restocked.append({"barcode": barcode, "name": sl_name, "qty": qty})
                logger.info("Restocked %s x%d via shopping list commit", barcode, qty)
            else:
                unknown.append({"barcode": barcode, "name": sl_name})
    return jsonify({"ok": True, "restocked": restocked, "unknown_barcodes": unknown})


@app.route("/api/shopping-lists/<int:list_id>/set-default", methods=["POST"])
def set_default_shopping_list(list_id):
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE shopping_lists SET is_default = 0")
        conn.execute("UPDATE shopping_lists SET is_default = 1 WHERE id = ?", (list_id,))
    return jsonify({"ok": True})


@app.route("/api/shopping-lists/<int:list_id>/rename", methods=["POST"])
def rename_shopping_list(list_id):
    data = request.get_json(silent=True) or {}
    name = (request.form.get("name") or data.get("name", "")).strip()
    store_name = (request.form.get("store_name") or data.get("store_name", "")).strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    with sqlite3.connect(DB) as conn:
        conn.execute(
            "UPDATE shopping_lists SET name = ?, store_name = ? WHERE id = ?",
            (name, store_name, list_id)
        )
    return jsonify({"ok": True})


@app.route("/api/shopping-lists/<int:list_id>/delete", methods=["POST"])
@admin_required
def delete_shopping_list(list_id):
    with sqlite3.connect(DB) as conn:
        lst = conn.execute(
            "SELECT is_default FROM shopping_lists WHERE id = ?", (list_id,)
        ).fetchone()
        # Explicitly delete items first (FK cascade requires PRAGMA foreign_keys=ON)
        conn.execute("DELETE FROM shopping_list_items WHERE list_id = ?", (list_id,))
        conn.execute("DELETE FROM shopping_lists WHERE id = ?", (list_id,))
        # Promote another list to default if we just deleted the default
        if lst and lst[0] == 1:
            next_lst = conn.execute("SELECT id FROM shopping_lists LIMIT 1").fetchone()
            if next_lst:
                conn.execute("UPDATE shopping_lists SET is_default = 1 WHERE id = ?", (next_lst[0],))
    logger.info("Deleted shopping list %d", list_id)
    return jsonify({"ok": True})


init_db()

# Log the public shopping URL so you always know where to send the link
logger.info(
    "Public shopping list URL: https://stashgrid.devinerickson.com/shop/%s",
    SHOPPING_TOKEN
)
print(f"[StashGrid] Public shopping list URL: https://stashgrid.devinerickson.com/shop/{SHOPPING_TOKEN}")


# ── Public Shopping List Routes (token-secured, Cloudflare-exposed) ───────────

def _get_shopping_lists_payload():
    """Shared data-fetch used by both the local and public shopping list APIs."""
    with sqlite3.connect(DB) as conn:
        lists = conn.execute(
            "SELECT id, name, store_name, is_default FROM shopping_lists ORDER BY is_default DESC, name ASC"
        ).fetchall()
        items = conn.execute(
            "SELECT id, list_id, barcode, name, requested_qty, is_completed "
            "FROM shopping_list_items ORDER BY is_completed ASC, id ASC"
        ).fetchall()

    lists_data = []
    for lst in lists:
        list_items = [
            {"id": row[0], "barcode": row[2], "name": row[3], "requested_qty": row[4], "is_completed": row[5]}
            for row in items if row[1] == lst[0]
        ]
        lists_data.append({
            "id": lst[0], "name": lst[1], "store_name": lst[2] or "",
            "is_default": lst[3], "items": list_items
        })
    return lists_data


@app.route("/shop/<token>")
def shopping_view(token):
    """Public read-only shopping list view — token-secured, Cloudflare-exposed."""
    if token != SHOPPING_TOKEN:
        abort(404)
    return render_template("shopping_view.html", token=token)


@app.route("/api/public/shopping-lists/<token>")
def public_shopping_lists(token):
    """Public API: return shopping list data. Token-secured."""
    if token != SHOPPING_TOKEN:
        abort(404)
    return jsonify({"lists": _get_shopping_lists_payload()})


@app.route("/api/public/shopping-lists/toggle/<token>/<int:item_id>", methods=["POST"])
def public_toggle_item(token, item_id):
    """Public API: toggle a shopping list item's completion state. Token-secured."""
    if token != SHOPPING_TOKEN:
        abort(404)
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT is_completed FROM shopping_list_items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Item not found"}), 404
        new_state = 0 if row[0] else 1
        conn.execute(
            "UPDATE shopping_list_items SET is_completed = ? WHERE id = ?", (new_state, item_id)
        )
    logger.info("Public toggle: item %d → is_completed=%d", item_id, new_state)
    return jsonify({"ok": True, "is_completed": new_state})


@app.route("/api/public/shopping-lists/create/<token>", methods=["POST"])
def public_create_list(token):
    """Public API: create a new shopping list. Token-secured."""
    if token != SHOPPING_TOKEN:
        abort(404)
    data = request.get_json(silent=True) or {}
    name = (request.form.get("name") or data.get("name", "")).strip()
    store_name = (request.form.get("store_name") or data.get("store_name", "")).strip()
    if not name:
        return jsonify({"error": "List name is required"}), 400
    with sqlite3.connect(DB) as conn:
        existing_count = conn.execute("SELECT COUNT(*) FROM shopping_lists").fetchone()[0]
        is_default = 1 if existing_count == 0 else 0
        cursor = conn.execute(
            "INSERT INTO shopping_lists (name, store_name, is_default) VALUES (?, ?, ?)",
            (name, store_name, is_default)
        )
        list_id = cursor.lastrowid
    logger.info("Public created shopping list '%s' (id=%d)", name, list_id)
    return jsonify({"ok": True, "list_id": list_id})


@app.route("/api/public/shopping-lists/add/<token>", methods=["POST"])
def public_add_item(token):
    """Public API: add an item to a shopping list. Token-secured."""
    if token != SHOPPING_TOKEN:
        abort(404)
    data = request.get_json(silent=True) or {}
    list_id = int(request.form.get("list_id") or data.get("list_id") or 0)
    name = (request.form.get("name") or data.get("name", "")).strip()
    barcode = (request.form.get("barcode") or data.get("barcode", "")).strip().upper()
    qty = int(request.form.get("qty") or data.get("qty") or 1)
    if not list_id or not name:
        return jsonify({"error": "list_id and name are required"}), 400
    # Use barcode if provided, otherwise generate a placeholder
    effective_barcode = barcode if barcode else f"MANUAL-{list_id}-{int(datetime.now().timestamp())}"
    with sqlite3.connect(DB) as conn:
        # Verify list exists
        if not conn.execute("SELECT id FROM shopping_lists WHERE id = ?", (list_id,)).fetchone():
            return jsonify({"error": "List not found"}), 404
        existing = conn.execute(
            "SELECT id FROM shopping_list_items WHERE list_id = ? AND LOWER(name) = LOWER(?) AND is_completed = 0",
            (list_id, name)
        ).fetchone()
        if existing:
            return jsonify({"ok": True, "already_exists": True})
        conn.execute(
            "INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty, is_completed) VALUES (?, ?, ?, ?, 0)",
            (list_id, effective_barcode, name, qty)
        )
    logger.info("Public added item '%s' to list %d", name, list_id)
    return jsonify({"ok": True, "already_exists": False})

# ── Kiosk Display Engine ────────────────────────────────

def _detect_display():
    """Find an active X11 display on this machine.
    Checks the environment first (works when launched from a desktop session),
    then probes common display numbers via xrandr (works when running as a
    system service with an X session already active on the console).
    Returns a display string such as ':0', or None if nothing is detected.
    """
    env_display = os.environ.get("DISPLAY")
    if env_display:
        return env_display

    # When running as a system service DISPLAY is not inherited.
    # Probe :0 and :1 directly — one of these will be live if a desktop is running.
    if shutil.which("xrandr"):
        for disp in (":0", ":1"):
            try:
                result = subprocess.run(
                    ["xrandr", "--display", disp],
                    timeout=2,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    return disp
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass

    return None


def _find_kiosk_browser():
    """Return a command list suitable for kiosk mode, or None.
    Tries Chromium first (the default browser on Raspberry Pi OS),
    then Firefox as a fallback.
    """
    for bin_name in ("chromium", "chromium-browser"):
        if shutil.which(bin_name):
            return [
                bin_name,
                "--kiosk",
                "--noerrdialogs",
                "--disable-infobars",
                "--no-first-run",
                "--disable-session-crashed-bubble",
                "--disable-features=TranslateUI",
                "--disable-restore-session-state",
                "--ignore-certificate-errors",  # required for the adhoc self-signed cert
            ]

    if shutil.which("firefox"):
        return ["firefox", "--kiosk"]

    return None


def _launch_kiosk_display(startup_delay=3):
    """Detect an attached display and open StashGrid in a kiosk browser window.
    Intended to be run in a daemon thread so it never blocks the Flask server.
    A short delay is applied first to ensure the server is accepting connections.
    Completely non-fatal: logs a message and returns if no display or browser
    is available.
    """
    time.sleep(startup_delay)

    display = _detect_display()
    if not display:
        logger.info("Kiosk: no display detected — skipping browser launch")
        return

    browser_cmd = _find_kiosk_browser()
    if not browser_cmd:
        logger.warning(
            "Kiosk: display found (%s) but no supported browser detected. "
            "Install Chromium with: sudo apt install chromium-browser",
            display,
        )
        return

    # Choose protocol to match however Flask actually started
    try:
        import OpenSSL  # noqa: F401
        url = "https://localhost:5000"
    except ImportError:
        url = "http://localhost:5000"

    env = os.environ.copy()
    env["DISPLAY"] = display

    try:
        subprocess.Popen(
            browser_cmd + [url],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(
            "Kiosk: launched %s on display %s → %s",
            browser_cmd[0], display, url,
        )
    except Exception as exc:
        logger.error("Kiosk: failed to launch browser — %s", exc)


if __name__ == "__main__":
    # Spin up the kiosk display in a background thread.
    # The thread is daemonised so it dies automatically when Flask exits.
    # It will silently do nothing if no display or browser is found.
    threading.Thread(target=_launch_kiosk_display, daemon=True).start()

    # HTTPS is required for camera access on mobile browsers (iOS Safari, Android Chrome).
    # ssl_context='adhoc' generates a self-signed cert automatically via pyOpenSSL.
    # Install on Pi with: pip install pyOpenSSL
    # First visit: browser will show a security warning — tap "Advanced" → "Proceed" once.
    try:
        app.run(host="0.0.0.0", port=5000, ssl_context="adhoc")
    except Exception:
        # pyOpenSSL not installed — fall back to plain HTTP (camera will not work on mobile)
        print("[!] pyOpenSSL not found. Running on HTTP — camera scanning will not work on mobile.")
        print("    Install with: pip install pyOpenSSL")
        app.run(host="0.0.0.0", port=5000)