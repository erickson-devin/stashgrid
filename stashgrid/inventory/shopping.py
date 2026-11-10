"""Shopping lists, auto-add on low stock, restock and the public token routes (moved from app.py, SG-06).

SG-32: every query is scoped to a household — the current one for logged-in
routes, and for the public routes the household (and optionally the single
list) that the share link was created for (SG-35).
"""
from datetime import datetime

from flask import request, render_template, jsonify, abort

from stashgrid.db import get_db
from stashgrid.config import logger
from stashgrid.core.security import admin_required
from stashgrid.core.sharing import resolve_share_token
from stashgrid.core.tenancy import current_household_id, require_list
from stashgrid.inventory import bp


def _auto_add_to_shopping_list(conn, household_id, barcode, name, preferred_store="Costco", inventory_id=None):
    """Auto-insert a low-stock item into the best-matching shopping list of the household.
    inventory_id is the row that ran low; restock puts the stock back there (SG-14).
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
            "SELECT id FROM shopping_lists WHERE household_id = ? AND LOWER(store_name) = LOWER(?) LIMIT 1",
            (household_id, preferred_store)
        ).fetchone()
        if match:
            list_id = match[0]

    # 2. Fall back to the default list
    if list_id is None:
        default_list = conn.execute(
            "SELECT id FROM shopping_lists WHERE household_id = ? AND is_default = 1 LIMIT 1",
            (household_id,)
        ).fetchone()
        if default_list:
            list_id = default_list[0]

    # 3. No lists exist at all — create one named after the preferred store
    if list_id is None:
        cursor = conn.execute(
            "INSERT INTO shopping_lists (name, store_name, is_default, household_id) VALUES (?, ?, 1, ?)",
            (preferred_store or "Shopping List", preferred_store or "", household_id)
        )
        list_id = cursor.lastrowid

    existing = conn.execute(
        "SELECT id FROM shopping_list_items WHERE household_id = ? AND list_id = ? AND barcode = ? AND is_completed = 0",
        (household_id, list_id, barcode)
    ).fetchone()

    if existing:
        if inventory_id is not None:
            conn.execute(
                "UPDATE shopping_list_items SET inventory_id = ? WHERE id = ? AND household_id = ? "
                "AND inventory_id IS NULL",
                (inventory_id, existing[0], household_id),
            )
    else:
        conn.execute(
            "INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty, is_completed, household_id, inventory_id) "
            "VALUES (?, ?, ?, 1, 0, ?, ?)",
            (list_id, barcode, name, household_id, inventory_id)
        )
        logger.info(
            "Auto-added '%s' (%s) to shopping list %d (store: %s, low stock triggered)",
            name, barcode, list_id, preferred_store or "default"
        )


def _get_shopping_lists_payload(household_id, only_list_id=None):
    """Shared data-fetch used by both the local and public shopping list APIs.
    only_list_id limits it to one list (single-list share links)."""
    scope = " AND id = ?" if only_list_id else ""
    item_scope = " AND list_id = ?" if only_list_id else ""
    extra = (only_list_id,) if only_list_id else ()
    with get_db() as conn:
        lists = conn.execute(
            "SELECT id, name, store_name, is_default FROM shopping_lists WHERE household_id = ?" + scope +
            " ORDER BY is_default DESC, name ASC",
            (household_id,) + extra
        ).fetchall()
        items = conn.execute(
            "SELECT id, list_id, barcode, name, requested_qty, is_completed "
            "FROM shopping_list_items WHERE household_id = ?" + item_scope + " ORDER BY is_completed ASC, id ASC",
            (household_id,) + extra
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


def _create_list(household_id, name, store_name):
    with get_db() as conn:
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM shopping_lists WHERE household_id = ?", (household_id,)
        ).fetchone()[0]
        is_default = 1 if existing_count == 0 else 0
        cursor = conn.execute(
            "INSERT INTO shopping_lists (name, store_name, is_default, household_id) VALUES (?, ?, ?, ?)",
            (name, store_name, is_default, household_id)
        )
        return cursor.lastrowid


def _toggle_item(conn, household_id, item_id, only_list_id=None):
    row = conn.execute(
        "SELECT is_completed, list_id FROM shopping_list_items WHERE id = ? AND household_id = ?",
        (item_id, household_id)
    ).fetchone()
    if not row or (only_list_id and row[1] != only_list_id):
        abort(404)
    new_state = 0 if row[0] else 1
    conn.execute(
        "UPDATE shopping_list_items SET is_completed = ? WHERE id = ? AND household_id = ?",
        (new_state, item_id, household_id)
    )
    return new_state


# ── Shopping List Routes ──────────────────────────────────────

@bp.route("/api/shopping-lists")
def get_shopping_lists():
    return jsonify({"lists": _get_shopping_lists_payload(current_household_id())})


@bp.route("/api/shopping-lists/create", methods=["POST"])
def create_shopping_list():
    data = request.get_json(silent=True) or {}
    name = (request.form.get("name") or data.get("name", "")).strip()
    store_name = (request.form.get("store_name") or data.get("store_name", "")).strip()
    if not name:
        return jsonify({"error": "List name is required"}), 400
    list_id = _create_list(current_household_id(), name, store_name)
    logger.info("Created shopping list '%s' (id=%d)", name, list_id)
    return jsonify({"ok": True, "list_id": list_id})


@bp.route("/api/shopping-lists/add", methods=["POST"])
def add_to_shopping_list():
    data = request.get_json(silent=True) or {}
    list_id = int(request.form.get("list_id") or data.get("list_id") or 0)
    barcode = (request.form.get("barcode") or data.get("barcode", "")).strip().upper()
    name = (request.form.get("name") or data.get("name", "")).strip()
    qty = int(request.form.get("qty") or data.get("qty") or 1)
    if not list_id or not barcode:
        return jsonify({"error": "list_id and barcode are required"}), 400
    hid = current_household_id()
    with get_db() as conn:
        require_list(conn, list_id)
        existing = conn.execute(
            "SELECT id FROM shopping_list_items WHERE household_id = ? AND list_id = ? AND barcode = ? AND is_completed = 0",
            (hid, list_id, barcode)
        ).fetchone()
        if existing:
            return jsonify({"ok": True, "already_exists": True})
        conn.execute(
            "INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty, is_completed, household_id) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (list_id, barcode, name or barcode, qty, hid)
        )
    return jsonify({"ok": True, "already_exists": False})


@bp.route("/api/shopping-lists/toggle-complete/<int:item_id>", methods=["POST"])
def toggle_shopping_item(item_id):
    with get_db() as conn:
        new_state = _toggle_item(conn, current_household_id(), item_id)
    return jsonify({"ok": True, "is_completed": new_state})


def _restock_target(conn, household_id, barcode, inventory_id):
    """Which inventory row a bought item goes back to (SG-13/14), as
    (row id, reactivate) or None if it has no known location.

    1. The row that ran low (remembered when the item was auto-added), even if
       it was soft-deleted when its last unit was removed.
    2. Otherwise the most recently updated row for the barcode that still has
       a room and shelf, active or not.
    A removed row is only revived if nothing active has been scanned onto the
    same shelf since; otherwise the stock goes to that active row.
    """
    cols = "SELECT id, barcode, room_id, shelf_id, is_active FROM inventory WHERE household_id = ? "
    row = None
    if inventory_id is not None:
        row = conn.execute(cols + "AND id = ?", (household_id, inventory_id)).fetchone()
    if row is None or row[2] is None or row[3] is None:
        if not barcode:
            return None
        row = conn.execute(
            cols + "AND barcode = ? AND room_id IS NOT NULL AND shelf_id IS NOT NULL "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (household_id, barcode),
        ).fetchone()
    if row is None:
        return None
    row_id, row_barcode, room_id, shelf_id, is_active = row
    if not is_active:
        active = conn.execute(
            "SELECT id FROM inventory WHERE household_id = ? AND barcode = ? AND room_id = ? AND shelf_id = ? "
            "AND is_active = 1 ORDER BY id DESC LIMIT 1",
            (household_id, row_barcode, room_id, shelf_id),
        ).fetchone()
        if active:
            return active[0], False
    return row_id, not is_active


@bp.route("/api/shopping-lists/commit", methods=["POST"])
def commit_restock():
    data = request.get_json(silent=True) or {}
    list_id = int(request.form.get("list_id") or data.get("list_id") or 0)
    if not list_id:
        return jsonify({"error": "list_id is required"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hid = current_household_id()
    restocked = []
    unknown = []
    with get_db() as conn:
        require_list(conn, list_id)
        completed = conn.execute(
            "SELECT id, barcode, name, requested_qty, inventory_id FROM shopping_list_items "
            "WHERE household_id = ? AND list_id = ? AND is_completed = 1",
            (hid, list_id)
        ).fetchall()
        for sl_id, barcode, sl_name, qty, inventory_id in completed:
            target = _restock_target(conn, hid, barcode, inventory_id)
            if target is None:
                # No known shelf (e.g. typed in by hand): stays on the list, ticked.
                unknown.append({"barcode": barcode, "name": sl_name})
                continue
            inv_id, reactivate = target
            if reactivate:
                conn.execute(
                    "UPDATE inventory SET quantity = ?, is_active = 1, removed_at = NULL, removed_reason = NULL, "
                    "updated_at = ? WHERE id = ? AND household_id = ?",
                    (qty, now, inv_id, hid)
                )
            else:
                conn.execute(
                    "UPDATE inventory SET quantity = quantity + ?, updated_at = ? WHERE id = ? AND household_id = ?",
                    (qty, now, inv_id, hid)
                )
            conn.execute("DELETE FROM shopping_list_items WHERE id = ? AND household_id = ?", (sl_id, hid))
            restocked.append({"barcode": barcode, "name": sl_name, "qty": qty, "inventory_id": inv_id,
                              "reactivated": reactivate})
            logger.info("Restocked %s x%d into item %d via shopping list commit%s", barcode, qty, inv_id,
                        " (back in stock)" if reactivate else "")
    return jsonify({"ok": True, "restocked": restocked, "unknown_barcodes": unknown})


@bp.route("/api/shopping-lists/<int:list_id>/set-default", methods=["POST"])
def set_default_shopping_list(list_id):
    hid = current_household_id()
    with get_db() as conn:
        require_list(conn, list_id)
        conn.execute("UPDATE shopping_lists SET is_default = 0 WHERE household_id = ?", (hid,))
        conn.execute("UPDATE shopping_lists SET is_default = 1 WHERE id = ? AND household_id = ?", (list_id, hid))
    return jsonify({"ok": True})


@bp.route("/api/shopping-lists/<int:list_id>/rename", methods=["POST"])
def rename_shopping_list(list_id):
    data = request.get_json(silent=True) or {}
    name = (request.form.get("name") or data.get("name", "")).strip()
    store_name = (request.form.get("store_name") or data.get("store_name", "")).strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    hid = current_household_id()
    with get_db() as conn:
        require_list(conn, list_id)
        conn.execute(
            "UPDATE shopping_lists SET name = ?, store_name = ? WHERE id = ? AND household_id = ?",
            (name, store_name, list_id, hid)
        )
    return jsonify({"ok": True})


@bp.route("/api/shopping-lists/<int:list_id>/delete", methods=["POST"])
@admin_required
def delete_shopping_list(list_id):
    hid = current_household_id()
    with get_db() as conn:
        lst = require_list(conn, list_id)
        # Explicitly delete items first (FK cascade requires PRAGMA foreign_keys=ON)
        conn.execute("DELETE FROM shopping_list_items WHERE list_id = ? AND household_id = ?", (list_id, hid))
        conn.execute("DELETE FROM shopping_lists WHERE id = ? AND household_id = ?", (list_id, hid))
        # Promote another list to default if we just deleted the default
        if lst[1] == 1:
            next_lst = conn.execute(
                "SELECT id FROM shopping_lists WHERE household_id = ? LIMIT 1", (hid,)
            ).fetchone()
            if next_lst:
                conn.execute(
                    "UPDATE shopping_lists SET is_default = 1 WHERE id = ? AND household_id = ?",
                    (next_lst[0], hid)
                )
    logger.info("Deleted shopping list %d", list_id)
    return jsonify({"ok": True})


# ── Public Shopping List Routes (token-secured, Cloudflare-exposed) ───────────

def _public_scope(token):
    """Resolve a share link (SG-35) to (household_id, list_id or None).
    Unknown, revoked or expired links -> 404."""
    with get_db() as conn:
        scope = resolve_share_token(conn, token)
    if not scope:
        abort(404)
    return scope


@bp.route("/shop/<token>")
def shopping_view(token):
    """Public read-only shopping list view — token-secured, Cloudflare-exposed."""
    _public_scope(token)
    return render_template("shopping_view.html", token=token)


@bp.route("/api/public/shopping-lists/<token>")
def public_shopping_lists(token):
    """Public API: return shopping list data. Token-secured."""
    hid, only_list = _public_scope(token)
    return jsonify({"lists": _get_shopping_lists_payload(hid, only_list)})


@bp.route("/api/public/shopping-lists/toggle/<token>/<int:item_id>", methods=["POST"])
def public_toggle_item(token, item_id):
    """Public API: toggle a shopping list item's completion state. Token-secured."""
    hid, only_list = _public_scope(token)
    with get_db() as conn:
        new_state = _toggle_item(conn, hid, item_id, only_list)
    logger.info("Public toggle: item %d → is_completed=%d", item_id, new_state)
    return jsonify({"ok": True, "is_completed": new_state})


