"""Rooms, shelves, items, scanning and photo routes (moved from app.py, SG-06).

SG-32: every query is scoped to the current household. IDs that belong to
another household (or don't exist) get a 404.
"""
import hashlib
import json
import os
from datetime import datetime

from flask import abort, request, render_template, jsonify, redirect, session, g
from PIL import Image

from stashgrid.db import get_db
from stashgrid.config import PHOTOS_DIR, SCANNER_STATE_FILE, SYSTEM_USER_ID, logger
from stashgrid.core.audit import log_audit
from stashgrid.core.security import admin_required
from stashgrid.core.tenancy import (
    current_household_id, require_item, require_room, require_shelf,
)
from stashgrid.inventory import bp
from stashgrid.services.lookup import _web_lookup_name
from stashgrid.inventory.shopping import _auto_add_to_shopping_list


def _get_rooms_list():
    with get_db() as conn:
        return conn.execute(
            "SELECT id, name, is_default FROM rooms WHERE household_id = ? ORDER BY name ASC",
            (current_household_id(),),
        ).fetchall()


def _get_room_layout(room_id):
    if not room_id:
        return None, []
    with get_db() as conn:
        room = conn.execute(
            "SELECT id, name, is_default FROM rooms WHERE id = ? AND household_id = ?",
            (room_id, current_household_id()),
        ).fetchone()
        if not room:
            return None, []
        shelves = conn.execute(
            "SELECT id, shelf_number FROM shelves WHERE room_id = ? AND household_id = ? "
            "ORDER BY shelf_number DESC",
            (room_id, current_household_id()),
        ).fetchall()
        return room, shelves


def _fetch_items_by_layout(room_id=None, q=""):
    hid = current_household_id()
    with get_db() as conn:
        if q:
            like = f"%{q.lower()}%"
            rows = conn.execute("""
                SELECT i.id, i.barcode, i.name, r.name, s.shelf_number, i.quantity, i.notes, i.updated_at, i.room_id, i.shelf_id, i.photo_path, i.low_stock_threshold, i.preferred_store
                FROM inventory i
                LEFT JOIN rooms r ON i.room_id = r.id
                LEFT JOIN shelves s ON i.shelf_id = s.id
                WHERE i.household_id = ? AND i.is_active = 1
                  AND (LOWER(i.barcode) LIKE ? OR LOWER(i.name) LIKE ? OR LOWER(i.notes) LIKE ? OR LOWER(r.name) LIKE ?)
                ORDER BY i.updated_at DESC
            """, (hid, like, like, like, like)).fetchall()
            return rows
        elif room_id:
            rows = conn.execute("""
                SELECT i.id, i.barcode, i.name, r.name, s.shelf_number, i.quantity, i.notes, i.updated_at, i.room_id, i.shelf_id, i.photo_path, i.low_stock_threshold, i.preferred_store
                FROM inventory i
                LEFT JOIN rooms r ON i.room_id = r.id
                LEFT JOIN shelves s ON i.shelf_id = s.id
                WHERE i.household_id = ? AND i.is_active = 1 AND i.room_id = ?
                ORDER BY s.shelf_number DESC, i.updated_at DESC
            """, (hid, room_id)).fetchall()
            return rows
        return []


def _resolve_room_id(raw_id, rooms):
    """Safely extracts fallback integers to avoid API data parameter crashes."""
    if not raw_id or str(raw_id).strip() in ("", "None", "undefined"):
        with get_db() as conn:
            default_room = conn.execute(
                "SELECT id FROM rooms WHERE household_id = ? AND is_default = 1 LIMIT 1",
                (current_household_id(),),
            ).fetchone()
            if default_room:
                return int(default_room[0])
            elif rooms:
                return int(rooms[0][0])
            return None
    try:
        return int(raw_id)
    except ValueError:
        return None


def _store_names():
    with get_db() as conn:
        return [
            row[0] for row in conn.execute(
                "SELECT DISTINCT store_name FROM shopping_lists WHERE household_id = ? "
                "AND store_name IS NOT NULL AND store_name != '' ORDER BY store_name ASC",
                (current_household_id(),),
            ).fetchall()
        ]


