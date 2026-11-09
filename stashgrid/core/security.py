"""Request gates, CSRF, password policy, throttling, headers (moved from app.py, SG-06)."""
import re
import secrets
import sqlite3
import threading
import time
from functools import wraps

from flask import request, render_template, jsonify, redirect, session, url_for, g, flash
from flask_wtf.csrf import CSRFError

from stashgrid.config import DB, SCANNER_API_KEY, SYSTEM_USER_ID, logger
from stashgrid.core import bp
from stashgrid.extensions import csrf


# Endpoints reachable without a session. Everything else requires login.
# The public shopping routes are protected by their own token check instead.
PUBLIC_ENDPOINTS = {
    "core.login",
    "core.logout",   # harmless without a session; avoids /login?next=/logout loops
    "static",
    "inventory.shopping_view",
    "inventory.public_shopping_lists",
    "inventory.public_toggle_item",
    "inventory.public_create_list",
    "inventory.public_add_item",
}

# Endpoints a headless client may call with X-StashGrid-Key instead of a session.
SCANNER_KEY_ENDPOINTS = {"inventory.api_scan"}


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
    return redirect(url_for("core.login", next=nxt.rstrip("?")))


def _valid_scanner_key():
    supplied = request.headers.get("X-StashGrid-Key", "")
    return bool(SCANNER_API_KEY) and secrets.compare_digest(
        supplied.encode(), SCANNER_API_KEY.encode()
    )


# While a password change is pending, only these endpoints are reachable.
PASSWORD_CHANGE_ENDPOINTS = {"core.change_password", "core.logout", "static"}


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


@bp.before_app_request
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
            return redirect(url_for("core.change_password"))
        return None
    if endpoint in SCANNER_KEY_ENDPOINTS and _valid_scanner_key():
        g.user_id = SYSTEM_USER_ID
        g.auth_via_key = True
        return None
    return _unauthenticated_response()


# Public token routes are reached from the shopping link (possibly another
# device, no session) and are already protected by the unguessable token.
CSRF_EXEMPT_ENDPOINTS = PUBLIC_ENDPOINTS - {"core.login"}
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@bp.before_app_request
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


@bp.app_errorhandler(CSRFError)
def _csrf_error(e):
    logger.warning("CSRF rejected %s %s: %s", request.method, request.path, e.description)
    # Browser form submissions (some post to /api/... paths) send Accept: text/html;
    # fetch() calls don't. Forms get a redirect + message, fetch gets JSON.
    is_page_form = "text/html" in request.headers.get("Accept", "")
    if _wants_json() and not is_page_form:
        return jsonify({"error": "SECURITY TOKEN MISSING OR EXPIRED // RELOAD THE PAGE"}), 400
    if request.endpoint == "core.login":
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


@bp.after_app_request
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
    return url_for("inventory.index")
