"""Email accounts (SG-33): sign-up, email verification, password reset,
household invites, the account page and household switching.

Rules that keep these flows safe:
- Emailed tokens are single-use and expire (see core/tokens.py); only their
  hash is stored.
- "Forgot password", "resend verification" and sign-up never reveal whether
  an email has an account: the response is the same either way.
- Every form keeps CSRF protection; the endpoints that send email are
  rate-limited.
"""
import re

from argon2.exceptions import VerifyMismatchError, InvalidHashError
from flask import abort, flash, g, redirect, render_template, request, session, url_for

from stashgrid import config
from stashgrid.config import logger
from stashgrid.core import bp
from stashgrid.core import tokens
from stashgrid.core.audit import log_audit
from stashgrid.core.security import (
    MIN_PASSWORD_LENGTH, Throttle, _clear_login_failures, _password_problem, admin_required,
)
from stashgrid.core.tenancy import current_household_id, load_membership
from stashgrid import db
from stashgrid.db import get_db
from stashgrid.extensions import password_hasher as _ph
from stashgrid.services.mail import send_mail

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
INVITE_ROLES = ("member", "admin", "owner")

signup_ip_throttle = Throttle(5, 3600)
email_ip_throttle = Throttle(10, 900)        # forgot / resend, per IP
email_addr_throttle = Throttle(3, 3600)      # forgot / resend, per address
invite_throttle = Throttle(30, 86400)        # invites sent, per user


# ── helpers ──────────────────────────────────────────────────────────────────

def _norm_email(value):
    value = (value or "").strip().lower()
    return value if EMAIL_RE.match(value) and len(value) <= 254 else None


def _abs_url(endpoint, **values):
    path = url_for(endpoint, **values)
    base = config.PUBLIC_BASE_URL or request.host_url.rstrip("/")
    return base + path


def _ip():
    return request.remote_addr or "?"


def _new_username(conn, email):
    """Usernames stay unique (and usable for login); derive one from the email."""
    base = re.sub(r"[^a-z0-9_.-]", "", email.split("@")[0].lower())[:24]
    if len(base) < 3:
        base = (base + "user")[:24]
    candidate, n = base, 1
    while conn.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{base}{n}"
    return candidate


def _start_session(user_id, username, household_id, role, household_name, session_version=0):
    session.clear()
    session.permanent = True
    session.update(user_id=user_id, username=username, household_id=household_id, role=role,
                   household_name=household_name, session_version=session_version,
                   must_change_password=False)


def _render_message(title, message, *, status=200, link=None, link_text=None, show_resend=False):
    return render_template("account_message.html", title=title, message=message, link=link,
                           link_text=link_text, show_resend=show_resend), status


def _check_email_page():
    return _render_message(
        "Check Your Email",
        "If that address can be used, we've sent it a message with a link. "
        "The link expires, so use it soon. Nothing arrived? Check spam, or request a new link.",
        link=url_for("core.login"), link_text="Back to login", show_resend=True)


def _password_errors(new, confirm, label):
    if new != confirm:
        return "PASSWORDS DO NOT MATCH"
    return _password_problem(new, label)


# ── emails ───────────────────────────────────────────────────────────────────

def _send_verify(email, raw):
    return send_mail(email, "Confirm your StashGrid email",
              "Confirm this email address for your StashGrid account:\n\n"
              f"{_abs_url('core.verify_email', token=raw)}\n\n"
              "The link expires in 48 hours. If you didn't ask for this, ignore this email.\n")


def _send_reset(email, raw):
    return send_mail(email, "Reset your StashGrid password",
              "Someone asked to reset the password for the StashGrid account using this email.\n\n"
              f"Choose a new password: {_abs_url('core.reset_password', token=raw)}\n\n"
              "The link expires in 1 hour and works once. If it wasn't you, ignore this email; "
              "your password hasn't changed.\n")


def _send_account_exists(email):
    return send_mail(email, "You already have a StashGrid account",
              "Someone tried to sign up for StashGrid with this email, but it already has an account.\n\n"
              f"Log in: {_abs_url('core.login')}\n"
              f"Forgot your password? {_abs_url('core.forgot_password')}\n\n"
              "If this wasn't you, you can ignore this email.\n")


def _send_invite(email, raw, inviter, household_name, role):
    return send_mail(email, f"{inviter} invited you to {household_name} on StashGrid",
              f"{inviter} invited you to join the household \"{household_name}\" on StashGrid as {role}.\n\n"
              f"Accept the invitation: {_abs_url('core.accept_invite', token=raw)}\n\n"
              "The link expires in 7 days and works once.\n")


