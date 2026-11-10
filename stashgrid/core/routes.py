"""Login, logout, password change and user management routes (moved from app.py, SG-06)."""

from argon2.exceptions import VerifyMismatchError, InvalidHashError
from flask import request, render_template, redirect, session, url_for, abort, g, flash

from stashgrid import db
from stashgrid.db import get_db
from stashgrid.config import logger
from stashgrid.core import bp
from stashgrid.core.audit import log_audit
from stashgrid.extensions import password_hasher as _ph
from stashgrid.core.tenancy import current_household_id, load_membership
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
            # The login field takes a username or an email address (SG-33).
            row = conn.execute(
                "SELECT id, username, password_hash, role, must_change_password, session_version, "
                "email, email_verified_at "
                "FROM users WHERE username = ? OR (email IS NOT NULL AND email = ?)",
                (username, username.lower())
            ).fetchone()
            membership = load_membership(conn, row[0]) if row else None
        # System user has no password and cannot log in
        if row and row[2] and row[3] != "system":
            try:
                _ph.verify(row[2], password)
                # ✅ Credentials valid — establish session
                _clear_login_failures(username)
                if row[6] and not row[7]:
                    # Self-service sign-up: the email must be verified first.
                    return render_template(
                        "login.html", unverified_email=row[6],
                        error="EMAIL NOT VERIFIED // CHECK YOUR INBOX FOR THE LINK"), 403
                if not membership:
                    # SG-32: every account must belong to a household.
                    logger.warning("Auth: user '%s' has no household; login refused", row[1])
                    return render_template(
                        "login.html", error="ACCOUNT NOT LINKED TO A HOUSEHOLD // ASK AN ADMIN"), 403
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
                session["household_id"], session["role"], session["household_name"] = membership
                session["session_version"] = row[5]
                session["must_change_password"] = must_change
                logger.info("Auth: user '%s' (household %d, role=%s) logged in",
                            row[1], membership[0], membership[1])
                log_audit(row[0], "login", "user", row[0], household_id=membership[0])
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


# ── User management (household owners/admins, SG-32) ─────────────────────────

def _owner_count(conn, household_id):
    return conn.execute(
        "SELECT COUNT(*) FROM household_members WHERE household_id = ? AND role = 'owner'",
        (household_id,),
    ).fetchone()[0]


def _require_member(conn, user_id):
    """(username, role-in-this-household) for a member of the current household, else 404."""
    row = conn.execute(
        "SELECT u.username, m.role FROM household_members m JOIN users u ON u.id = m.user_id "
        "WHERE m.household_id = ? AND m.user_id = ? AND u.role != 'system'",
        (current_household_id(), user_id),
    ).fetchone()
    if not row:
        abort(404)
    return row


@bp.route("/admin/users")
@admin_required
def admin_users():
    with get_db() as conn:
        users = conn.execute(
            "SELECT u.id, u.username, m.role, u.must_change_password, u.email, u.display_name "
            "FROM household_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.household_id = ? AND u.role != 'system' ORDER BY LOWER(u.username)",
            (current_household_id(),),
        ).fetchall()
        invites = conn.execute(
            "SELECT id, email, role, expires_at FROM auth_tokens "
            "WHERE purpose = 'invite' AND household_id = ? AND used_at IS NULL "
            "AND expires_at > ? ORDER BY created_at DESC",
            (current_household_id(), db.now()),
        ).fetchall()
    return render_template("admin_users.html", users=users, roles=ROLES, invites=invites,
                           min_len=MIN_PASSWORD_LENGTH)


@bp.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "member")
    if not USERNAME_RE.match(username) or username.startswith("__"):
        flash("ERROR: USERNAME MUST BE 3-32 CHARACTERS: LETTERS, NUMBERS, . _ -")
    elif role not in ROLES:
        flash("ERROR: INVALID ROLE")
    elif role == "owner" and g.household_role != "owner":
        flash("ERROR: ONLY AN OWNER CAN ADD ANOTHER OWNER")
    elif (problem := _password_problem(password, username)):
        flash("ERROR: " + problem)
    else:
        try:
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, role, must_change_password) "
                    "VALUES (?, ?, 'user', 1)",
                    (username, _ph.hash(password))
                )
                new_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO household_members (household_id, user_id, role) VALUES (?, ?, ?)",
                    (current_household_id(), new_id, role),
                )
            log_audit(g.user_id, "create_user", "user", new_id)
            logger.info("Auth: '%s' added user '%s' (%s) to household %d",
                        session.get("username"), username, role, current_household_id())
            flash(f"USER {username} CREATED // THEY MUST CHANGE THE PASSWORD AT FIRST LOGIN")
        except db.IntegrityError:
            flash("ERROR: USERNAME ALREADY EXISTS")
    return redirect(url_for("core.admin_users"))


@bp.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    password = request.form.get("password", "")
    with get_db() as conn:
        row = _require_member(conn, user_id)
        if row[1] == "owner" and g.household_role != "owner":
            flash("ERROR: ONLY AN OWNER CAN RESET AN OWNER'S PASSWORD")
            return redirect(url_for("core.admin_users"))
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
    hid = current_household_id()
    with get_db() as conn:
        row = _require_member(conn, user_id)
        if row[1] == role:
            return redirect(url_for("core.admin_users"))
        if (role == "owner" or row[1] == "owner") and g.household_role != "owner":
            flash("ERROR: ONLY AN OWNER CAN GRANT OR CHANGE THE OWNER ROLE")
            return redirect(url_for("core.admin_users"))
        if row[1] == "owner" and _owner_count(conn, hid) <= 1:
            flash("ERROR: CANNOT REMOVE THE LAST OWNER")
            return redirect(url_for("core.admin_users"))
        conn.execute(
            "UPDATE household_members SET role = ? WHERE household_id = ? AND user_id = ?",
            (role, hid, user_id)
        )
        conn.execute(
            "UPDATE users SET session_version = session_version + 1 WHERE id = ?", (user_id,)
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