@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    rooms = _get_rooms_list()
    selected_room_id = _resolve_room_id(request.args.get("room_id"), rooms)

    room_info, shelves = _get_room_layout(selected_room_id)
    items = _fetch_items_by_layout(room_id=selected_room_id, q=q)

    return render_template(
        "index.html",
        rooms=rooms,
        room_info=room_info,
        shelves=shelves,
        items=items,
        selected_room_id=selected_room_id,
        q=q,
        store_names=_store_names()
    )


@bp.route("/api/rooms", methods=["POST"])
def create_room():
    name = request.form.get("name", "").strip()
    shelf_count = int(request.form.get("shelf_count", 3))
    if not name:
        return redirect("/")
    hid = current_household_id()

    with get_db() as conn:
        existing = conn.execute("SELECT count(*) FROM rooms WHERE household_id = ?", (hid,)).fetchone()[0]
        is_default = 1 if existing == 0 else 0

        cursor = conn.execute(
            "INSERT INTO rooms (name, is_default, household_id) VALUES (?, ?, ?)", (name, is_default, hid)
        )
        room_id = cursor.lastrowid

        for i in range(1, shelf_count + 1):
            conn.execute(
                "INSERT INTO shelves (room_id, shelf_number, household_id) VALUES (?, ?, ?)", (room_id, i, hid)
            )

    log_audit(session.get("user_id"), "create_room", "room", room_id)
    return redirect(f"/?room_id={room_id}")


@bp.route("/api/rooms/<int:room_id>/set-default", methods=["POST"])
def set_default_room(room_id):
    hid = current_household_id()
    with get_db() as conn:
        require_room(conn, room_id)
        conn.execute("UPDATE rooms SET is_default = 0 WHERE household_id = ?", (hid,))
        conn.execute("UPDATE rooms SET is_default = 1 WHERE id = ? AND household_id = ?", (room_id, hid))
    return redirect(f"/?room_id={room_id}")


@bp.route("/api/rooms/<int:room_id>/add-shelves", methods=["POST"])
def add_shelves_to_room(room_id):
    """Append additional shelves to an existing room."""
    add_count = int(request.form.get("add_count", 1))
    hid = current_household_id()
    with get_db() as conn:
        require_room(conn, room_id)
        # Find the highest existing shelf number so we continue from there
        max_shelf = conn.execute(
            "SELECT COALESCE(MAX(shelf_number), 0) FROM shelves WHERE room_id = ? AND household_id = ?",
            (room_id, hid),
        ).fetchone()[0]
        for i in range(1, add_count + 1):
            conn.execute(
                "INSERT INTO shelves (room_id, shelf_number, household_id) VALUES (?, ?, ?)",
                (room_id, max_shelf + i, hid),
            )
    logger.info("Added %d shelf(ves) to room %d", add_count, room_id)
    return redirect(f"/?room_id={room_id}")


@bp.route("/api/rooms/<int:room_id>/delete", methods=["POST"])
@admin_required
def delete_room(room_id):
    """Delete a room, its shelves, and soft-delete all its inventory items."""
    hid = current_household_id()
    with get_db() as conn:
        require_room(conn, room_id)
        # Soft-delete all inventory belonging to this room. With foreign keys
        # enforced (SG-07) the rows must also let go of the room and shelves
        # being deleted; they stay in history as removed items without a location.
        conn.execute(
            "UPDATE inventory SET is_active = 0, quantity = 0, removed_at = CURRENT_TIMESTAMP, "
            "removed_reason = 'Room deleted via HUD', room_id = NULL, shelf_id = NULL "
            "WHERE room_id = ? AND household_id = ?",
            (room_id, hid)
        )
        # Items filed in another room but pointing at one of this room's shelves
        # (inconsistent legacy data): keep them, just clear the shelf.
        conn.execute(
            "UPDATE inventory SET shelf_id = NULL WHERE household_id = ? "
            "AND shelf_id IN (SELECT id FROM shelves WHERE room_id = ? AND household_id = ?)",
            (hid, room_id, hid)
        )
        # Hard-delete shelves and room (FK cascade handles shelves if pragma is on, but explicit is safer)
        conn.execute("DELETE FROM shelves WHERE room_id = ? AND household_id = ?", (room_id, hid))
        conn.execute("DELETE FROM rooms WHERE id = ? AND household_id = ?", (room_id, hid))
    log_audit(session.get("user_id"), "delete_room", "room", room_id)
    logger.info("Deleted room %d", room_id)
    return redirect("/")