@bp.route("/api/public/shopping-lists/create/<token>", methods=["POST"])
def public_create_list(token):
    """Public API: create a new shopping list. Token-secured.
    Single-list links can't create lists."""
    hid, only_list = _public_scope(token)
    if only_list:
        abort(404)
    data = request.get_json(silent=True) or {}
    name = (request.form.get("name") or data.get("name", "")).strip()
    store_name = (request.form.get("store_name") or data.get("store_name", "")).strip()
    if not name:
        return jsonify({"error": "List name is required"}), 400
    list_id = _create_list(hid, name, store_name)
    logger.info("Public created shopping list '%s' (id=%d)", name, list_id)
    return jsonify({"ok": True, "list_id": list_id})


@bp.route("/api/public/shopping-lists/add/<token>", methods=["POST"])
def public_add_item(token):
    """Public API: add an item to a shopping list. Token-secured."""
    hid, only_list = _public_scope(token)
    data = request.get_json(silent=True) or {}
    list_id = int(request.form.get("list_id") or data.get("list_id") or 0)
    name = (request.form.get("name") or data.get("name", "")).strip()
    barcode = (request.form.get("barcode") or data.get("barcode", "")).strip().upper()
    qty = int(request.form.get("qty") or data.get("qty") or 1)
    if not list_id or not name:
        return jsonify({"error": "list_id and name are required"}), 400
    if only_list and list_id != only_list:
        return jsonify({"error": "List not found"}), 404
    # Use barcode if provided, otherwise generate a placeholder
    effective_barcode = barcode if barcode else f"MANUAL-{list_id}-{int(datetime.now().timestamp())}"
    with get_db() as conn:
        # Verify the list exists in this household
        if not conn.execute(
            "SELECT id FROM shopping_lists WHERE id = ? AND household_id = ?", (list_id, hid)
        ).fetchone():
            return jsonify({"error": "List not found"}), 404
        existing = conn.execute(
            "SELECT id FROM shopping_list_items WHERE household_id = ? AND list_id = ? "
            "AND LOWER(name) = LOWER(?) AND is_completed = 0",
            (hid, list_id, name)
        ).fetchone()
        if existing:
            return jsonify({"ok": True, "already_exists": True})
        conn.execute(
            "INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty, is_completed, household_id) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (list_id, effective_barcode, name, qty, hid)
        )
    logger.info("Public added item '%s' to list %d", name, list_id)
    return jsonify({"ok": True, "already_exists": False})
