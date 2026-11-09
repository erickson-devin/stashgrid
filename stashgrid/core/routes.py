"""Login, logout, password change and user management routes (moved from app.py, SG-06)."""
import sqlite3

from argon2.exceptions import VerifyMismatchError, InvalidHashError
from flask import request, render_template, redirect, session, url_for, abort, g, flash

from stashgrid.db import get_db
from stashgrid.config import logger
from stashgrid.core import bp
from stashgrid.core.audit import log_audit
from stashgrid.extensions import password_hasher as _ph
from stashgrid.core.security import (
    ROLES, MIN_PASSWORD_LENGTH, USERNAME_RE, admin_required, _password_problem,
    _login_retry_after, _record_login_failure, _clear_login_failures, _safe_next,
)


@bp.route("/login", methods=["GET", "POST"])
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

        with get_db() as conn:
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
                    with get_db() as conn:
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
                    return redirect(url_for("core.change_password", next=request.args.get("next") or None))
                return redirect(_safe_next(request.args.get("next")))
            except (VerifyMismatchError, InvalidHashError):
                pass  # Fall through to generic error
        # Generic message — don't reveal whether the username exists
        _record_login_failure(username)
        error = "AUTHENTICATION FAILED // INVALID CREDENTIALS"
        logger.warning("Auth: failed login attempt for username '%s'", username)

    return render_template("login.html", error=error)


@bp.route("/account/password", methods=["GET", "POST"])
def change_password():
    """Change your own password. Mandatory while must_change_password is set."""
    forced = bool(session.get("must_change_password"))
    error = None
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        with get_db() as conn:
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
            with get_db() as conn:
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


@bp.route("/admin/users")
@admin_required
def admin_users():
    with get_db() as conn:
        users = conn.execute(
            "SELECT id, username, role, must_change_password FROM users "
            "WHERE role != 'system' ORDER BY username COLLATE NOCASE"
        ).fetchall()
    return render_template("admin_users.html", users=users, roles=ROLES,
                           min_len=MIN_PASSWORD_LENGTH)


@bp.route("/admin/users/create", methods=["POST"])
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
            with get_db() as conn:
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
    return redirect(url_for("core.admin_users"))


@bp.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    password = request.form.get("password", "")
    with get_db() as conn:
        row = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or row[1] == "system":
            abort(404)
        problem = _password_problem(password, row[0])
        if problem:
            flash("ERROR: " + problem)
            return redirect(url_for("core.admin_users"))
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
        return redirect(url_for("core.login"))
    flash(f"PASSWORD RESET FOR {row[0]} // THEY MUST CHANGE IT AT NEXT LOGIN")
    return redirect(url_for("core.admin_users"))


@bp.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_set_role(user_id):
    role = request.form.get("role", "")
    if role not in ROLES:
        flash("ERROR: INVALID ROLE")
        return redirect(url_for("core.admin_users"))
    with get_db() as conn:
        row = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or row[1] == "system":
            abort(404)
        if row[1] == role:
            return redirect(url_for("core.admin_users"))
        if row[1] == "admin" and _admin_count(conn) <= 1:
            flash("ERROR: CANNOT REMOVE THE LAST ADMIN")
            return redirect(url_for("core.admin_users"))
        conn.execute(
            "UPDATE users SET role = ?, session_version = session_version + 1 WHERE id = ?",
            (role, user_id)
        )
    log_audit(g.user_id, f"set_role_{role}", "user", user_id)
    logger.info("Auth: admin '%s' set role of '%s' to %s", session.get("username"), row[0], role)
    if user_id == g.user_id:
        session.clear()
        return redirect(url_for("core.login"))
    flash(f"{row[0]} IS NOW {role.upper()}")
    return redirect(url_for("core.admin_users"))


@bp.route("/logout")
def logout():
    """Terminate the active session and return to the login gateway."""
    username = session.get("username", "unknown")
    user_id  = session.get("user_id")
    if user_id:
        log_audit(user_id, "logout", "user", user_id)
    session.clear()
    logger.info("Auth: user '%s' logged out", username)
    return redirect(url_for("core.login"))