@bp.route("/api/scan", methods=["POST"])
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
    hid = current_household_id()

    with get_db() as conn:
        # The room must be ours and the shelf must be in that room.
        require_room(conn, room_id)
        require_shelf(conn, shelf_id, room_id=room_id)
        existing = conn.execute("""
            SELECT id, quantity, name, photo_path FROM inventory
            WHERE household_id = ? AND barcode = ? AND room_id = ? AND shelf_id = ? AND is_active = 1
        """, (hid, barcode, room_id, shelf_id)).fetchone()

        if existing:
            conn.execute(
                "UPDATE inventory SET quantity = quantity + 1, updated_at = ? WHERE id = ? AND household_id = ?",
                (now, existing[0], hid)
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
                INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity, notes, updated_at, is_active, household_id)
                VALUES (?, ?, ?, ?, 1, '', ?, 1, ?)
            """, (barcode, name, room_id, shelf_id, now, hid))
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


@bp.route("/api/items/<int:item_id>/photo", methods=["POST"])
def upload_item_photo(item_id):
    """Accept a photo upload, resize it with Pillow, save to static/photos/, update DB."""
    if "photo" not in request.files or not request.files["photo"].filename:
        return jsonify({"error": "No photo file provided"}), 400

    # Check ownership before touching the file on disk ({item_id}.jpg).
    with get_db() as conn:
        require_item(conn, item_id)

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
        with get_db() as conn:
            conn.execute(
                "UPDATE inventory SET photo_path = ? WHERE id = ? AND household_id = ?",
                (photo_url, item_id, current_household_id()),
            )

        logger.info("Photo saved for item %d at %s", item_id, save_path)
        return jsonify({"photo_url": photo_url})

    except Exception as e:
        logger.error("Photo upload failed for item %d: %s", item_id, e)
        return jsonify({"error": str(e)}), 500


_SCANNER_OFFLINE = {"online": False, "mode": "IN", "shelf_number": None, "shelf_id": None, "room_id": None}


@bp.route("/api/scanner-state")
def scanner_state():
    """Return the live state written by scanner.py (mode, shelf, room).
    Returns a default 'offline' payload if scanner.py isn't running yet, or if
    the scanner is working in another household's rooms (SG-32)."""
    try:
        with open(SCANNER_STATE_FILE, "r") as f:
            state = json.load(f)
    except FileNotFoundError:
        return jsonify(_SCANNER_OFFLINE)
    except Exception as e:
        logger.warning("Failed to read scanner state file: %s", e)
        return jsonify(_SCANNER_OFFLINE)
    if state.get("room_id"):
        with get_db() as conn:
            ours = conn.execute(
                "SELECT 1 FROM rooms WHERE id = ? AND household_id = ?",
                (state["room_id"], current_household_id()),
            ).fetchone()
        if not ours:
            return jsonify(_SCANNER_OFFLINE)
    state["online"] = True
    return jsonify(state)


@bp.route("/api/inventory-hash")
def inventory_hash():
    q = request.args.get("q", "").strip()
    rooms = _get_rooms_list()
    selected_room_id = _resolve_room_id(request.args.get("room_id"), rooms)
    hid = current_household_id()

    items = _fetch_items_by_layout(room_id=selected_room_id, q=q)
    with get_db() as conn:
        sl_lists = conn.execute(
            "SELECT id, name, store_name, is_default FROM shopping_lists WHERE household_id = ? ORDER BY id",
            (hid,),
        ).fetchall()
        sl_items = conn.execute(
            "SELECT id, list_id, barcode, is_completed FROM shopping_list_items WHERE household_id = ? ORDER BY id",
            (hid,),
        ).fetchall()
    state = {"items": items, "rooms": rooms, "sl_lists": sl_lists, "sl_items": sl_items}
    digest = hashlib.md5(json.dumps(state, default=str).encode()).hexdigest()
    return jsonify({"hash": digest})


