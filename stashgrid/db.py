"""Database schema setup (moved from app.py, SG-06). Versioned migrations: SG-07."""
import sqlite3

from stashgrid.config import DB, logger
from stashgrid.extensions import password_hasher as _ph


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
        # Idempotent migration: add low_stock_threshold column
        try:
            conn.execute("ALTER TABLE inventory ADD COLUMN low_stock_threshold INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
        # Idempotent migration: add preferred_store column
        try:
            conn.execute("ALTER TABLE inventory ADD COLUMN preferred_store TEXT DEFAULT 'Costco'")
        except Exception:
            pass  # Column already exists
        # Shopping list tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shopping_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                store_name TEXT,
                is_default INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shopping_list_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER,
                barcode TEXT,
                name TEXT,
                requested_qty INTEGER DEFAULT 1,
                is_completed INTEGER DEFAULT 0,
                FOREIGN KEY(list_id) REFERENCES shopping_lists(id) ON DELETE CASCADE
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
        # ── Idempotent migration: add created_by to inventory ─────────────────
        try:
            conn.execute("ALTER TABLE inventory ADD COLUMN created_by INTEGER")
        except Exception:
            pass  # Column already exists
        # ── Idempotent migrations: password-change flag + session version ─────
        # must_change_password: user is sent to the change-password page until
        #   they set a new one (seeded admin, new users, admin resets).
        # session_version: bumped on password/role changes so existing session
        #   cookies for that user stop working.
        for ddl in (
            "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(ddl)
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
                "INSERT INTO users (username, password_hash, role, must_change_password) "
                "VALUES (?, ?, 'admin', 1)",
                ("admin", _ph.hash("admin"))
            )
            logger.info("Auth: seeded __system__ (id=1) and default admin user (must change password)")
