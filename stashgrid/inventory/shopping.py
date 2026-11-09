"""Shopping lists, auto-add on low stock, restock and the public token routes (moved from app.py, SG-06)."""
import sqlite3
from datetime import datetime

from flask import request, render_template, jsonify, abort

from stashgrid.config import DB, SHOPPING_TOKEN, logger
from stashgrid.core.security import admin_required
from stashgrid.inventory import bp


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


# ── Shopping List Routes ──────────────────────────────────────

@bp.route("/api/shopping-lists")
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


@bp.route("/api/shopping-lists/create", methods=["POST"])
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


@bp.route("/api/shopping-lists/add", methods=["POST"])
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


@bp.route("/api/shopping-lists/toggle-complete/<int:item_id>", methods=["POST"])
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


@bp.route("/api/shopping-lists/commit", methods=["POST"])
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


@bp.route("/api/shopping-lists/<int:list_id>/set-default", methods=["POST"])
def set_default_shopping_list(list_id):
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE shopping_lists SET is_default = 0")
        conn.execute("UPDATE shopping_lists SET is_default = 1 WHERE id = ?", (list_id,))
    return jsonify({"ok": True})


@bp.route("/api/shopping-lists/<int:list_id>/rename", methods=["POST"])
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


@bp.route("/api/shopping-lists/<int:list_id>/delete", methods=["POST"])
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


@bp.route("/shop/<token>")
def shopping_view(token):
    """Public read-only shopping list view — token-secured, Cloudflare-exposed."""
    if token != SHOPPING_TOKEN:
        abort(404)
    return render_template("shopping_view.html", token=token)


@bp.route("/api/public/shopping-lists/<token>")
def public_shopping_lists(token):
    """Public API: return shopping list data. Token-secured."""
    if token != SHOPPING_TOKEN:
        abort(404)
    return jsonify({"lists": _get_shopping_lists_payload()})


@bp.route("/api/public/shopping-lists/toggle/<token>/<int:item_id>", methods=["POST"])
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


@bp.route("/api/public/shopping-lists/create/<token>", methods=["POST"])
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


@bp.route("/api/public/shopping-lists/add/<token>", methods=["POST"])
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
