"""Versioned schema migrations (SG-07).

Rules:
- Migrations are numbered and run in order, once each. The applied versions
  are recorded in the schema_version table.
- Never DROP TABLE or delete user data in a migration. Add columns and tables;
  move data with INSERT ... SELECT if a table must be reshaped.
- Each migration runs in its own transaction; a failure rolls it back and
  stops startup with the database unchanged.
- Before any pending migration runs on a database that already has data, a
  full copy is written to the backups directory.

To add a migration: write `def _m3_short_name(conn): ...` and append
`(3, "short description", _m3_short_name)` to MIGRATIONS.
"""
import glob
import os
import sqlite3
from datetime import datetime

from stashgrid.config import BACKUP_DIR, DB, logger
from stashgrid.db import connect
from stashgrid.extensions import password_hasher as _ph

BACKUPS_TO_KEEP = 10


# ── helpers ──────────────────────────────────────────────────────────────────

def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, column, ddl):
    """ALTER TABLE ... ADD COLUMN only if it's missing (no try/except: pass)."""
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _user_tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


# ── migrations ───────────────────────────────────────────────────────────────

def _m1_baseline(conn):
    """Schema as of SG-03/SG-06. Works on a brand-new database and on every
    older StashGrid database (tables are created only if missing, columns are
    added only if missing), so existing data is never touched."""
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
    _add_column(conn, "inventory", "photo_path", "TEXT")
    _add_column(conn, "inventory", "low_stock_threshold", "INTEGER DEFAULT 0")
    _add_column(conn, "inventory", "preferred_store", "TEXT DEFAULT 'Costco'")
    _add_column(conn, "inventory", "created_by", "INTEGER")

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user'
        )
    """)
    # must_change_password: user is held on the change-password page (SG-03).
    # session_version: bumped on password/role changes to end other sessions.
    _add_column(conn, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "users", "session_version", "INTEGER NOT NULL DEFAULT 0")

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

    # Seed system & admin users if the table is brand new
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
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


def _m2_repair_references(conn):
    """Foreign keys were never enforced before SG-07, so older databases may
    hold rows that point at a room, shelf, list or user that no longer exists.
    Clear those dangling pointers so enforcement can be switched on. Item rows
    themselves are kept; only references to things that are already gone are
    cleared. Orphaned list items and shelves (whose parent is gone) can't be
    reached from the UI and are removed."""
    fixes = {
        "inventory.room_id -> NULL": conn.execute(
            "UPDATE inventory SET room_id = NULL WHERE room_id IS NOT NULL "
            "AND room_id NOT IN (SELECT id FROM rooms)").rowcount,
        "shelves without a room removed": 0,
        "inventory.shelf_id -> NULL": 0,
    }
    # Shelves whose room is gone: detach items first, then remove the shelves.
    orphan_shelves = "SELECT id FROM shelves WHERE room_id NOT IN (SELECT id FROM rooms)"
    fixes["inventory.shelf_id -> NULL"] += conn.execute(
        f"UPDATE inventory SET shelf_id = NULL WHERE shelf_id IN ({orphan_shelves})").rowcount
    fixes["shelves without a room removed"] = conn.execute(
        f"DELETE FROM shelves WHERE id IN ({orphan_shelves})").rowcount
    fixes["inventory.shelf_id -> NULL"] += conn.execute(
        "UPDATE inventory SET shelf_id = NULL WHERE shelf_id IS NOT NULL "
        "AND shelf_id NOT IN (SELECT id FROM shelves)").rowcount
    fixes["shopping items without a list removed"] = conn.execute(
        "DELETE FROM shopping_list_items WHERE list_id IS NULL "
        "OR list_id NOT IN (SELECT id FROM shopping_lists)").rowcount
    fixes["audit_logs.user_id -> NULL"] = conn.execute(
        "UPDATE audit_logs SET user_id = NULL WHERE user_id IS NOT NULL "
        "AND user_id NOT IN (SELECT id FROM users)").rowcount
    for what, n in fixes.items():
        if n:
            logger.warning("DB repair: %s: %d row(s)", what, n)


MIGRATIONS = [
    (1, "baseline schema (rooms, shelves, inventory, shopping lists, users, audit log)", _m1_baseline),
    (2, "repair dangling references so foreign keys can be enforced", _m2_repair_references),
]


# ── runner ───────────────────────────────────────────────────────────────────

def _backup(conn, target_version):
    """Copy the live database (consistent snapshot via the SQLite backup API)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, f"inventory-{stamp}-pre-v{target_version}.db")
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    old = sorted(glob.glob(os.path.join(BACKUP_DIR, "inventory-*-pre-v*.db")))
    for stale in old[:-BACKUPS_TO_KEEP]:
        os.remove(stale)
    logger.info("DB: backup written to %s", path)
    return path


def current_version(conn):
    if "schema_version" not in _user_tables(conn):
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def run_migrations(path=None):
    """Apply pending migrations in order. Returns the list of versions applied."""
    conn = connect(path or DB)
    conn.isolation_level = None  # explicit BEGIN/COMMIT below
    try:
        # WAL lets the web app and scanner write without blocking readers.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        have = current_version(conn)
        pending = [m for m in MIGRATIONS if m[0] > have]
        if not pending:
            return []

        has_data = bool(_user_tables(conn) - {"schema_version"})
        if has_data:
            _backup(conn, pending[-1][0])

        applied = []
        for version, name, fn in pending:
            logger.info("DB: applying migration %d: %s", version, name)
            conn.execute("BEGIN IMMEDIATE")
            try:
                fn(conn)
                conn.execute("INSERT INTO schema_version (version, name) VALUES (?, ?)", (version, name))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                logger.exception("DB: migration %d failed; database left at version %d", version, have)
                raise
            applied.append(version)
            have = version

        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            logger.warning("DB: %d foreign key problem(s) remain: %s", len(problems), problems[:10])
        return applied
    finally:
        conn.close()