# ── sign-up and verification ─────────────────────────────────────────────────

@bp.app_context_processor
def _account_template_globals():
    return {"signup_enabled": config.SIGNUP_ENABLED}


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if not config.SIGNUP_ENABLED:
        abort(404)
    if session.get("user_id"):
        return redirect(url_for("inventory.index"))
    error = None
    form = request.form
    if request.method == "POST":
        if signup_ip_throttle.hit(_ip()):
            return _render_message("Slow Down", "Too many sign-ups from this network. Try again later.", status=429)
        name = form.get("display_name", "").strip()[:60]
        household = form.get("household_name", "").strip()[:60] or (f"{name}'s household" if name else "My household")
        email = _norm_email(form.get("email"))
        password, confirm = form.get("password", ""), form.get("confirm_password", "")
        if not name:
            error = "ENTER YOUR NAME"
        elif not email:
            error = "ENTER A VALID EMAIL ADDRESS"
        else:
            error = _password_errors(password, confirm, email.split("@")[0])
        if not error:
            with get_db() as conn:
                if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                    # Don't reveal that the address is registered; tell its owner instead.
                    _send_account_exists(email)
                    logger.info("Signup: attempt for an existing email")
                    return _check_email_page()
                hid = conn.execute("INSERT INTO households (name) VALUES (?)", (household,)).lastrowid
                uid = conn.execute(
                    "INSERT INTO users (username, password_hash, role, email, display_name) "
                    "VALUES (?, ?, 'user', ?, ?)",
                    (_new_username(conn, email), _ph.hash(password), email, name),
                ).lastrowid
                conn.execute(
                    "INSERT INTO household_members (household_id, user_id, role) VALUES (?, ?, 'owner')",
                    (hid, uid),
                )
                raw = tokens.create(conn, "verify_email", tokens.VERIFY_TTL, user_id=uid, email=email)
                log_audit(uid, "signup", "household", hid, conn=conn, household_id=hid)
            _send_verify(email, raw)
            logger.info("Signup: new account %d with household %d (awaiting email verification)", uid, hid)
            return _check_email_page()
    return render_template("signup.html", error=error, form=form, min_len=MIN_PASSWORD_LENGTH)


@bp.route("/verify/<token>")
def verify_email(token):
    with get_db() as conn:
        t = tokens.find(conn, token, "verify_email")
        if not t or not tokens.consume(conn, t["id"]):
            return _render_message("Link Expired",
                                   "This confirmation link is invalid, already used, or expired. "
                                   "Request a new one below.", status=400, show_resend=True)
        taken = conn.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (t["email"], t["user_id"])).fetchone()
        if taken:
            return _render_message("Email In Use", "That email address now belongs to another account.", status=409)
        conn.execute(
            "UPDATE users SET email = ?, email_verified_at = ? WHERE id = ?",
            (t["email"], db.now(), t["user_id"]),
        )
        log_audit(t["user_id"], "verify_email", "user", t["user_id"], conn=conn)
    flash("EMAIL CONFIRMED")
    return redirect(url_for("inventory.index") if session.get("user_id") else url_for("core.login"))


@bp.route("/verify/resend", methods=["POST"])
def resend_verification():
    email = _norm_email(request.form.get("email"))
    if email_ip_throttle.hit(_ip()) or (email and email_addr_throttle.hit(email)):
        return _render_message("Slow Down", "Too many requests. Try again later.", status=429)
    if email:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE email = ? AND email_verified_at IS NULL", (email,)
            ).fetchone()
            if row:
                tokens.revoke(conn, "verify_email", user_id=row[0])
                raw = tokens.create(conn, "verify_email", tokens.VERIFY_TTL, user_id=row[0], email=email)
        if row:
            _send_verify(email, raw)
    return _check_email_page()


# ── password reset ───────────────────────────────────────────────────────────

@bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = _norm_email(request.form.get("email"))
        if email_ip_throttle.hit(_ip()) or (email and email_addr_throttle.hit(email)):
            return _render_message("Slow Down", "Too many requests. Try again later.", status=429)
        if email:
            raw = None
            with get_db() as conn:
                row = conn.execute("SELECT id FROM users WHERE email = ? AND role != 'system'", (email,)).fetchone()
                if row:
                    tokens.revoke(conn, "reset_password", user_id=row[0])
                    raw = tokens.create(conn, "reset_password", tokens.RESET_TTL, user_id=row[0], email=email)
            if raw:
                _send_reset(email, raw)
                logger.info("Password reset link sent for user %d", row[0])
        return _check_email_page()
    return render_template("forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    with get_db() as conn:
        t = tokens.find(conn, token, "reset_password")
    if not t:
        return _render_message("Link Expired",
                               "This reset link is invalid, already used, or expired.",
                               status=400, link=url_for("core.forgot_password"), link_text="Request a new link")
    error = None
    if request.method == "POST":
        new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
        with get_db() as conn:
            username = conn.execute("SELECT username FROM users WHERE id = ?", (t["user_id"],)).fetchone()[0]
            error = _password_errors(new, confirm, username)
            if not error:
                if not tokens.consume(conn, t["id"]):
                    return _render_message("Link Expired", "This reset link was already used.", status=400)
                tokens.revoke(conn, "reset_password", user_id=t["user_id"])
                # Using the emailed link proves control of the inbox.
                conn.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = 0, "
                    "session_version = session_version + 1, "
                    "email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?",
                    (_ph.hash(new), db.now(), t["user_id"]),
                )
                log_audit(t["user_id"], "reset_password_self", "user", t["user_id"], conn=conn)
        if not error:
            _clear_login_failures(username)
            session.clear()
            flash("PASSWORD UPDATED // LOG IN WITH YOUR NEW PASSWORD")
            return redirect(url_for("core.login"))
    return render_template("reset.html", error=error, min_len=MIN_PASSWORD_LENGTH)


# ── invites ──────────────────────────────────────────────────────────────────

@bp.route("/admin/users/invite", methods=["POST"])
@admin_required
def admin_invite_user():
    email = _norm_email(request.form.get("email"))
    role = request.form.get("role", "member")
    hid = current_household_id()
    if not email:
        flash("ERROR: ENTER A VALID EMAIL ADDRESS")
    elif role not in INVITE_ROLES:
        flash("ERROR: INVALID ROLE")
    elif role == "owner" and g.household_role != "owner":
        flash("ERROR: ONLY AN OWNER CAN INVITE ANOTHER OWNER")
    elif invite_throttle.hit(g.user_id):
        flash("ERROR: TOO MANY INVITES TODAY // TRY AGAIN TOMORROW")
    else:
        with get_db() as conn:
            member = conn.execute(
                "SELECT 1 FROM household_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.household_id = ? AND u.email = ?", (hid, email)
            ).fetchone()
            if member:
                flash("ERROR: THAT PERSON IS ALREADY IN THIS HOUSEHOLD")
                return redirect(url_for("core.admin_users"))
            tokens.revoke(conn, "invite", email=email, household_id=hid)
            raw = tokens.create(conn, "invite", tokens.INVITE_TTL, email=email, household_id=hid,
                                role=role, created_by=g.user_id)
            household_name = conn.execute("SELECT name FROM households WHERE id = ?", (hid,)).fetchone()[0]
            log_audit(g.user_id, "invite", "household", hid, conn=conn)
        sent = _send_invite(email, raw, session.get("display_name") or session.get("username"), household_name, role)
        flash(f"INVITE SENT TO {email}" if sent else "ERROR: COULD NOT SEND THE INVITE EMAIL")
    return redirect(url_for("core.admin_users"))


@bp.route("/admin/invites/<int:invite_id>/revoke", methods=["POST"])
@admin_required
def admin_revoke_invite(invite_id):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE auth_tokens SET used_at = ? WHERE id = ? AND purpose = 'invite' "
            "AND household_id = ? AND used_at IS NULL",
            (db.now(), invite_id, current_household_id()),
        )
        if cur.rowcount != 1:
            abort(404)
    flash("INVITE REVOKED")
    return redirect(url_for("core.admin_users"))


def _invite_context(conn, t):
    household = conn.execute("SELECT name FROM households WHERE id = ?", (t["household_id"],)).fetchone()
    inviter = conn.execute(
        "SELECT COALESCE(display_name, username) FROM users WHERE id = ?", (t["created_by"],)
    ).fetchone() if t["created_by"] else None
    return {"household_name": household[0] if household else "?", "inviter": inviter[0] if inviter else "Someone",
            "email": t["email"], "role": t["role"]}


