from flask import Flask, request, render_template, jsonify, redirect, session, url_for, abort
from functools import wraps
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
import sqlite3
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
import time
import threading
import subprocess
import shutil
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
# Secret key signs session cookies — override via env var in production
app.secret_key = os.environ.get("STASHGRID_SECRET_KEY", "stashgrid-dev-secret-change-in-prod-!")
DB = "inventory.db"



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

# Ensure cover storage directory exists alongside the static folder
UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)


def init_db():
    with sqlite3.connect(DB) as conn:
        # Drop old tables
        conn.execute("DROP TABLE IF EXISTS inventory")
        conn.execute("DROP TABLE IF EXISTS shopping_lists")
        conn.execute("DROP TABLE IF EXISTS shopping_list_items")
        conn.execute("DROP TABLE IF EXISTS rooms")
        conn.execute("DROP TABLE IF EXISTS shelves")

        try:
            conn.execute("ALTER TABLE books DROP COLUMN room_id")
            conn.execute("ALTER TABLE books DROP COLUMN shelf_id")
        except Exception:
            pass # Older SQLite or already dropped

        conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT,
                isbn TEXT,
                genre TEXT,
                publication_year INTEGER,
                pages INTEGER,
                series TEXT,
                read_status TEXT DEFAULT 'Unread',
                notes TEXT,
                cover_path TEXT,
                created_by INTEGER,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1,
                removed_at TEXT,
                removed_reason TEXT
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
        # ── Idempotent migration: add created_by to books ─────────────────
        try:
            conn.execute("ALTER TABLE books ADD COLUMN created_by INTEGER")
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
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                ("admin", _ph.hash("admin"))
            )
            logger.info("Auth: seeded __system__ (id=1) and default admin user")


# ── Auth helpers & decorators ────────────────────────────────────────────────

def log_audit(user_id, action, target_type, target_id):
    """Insert a row into audit_logs. Never raises — a logging failure must not
    crash a route or corrupt a transaction."""
    try:
        with sqlite3.connect(DB) as conn:
            conn.execute(
                "INSERT INTO audit_logs (user_id, action, target_type, target_id) "
                "VALUES (?, ?, ?, ?)",
                (user_id, action, target_type, target_id)
            )
    except Exception as e:
        logger.warning("log_audit failed: %s", e)