@bp.route("/api/items")
def api_items():
    q = request.args.get("q", "").strip()
    rooms = _get_rooms_list()
    selected_room_id = _resolve_room_id(request.args.get("room_id"), rooms)

    room_info, shelves = _get_room_layout(selected_room_id)
    items = _fetch_items_by_layout(room_id=selected_room_id, q=q)

    html = render_template("_item_cards.html", room_info=room_info, items=items, shelves=shelves,
                           q=q, store_names=_store_names())
    return jsonify({"html": html, "count": len(items)})


@bp.route("/edit/<int:item_id>", methods=["POST"])
def edit_item(item_id):
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    room_id = request.form.get("room_id")
    threshold = int(request.form.get("low_stock_threshold", 0) or 0)
    preferred_store = request.form.get("preferred_store", "Costco").strip() or "Costco"
    new_shelf_id_raw = request.form.get("new_shelf_id", "").strip()
    hid = current_household_id()

    with get_db() as conn:
        require_item(conn, item_id)
        if new_shelf_id_raw.isdigit():
            # Relocate asset: the target shelf must be ours; the item follows
            # the shelf into its room.
            shelf = require_shelf(conn, int(new_shelf_id_raw))
            conn.execute("""
                UPDATE inventory
                SET name = ?, notes = ?, low_stock_threshold = ?, preferred_store = ?,
                    shelf_id = ?, room_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND household_id = ?
            """, (name, notes, threshold, preferred_store, shelf[0], shelf[1], item_id, hid))
            logger.info("Relocated item %d to shelf %s via Web HUD", item_id, new_shelf_id_raw)
        else:
            # No relocation requested — leave shelf_id untouched
            conn.execute("""
                UPDATE inventory
                SET name = ?, notes = ?, low_stock_threshold = ?, preferred_store = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND household_id = ?
            """, (name, notes, threshold, preferred_store, item_id, hid))

    log_audit(session.get("user_id"), "edit_item", "item", item_id)
    return redirect(f"/?room_id={room_id}" if room_id else "/")


@bp.route("/remove/<int:item_id>", methods=["POST"])
def remove_item_quantity(item_id):
    amount = int(request.form.get("amount", 1))
    reason = request.form.get("reason", "Removed via HUD")
    room_id = request.form.get("room_id")
    hid = current_household_id()

    with get_db() as conn:
        item = conn.execute(
            "SELECT quantity, barcode, name, low_stock_threshold, preferred_store FROM inventory "
            "WHERE id = ? AND household_id = ? AND is_active = 1",
            (item_id, hid)
        ).fetchone()
        if not item:
            abort(404)

        current_quantity, barcode, item_name, threshold, preferred_store = item
        if current_quantity > amount:
            conn.execute(
                "UPDATE inventory SET quantity = quantity - ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND household_id = ?",
                (amount, item_id, hid)
            )
            new_qty = current_quantity - amount
        else:
            conn.execute(
                "UPDATE inventory SET quantity = 0, is_active = 0, removed_at = CURRENT_TIMESTAMP, "
                "removed_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND household_id = ?",
                (reason, item_id, hid)
            )
            new_qty = 0

        # Auto-add to the item's preferred store list if stock drops below threshold
        if threshold and threshold > 0 and new_qty < threshold:
            _auto_add_to_shopping_list(
                conn, hid, barcode, item_name or "[ Unknown Item ]",
                preferred_store=preferred_store or "Costco"
            )

    log_audit(session.get("user_id"), "remove_qty", "item", item_id)
    return redirect(f"/?room_id={room_id}" if room_id else "/")


@bp.route("/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    reason = request.form.get("reason", "Purged via terminal")
    room_id = request.form.get("room_id")
    hid = current_household_id()

    with get_db() as conn:
        require_item(conn, item_id)
        conn.execute(
            "UPDATE inventory SET quantity = 0, is_active = 0, removed_at = CURRENT_TIMESTAMP, "
            "removed_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND household_id = ?",
            (reason, item_id, hid),
        )

    log_audit(session.get("user_id"), "delete_item", "item", item_id)
    return redirect(f"/?room_id={room_id}" if room_id else "/")
