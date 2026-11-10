"""Household settings (SG-35): share links and scanner API keys.

Owners and admins create, list and revoke them here. A new secret is shown
exactly once, in the response to the request that created it; afterwards only
its label, scope, prefix and usage are visible.
"""
from flask import abort, flash, g, redirect, render_template, request, url_for

from stashgrid import config
from stashgrid.config import logger
from stashgrid.core import bp
from stashgrid.core.audit import log_audit
from stashgrid.core.security import admin_required
from stashgrid.core.sharing import create_api_key, create_share_link
from stashgrid.core.tenancy import current_household_id
from stashgrid import db
from stashgrid.db import get_db

EXPIRY_CHOICES = {"never": None, "7": 7, "30": 30, "90": 90}


def _public_url(path):
    base = config.PUBLIC_BASE_URL or request.host_url.rstrip("/")
    return base + path


def _render(new_link=None, new_key=None, status=200):
    hid = current_household_id()
    with get_db() as conn:
        links = conn.execute(
            "SELECT s.id, s.label, s.list_id, l.name, s.created_at, s.expires_at, s.last_used_at, "
            "       CASE WHEN s.expires_at IS NOT NULL AND s.expires_at <= ? THEN 1 ELSE 0 END "
            "FROM share_links s LEFT JOIN shopping_lists l ON l.id = s.list_id "
            "WHERE s.household_id = ? AND s.revoked_at IS NULL ORDER BY s.created_at DESC",
            (db.now(), hid),
        ).fetchall()
        keys = conn.execute(
            "SELECT id, label, key_prefix, created_at, last_used_at FROM api_keys "
            "WHERE household_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
            (hid,),
        ).fetchall()
        lists = conn.execute(
            "SELECT id, name FROM shopping_lists WHERE household_id = ? ORDER BY LOWER(name)", (hid,)
        ).fetchall()
    return render_template(
        "settings.html", links=links, keys=keys, lists=lists, new_link=new_link, new_key=new_key,
        base_url_configured=bool(config.PUBLIC_BASE_URL),
    ), status


@bp.route("/settings")
@admin_required
def settings():
    return _render()


@bp.route("/settings/links", methods=["POST"])
@admin_required
def create_link():
    hid = current_household_id()
    label = request.form.get("label", "").strip()[:60] or "Shopping link"
    scope = request.form.get("scope", "all")
    expires = EXPIRY_CHOICES.get(request.form.get("expires", "never"), None)
    list_id = None
    with get_db() as conn:
        if scope != "all":
            if not scope.isdigit():
                abort(400)
            list_id = int(scope)
            if not conn.execute(
                "SELECT 1 FROM shopping_lists WHERE id = ? AND household_id = ?", (list_id, hid)
            ).fetchone():
                abort(404)
        token = create_share_link(conn, hid, label, list_id=list_id, expires_days=expires, created_by=g.user_id)
        log_audit(g.user_id, "create_share_link", "household", hid, conn=conn)
    logger.info("Share link '%s' created for household %d (list=%s)", label, hid, list_id or "all")
    return _render(new_link=_public_url(url_for("inventory.shopping_view", token=token)))


@bp.route("/settings/links/<int:link_id>/revoke", methods=["POST"])
@admin_required
def revoke_link(link_id):
    hid = current_household_id()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE share_links SET revoked_at = ? WHERE id = ? AND household_id = ? "
            "AND revoked_at IS NULL", (db.now(), link_id, hid),
        )
        if cur.rowcount != 1:
            abort(404)
        log_audit(g.user_id, "revoke_share_link", "household", hid, conn=conn)
    flash("SHARE LINK REVOKED // IT STOPS WORKING IMMEDIATELY")
    return redirect(url_for("core.settings"))


@bp.route("/settings/keys", methods=["POST"])
@admin_required
def create_key():
    hid = current_household_id()
    label = request.form.get("label", "").strip()[:60] or "Scanner"
    with get_db() as conn:
        key = create_api_key(conn, hid, label, created_by=g.user_id)
        log_audit(g.user_id, "create_api_key", "household", hid, conn=conn)
    logger.info("Scanner key '%s' created for household %d", label, hid)
    return _render(new_key=key)


@bp.route("/settings/keys/<int:key_id>/revoke", methods=["POST"])
@admin_required
def revoke_key(key_id):
    hid = current_household_id()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND household_id = ? "
            "AND revoked_at IS NULL", (db.now(), key_id, hid),
        )
        if cur.rowcount != 1:
            abort(404)
        log_audit(g.user_id, "revoke_api_key", "household", hid, conn=conn)
    flash("SCANNER KEY REVOKED // IT STOPS WORKING IMMEDIATELY")
    return redirect(url_for("core.settings"))