def login_required(f):
    """Decorator: redirect to /login if there is no active session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator: require an authenticated admin session.
    Returns HTTP 403 (not a redirect) so direct API abuse fails loudly."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            return jsonify({"error": "CLEARANCE DENIED // ADMIN REQUIRED"}), 403
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    """Secure Terminal Gateway — authenticate and establish a session."""
    if session.get("user_id"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with sqlite3.connect(DB) as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, role FROM users WHERE username = ?",
                (username,)
            ).fetchone()
        # System user has no password and cannot log in
        if row and row[2] and row[3] != "system":
            try:
                _ph.verify(row[2], password)
                # ✅ Credentials valid — establish session
                session["user_id"]  = row[0]
                session["username"] = row[1]
                session["role"]     = row[3]
                logger.info("Auth: user '%s' (role=%s) logged in", row[1], row[3])
                log_audit(row[0], "login", "user", row[0])
                return redirect(url_for("index"))
            except (VerifyMismatchError, InvalidHashError):
                pass  # Fall through to generic error
        # Generic message — don't reveal whether the username exists
        error = "AUTHENTICATION FAILED // INVALID CREDENTIALS"
        logger.warning("Auth: failed login attempt for username '%s'", username)

    return render_template("login.html", error=error)


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


def _fetch_all_books(q=""):
    with sqlite3.connect(DB) as conn:
        if q:
            like = f"%{q}%"
            rows = conn.execute("""
                SELECT id, isbn, title, author, genre, pages, series, read_status, notes, updated_at, cover_path
                FROM books
                WHERE is_active = 1
                  AND (isbn LIKE ? OR title LIKE ? OR author LIKE ? OR series LIKE ? OR notes LIKE ?)
                ORDER BY updated_at DESC
            """, (like, like, like, like, like)).fetchall()
            return rows
        else:
            rows = conn.execute("""
                SELECT id, isbn, title, author, genre, pages, series, read_status, notes, updated_at, cover_path
                FROM books
                WHERE is_active = 1
                ORDER BY updated_at DESC
            """).fetchall()
            return rows


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    books = _fetch_all_books(q=q)

    return render_template(
        "index.html",
        books=books,
        q=q
    )





def fetch_isbn_metadata(isbn):
    """Query OpenLibrary for book metadata and download the cover art."""
    if not isbn:
        return {"title": "UNKNOWN_ASSET_PENDING_MANUAL_ENTRY"}
        
    try:
        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
        resp = http_client.get(url, timeout=5, headers={"User-Agent": "ShelfGrid/1.0"})
        
        if resp.status_code == 200:
            data = resp.json()
            key = f"ISBN:{isbn}"
            if key in data:
                book_data = data[key]
                title = book_data.get("title", "UNKNOWN_ASSET_PENDING_MANUAL_ENTRY")
                authors = book_data.get("authors", [])
                author = authors[0].get("name") if authors else None
                pages = book_data.get("number_of_pages")
                
                cover_url = None
                cover_path = None
                if "cover" in book_data:
                    cover_url = book_data["cover"].get("large") or book_data["cover"].get("medium")
                
                if cover_url:
                    try:
                        img_resp = http_client.get(cover_url, timeout=5)
                        if img_resp.status_code == 200:
                            filename = f"{isbn}_{int(time.time())}.jpg"
                            save_path = os.path.join(UPLOADS_DIR, filename)
                            with open(save_path, "wb") as f:
                                f.write(img_resp.content)
                            cover_path = f"/static/uploads/{filename}"
                    except Exception as e:
                        logger.warning(f"Failed to download cover for {isbn}: {e}")

                return {
                    "title": title,
                    "author": author,
                    "pages": pages,
                    "cover_path": cover_path
                }
    except Exception as e:
        logger.error(f"OpenLibrary API error for ISBN {isbn}: {e}")
        
    return {"title": "UNKNOWN_ASSET_PENDING_MANUAL_ENTRY"}


@app.route("/api/books/add", methods=["POST"])
def api_books_add():
    """Receive an ISBN from the browser camera or hardware scanner.
    CRITICAL: This route is intentionally unprotected — the headless scanner.py
    script hits it directly without a browser session. Actions are attributed
    to SYSTEM_USER_ID (1) in the audit log."""
    isbn = request.form.get("barcode", "").strip().upper()

    if not isbn:
        return jsonify({"error": "barcode (isbn) is required"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB) as conn:
        existing = conn.execute("""
            SELECT id, title, cover_path FROM books
            WHERE isbn = ? AND is_active = 1
        """, (isbn,)).fetchone()

        if existing:
            # Books are generally unique, but if they scan the exact same ISBN,
            # we just acknowledge it.
            logger.info("Re-scanned existing ISBN %s", isbn)
            return jsonify({
                "id": existing[0],
                "title": existing[1] or isbn,
                "cover_path": existing[2],
                "is_new": False
            })
        else:
            metadata = fetch_isbn_metadata(isbn)
            title = metadata.get("title")
            author = metadata.get("author")
            pages = metadata.get("pages")
            cover_path = metadata.get("cover_path")

            cursor = conn.execute("""
                INSERT INTO books (isbn, title, author, pages, notes, updated_at, is_active, cover_path)
                VALUES (?, ?, ?, ?, '', ?, 1, ?)
            """, (isbn, title, author, pages, now, cover_path))
            new_id = cursor.lastrowid
            logger.info("New book scanned: %s (%s)", title, isbn)
            log_audit(SYSTEM_USER_ID, "scan_new", "book", new_id)
            return jsonify({
                "id": new_id,
                "title": title,
                "cover_path": cover_path,
                "is_new": True
            })


@app.route("/api/books/<int:book_id>/cover", methods=["POST"])
def upload_book_cover(book_id):
    """Accept a cover upload, resize it with Pillow, save to static/uploads/, update DB."""
    if "photo" not in request.files or not request.files["photo"].filename:
        return jsonify({"error": "No file provided"}), 400

    try:
        photo_file = request.files["photo"]
        img = Image.open(photo_file)
        img.thumbnail((400, 600), Image.LANCZOS)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        filename = f"manual_{book_id}_{int(time.time())}.jpg"
        save_path = os.path.join(UPLOADS_DIR, filename)
        img.save(save_path, "JPEG", quality=85)

        photo_url = f"/static/uploads/{filename}"
        with sqlite3.connect(DB) as conn:
            conn.execute("UPDATE books SET cover_path = ? WHERE id = ?", (photo_url, book_id))

        logger.info("Cover saved for book %d at %s", book_id, save_path)
        return jsonify({"photo_url": photo_url})

    except Exception as e:
        logger.error("Cover upload failed for book %d: %s", book_id, e)
        return jsonify({"error": str(e)}), 500


SCANNER_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_state.json")


@app.route("/api/scanner-state")
def scanner_state():
    """Return the live state written by scanner.py.
    Returns a default 'offline' payload if scanner.py isn't running yet."""
    try:
        with open(SCANNER_STATE_FILE, "r") as f:
            state = json.load(f)
        state["online"] = True
        return jsonify(state)
    except FileNotFoundError:
        return jsonify({"online": False, "mode": "IN"})
    except Exception as e:
        logger.warning("Failed to read scanner state file: %s", e)
        return jsonify({"online": False, "mode": "IN"})