@bp.route("/invite/<token>", methods=["GET", "POST"])
def accept_invite(token):
    with get_db() as conn:
        t = tokens.find(conn, token, "invite")
        ctx = _invite_context(conn, t) if t else None
        email_taken = bool(t) and conn.execute("SELECT 1 FROM users WHERE email = ?", (t["email"],)).fetchone() is not None
    if not t:
        return _render_message("Invitation Expired",
                               "This invitation is invalid, already used, revoked, or expired. "
                               "Ask for a new one.", status=400)
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "join" and g.get("user_id"):
            with get_db() as conn:
                if not tokens.consume(conn, t["id"]):
                    return _render_message("Invitation Expired", "This invitation was already used.", status=400)
                conn.execute(
                    "INSERT INTO household_members (household_id, user_id, role) VALUES (?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (t["household_id"], g.user_id, t["role"]),
                )
                log_audit(g.user_id, "join_household", "household", t["household_id"], conn=conn,
                          household_id=t["household_id"])
            session["household_id"] = t["household_id"]
            flash(f"YOU JOINED {ctx['household_name']}")
            return redirect(url_for("inventory.index"))
        if action == "create" and not g.get("user_id"):
            name = request.form.get("display_name", "").strip()[:60]
            new, confirm = request.form.get("password", ""), request.form.get("confirm_password", "")
            if email_taken:
                error = "AN ACCOUNT ALREADY USES THIS EMAIL // LOG IN TO ACCEPT"
            elif not name:
                error = "ENTER YOUR NAME"
            else:
                error = _password_errors(new, confirm, t["email"].split("@")[0])
            if not error:
                try:
                    with get_db() as conn:
                        if not tokens.consume(conn, t["id"]):
                            return _render_message("Invitation Expired", "This invitation was already used.", status=400)
                        username = _new_username(conn, t["email"])
                        uid = conn.execute(
                            "INSERT INTO users (username, password_hash, role, email, email_verified_at, display_name) "
                            "VALUES (?, ?, 'user', ?, ?, ?)",
                            (username, _ph.hash(new), t["email"], db.now(), name),
                        ).lastrowid
                        conn.execute(
                            "INSERT INTO household_members (household_id, user_id, role) VALUES (?, ?, ?)",
                            (t["household_id"], uid, t["role"]),
                        )
                        log_audit(uid, "join_household", "household", t["household_id"], conn=conn,
                                  household_id=t["household_id"])
                except db.IntegrityError:
                    error = "AN ACCOUNT ALREADY USES THIS EMAIL // LOG IN TO ACCEPT"
                else:
                    _start_session(uid, username, t["household_id"], t["role"], ctx["household_name"])
                    flash(f"WELCOME TO {ctx['household_name']}")
                    return redirect(url_for("inventory.index"))
    return render_template("invite.html", error=error, token=token, email_taken=email_taken,
                           min_len=MIN_PASSWORD_LENGTH, **ctx)


# ── account page: email, households ──────────────────────────────────────────

@bp.route("/account", methods=["GET", "POST"])
def account():
    with get_db() as conn:
        user = conn.execute(
            "SELECT username, email, email_verified_at, display_name, password_hash FROM users WHERE id = ?",
            (g.user_id,)
        ).fetchone()
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "email":
            email = _norm_email(request.form.get("email"))
            try:
                _ph.verify(user[4], request.form.get("current_password", ""))
                ok = True
            except (VerifyMismatchError, InvalidHashError):
                ok = False
            if not ok:
                error = "CURRENT PASSWORD INCORRECT"
            elif not email:
                error = "ENTER A VALID EMAIL ADDRESS"
            elif email_addr_throttle.hit(email):
                error = "TOO MANY REQUESTS // TRY AGAIN LATER"
            else:
                with get_db() as conn:
                    tokens.revoke(conn, "verify_email", user_id=g.user_id)
                    raw = tokens.create(conn, "verify_email", tokens.VERIFY_TTL, user_id=g.user_id, email=email)
                _send_verify(email, raw)
                flash(f"CHECK {email} FOR A CONFIRMATION LINK // THE CHANGE APPLIES ONCE CONFIRMED")
                return redirect(url_for("core.account"))
        elif action == "name":
            name = request.form.get("display_name", "").strip()[:60]
            with get_db() as conn:
                conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name or None, g.user_id))
            flash("NAME UPDATED")
            return redirect(url_for("core.account"))
    with get_db() as conn:
        households = conn.execute(
            "SELECT h.id, h.name, m.role FROM household_members m JOIN households h ON h.id = m.household_id "
            "WHERE m.user_id = ? ORDER BY LOWER(h.name)", (g.user_id,)
        ).fetchall()
    return render_template("account.html", error=error, user=user, households=households,
                           current_household=session.get("household_id"))


@bp.route("/account/household", methods=["POST"])
def switch_household():
    hid = request.form.get("household_id", type=int)
    with get_db() as conn:
        membership = load_membership(conn, g.user_id, hid)
    if not membership or membership[0] != hid:
        abort(404)
    session["household_id"] = hid
    flash(f"SWITCHED TO {membership[2]}")
    return redirect(url_for("inventory.index"))
