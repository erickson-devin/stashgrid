from flask import Flask, request, render_template, jsonify, redirect
import sqlite3
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from datetime import datetime
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
DB = "inventory.db"

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
                SELECT i.id, i.barcode, i.name, r.name, s.shelf_number, i.quantity, i.notes, i.updated_at, i.room_id, i.shelf_id, i.photo_path
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
                SELECT i.id, i.barcode, i.name, r.name, s.shelf_number, i.quantity, i.notes, i.updated_at, i.room_id, i.shelf_id, i.photo_path
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

    return render_template(
        "index.html",
        rooms=rooms,
        room_info=room_info,
        shelves=shelves,
        items=items,
        selected_room_id=selected_room_id,
        q=q
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


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Receive a barcode from the browser camera and log it to the selected shelf."""
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
            logger.info("New item scanned: %s (%s) on shelf %d", name, barcode, shelf_id)
            return jsonify({
                "id": cursor.lastrowid,
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


@app.route("/api/inventory-hash")
def inventory_hash():
    q = request.args.get("q", "").strip()
    rooms = _get_rooms_list()
    selected_room_id = _resolve_room_id(request.args.get("room_id"), rooms)

    items = _fetch_items_by_layout(room_id=selected_room_id, q=q)
    state = {"items": items, "rooms": rooms}
    digest = hashlib.md5(json.dumps(state, default=str).encode()).hexdigest()
    return jsonify({"hash": digest})


@app.route("/api/items")
def api_items():
    q = request.args.get("q", "").strip()
    rooms = _get_rooms_list()
    selected_room_id = _resolve_room_id(request.args.get("room_id"), rooms)

    room_info, shelves = _get_room_layout(selected_room_id)
    items = _fetch_items_by_layout(room_id=selected_room_id, q=q)

    html = render_template("_item_cards.html", room_info=room_info, items=items, shelves=shelves, q=q)
    return jsonify({"html": html, "count": len(items)})


@app.route("/edit/<int:item_id>", methods=["POST"])
def edit_item(item_id):
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    room_id = request.form.get("room_id")

    with sqlite3.connect(DB) as conn:
        conn.execute("""
            UPDATE inventory
            SET name = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (name, notes, item_id))

    return redirect(f"/?room_id={room_id}" if room_id else "/")


@app.route("/remove/<int:item_id>", methods=["POST"])
def remove_item_quantity(item_id):
    amount = int(request.form.get("amount", 1))
    reason = request.form.get("reason", "Removed via HUD")
    room_id = request.form.get("room_id")

    with sqlite3.connect(DB) as conn:
        item = conn.execute("SELECT quantity FROM inventory WHERE id = ? AND is_active = 1", (item_id,)).fetchone()
        if not item:
            return redirect("/")

        current_quantity = item[0]
        if current_quantity > amount:
            conn.execute("UPDATE inventory SET quantity = quantity - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (amount, item_id))
        else:
            conn.execute("UPDATE inventory SET quantity = 0, is_active = 0, removed_at = CURRENT_TIMESTAMP, removed_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (reason, item_id))

    return redirect(f"/?room_id={room_id}" if room_id else "/")


@app.route("/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    reason = request.form.get("reason", "Purged via terminal")
    room_id = request.form.get("room_id")

    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE inventory SET quantity = 0, is_active = 0, removed_at = CURRENT_TIMESTAMP, removed_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (reason, item_id))

    return redirect(f"/?room_id={room_id}" if room_id else "/")


init_db()

if __name__ == "__main__":
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