@app.route("/api/library-hash")
def library_hash():
    q = request.args.get("q", "").strip()
    books = _fetch_all_books(q=q)
    state = {"books": books}
    digest = hashlib.md5(json.dumps(state, default=str).encode()).hexdigest()
    return jsonify({"hash": digest})


@app.route("/api/books")
def api_books():
    q = request.args.get("q", "").strip()
    books = _fetch_all_books(q=q)

    html = render_template("_book_cards.html", books=books, q=q)
    return jsonify({"html": html, "count": len(books)})


@app.route("/api/stats")
def api_stats():
    """Return library stats for the dashboard."""
    with sqlite3.connect(DB) as conn:
        total_books = conn.execute("SELECT count(*) FROM books WHERE is_active = 1").fetchone()[0]
        total_pages = conn.execute("SELECT sum(pages) FROM books WHERE is_active = 1 AND pages IS NOT NULL").fetchone()[0] or 0
        avg_pages = 0
        if total_books > 0:
            books_with_pages = conn.execute("SELECT count(*) FROM books WHERE is_active = 1 AND pages IS NOT NULL").fetchone()[0]
            if books_with_pages > 0:
                avg_pages = int(total_pages / books_with_pages)

        genres_query = conn.execute(
            "SELECT genre, count(*) as c FROM books WHERE is_active = 1 AND genre IS NOT NULL AND genre != '' GROUP BY genre ORDER BY c DESC LIMIT 5"
        ).fetchall()
        top_genres = [{"genre": g[0], "count": g[1]} for g in genres_query]

        authors_query = conn.execute(
            "SELECT author, count(*) as c FROM books WHERE is_active = 1 AND author IS NOT NULL AND author != '' GROUP BY author ORDER BY c DESC LIMIT 5"
        ).fetchall()
        top_authors = [{"author": a[0], "count": a[1]} for a in authors_query]
        
        # Simple placeholder for series data (just counts right now)
        series_query = conn.execute(
            "SELECT series, count(*) as c FROM books WHERE is_active = 1 AND series IS NOT NULL AND series != '' GROUP BY series ORDER BY c DESC LIMIT 5"
        ).fetchall()
        top_series = [{"series": s[0], "count": s[1]} for s in series_query]

    return jsonify({
        "total_books": total_books,
        "total_pages": total_pages,
        "avg_pages": avg_pages,
        "top_genres": top_genres,
        "top_authors": top_authors,
        "top_series": top_series
    })


@app.route("/edit/<int:book_id>", methods=["POST"])
def edit_book(book_id):
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    isbn = request.form.get("isbn", "").strip()
    genre = request.form.get("genre", "").strip()
    series = request.form.get("series", "").strip()
    read_status = request.form.get("read_status", "Unread").strip()
    try:
        pages = int(request.form.get("pages", 0) or 0)
    except ValueError:
        pages = 0

    notes = request.form.get("notes", "").strip()

    with sqlite3.connect(DB) as conn:
        conn.execute("""
            UPDATE books
            SET title = ?, author = ?, isbn = ?, genre = ?, series = ?, pages = ?, read_status = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (title, author, isbn, genre, series, pages, read_status, notes, book_id))

    log_audit(session.get("user_id"), "edit_book", "book", book_id)
    return redirect("/")


@app.route("/delete/<int:book_id>", methods=["POST"])
def delete_book(book_id):
    reason = request.form.get("reason", "Purged via terminal")

    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE books SET is_active = 0, removed_at = CURRENT_TIMESTAMP, removed_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (reason, book_id))

    log_audit(session.get("user_id"), "delete_book", "book", book_id)
    return redirect("/")


# ── Shopping List Routes Removed ──────────────────────────────

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

init_db()

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