"""Versioned schema migrations (SG-07; Postgres support SG-34).

Rules:
- Migrations are numbered and run in order, once each. The applied versions
  are recorded in the schema_version table.
- Never DROP TABLE or delete user data in a migration. Add columns and tables;
  move data with INSERT ... SELECT if a table must be reshaped.
- Each migration runs in its own transaction; a failure rolls it back and
  stops startup with the database unchanged.
- Before any pending migration runs on a database that already has data, a
  full copy is written to the backups directory.

To add a migration: write `def _m6_short_name(conn): ...` (SQLite) and append
`(6, "short description", _m6_short_name)` to MIGRATIONS, then write the
Postgres equivalent and add it to POSTGRES_MIGRATIONS (see below).

SQLite migrations get a raw sqlite3 connection. A Postgres database is created
from a single baseline schema instead of replaying versions 1-5, and has no
file backups (use the provider's backups / pg_dump).
"""
import glob
import os
import sqlite3
from datetime import datetime

from stashgrid.config import BACKUP_DIR, DB, logger
from stashgrid import db as _db
from stashgrid.db import sqlite_connect as connect
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


DEFAULT_HOUSEHOLD_ID = 1
TENANT_TABLES = ("rooms", "shelves", "inventory", "shopping_lists", "shopping_list_items", "audit_logs")


def _m3_households(conn):
    """Household accounts (SG-32). Every room, shelf, item, shopping list and
    list item belongs to exactly one household. Existing data moves into a
    default household (id 1) and existing users become its members.

    SQLite can't add a NOT NULL + REFERENCES column without rebuilding the
    table, so household_id is added as a nullable foreign key and the rules
    are enforced with triggers instead:
      - rooms and shopping_lists (root rows) must be inserted with a household;
      - shelves, inventory and shopping_list_items inherit the household of
        their room / shelf / list when inserted without one, and can never
        point at a room, shelf or list from a different household.
    The app also scopes every query (stashgrid/core/tenancy.py); the triggers
    are a second line of defence and keep scanner.py's direct writes correct.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS households (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT NOT NULL,
            created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            -- reserved for billing (SG-36)
            plan               TEXT,
            plan_status        TEXT,
            trial_ends_at      TEXT,
            current_period_end TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS household_members (
            household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role         TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
            created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (household_id, user_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_household_members_user ON household_members(user_id)")
    # Operator of the whole service (hosted mode). Not a household role.
    _add_column(conn, "users", "is_superuser", "INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        "INSERT OR IGNORE INTO households (id, name) VALUES (?, 'Home')", (DEFAULT_HOUSEHOLD_ID,)
    )

    for table in TENANT_TABLES:
        _add_column(conn, table, "household_id", "INTEGER REFERENCES households(id)")
        conn.execute(f"UPDATE {table} SET household_id = ? WHERE household_id IS NULL", (DEFAULT_HOUSEHOLD_ID,))
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_household ON {table}(household_id)")

    # Existing users join the default household. The first admin owns it.
    users = conn.execute(
        "SELECT id, role FROM users WHERE role != 'system' ORDER BY id"
    ).fetchall()
    owner_id = next((uid for uid, role in users if role == "admin"), None)
    for uid, role in users:
        member_role = "owner" if uid == owner_id else ("admin" if role == "admin" else "member")
        conn.execute(
            "INSERT OR IGNORE INTO household_members (household_id, user_id, role) VALUES (?, ?, ?)",
            (DEFAULT_HOUSEHOLD_ID, uid, member_role),
        )
    # Self-hosted installs: existing admins operate their own server.
    conn.execute("UPDATE users SET is_superuser = 1 WHERE role = 'admin'")

    # ── triggers ────────────────────────────────────────────────────────────
    for root in ("rooms", "shopping_lists"):
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_{root}_household_required
            BEFORE INSERT ON {root} WHEN NEW.household_id IS NULL
            BEGIN SELECT RAISE(ABORT, 'household_id required on {root}'); END
        """)

    # child table, parent table, child FK column
    children = [
        ("shelves", "rooms", "room_id"),
        ("shopping_list_items", "shopping_lists", "list_id"),
    ]
    for child, parent, col in children:
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_{child}_household_ins
            AFTER INSERT ON {child}
            BEGIN
                UPDATE {child} SET household_id = (SELECT household_id FROM {parent} WHERE id = NEW.{col})
                 WHERE id = NEW.id AND household_id IS NULL;
                SELECT RAISE(ABORT, 'household mismatch on {child}')
                 WHERE (SELECT household_id FROM {child} WHERE id = NEW.id) IS NOT
                       (SELECT household_id FROM {parent} WHERE id = NEW.{col});
            END
        """)
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_{child}_household_upd
            AFTER UPDATE OF household_id, {col} ON {child}
            BEGIN
                SELECT RAISE(ABORT, 'household mismatch on {child}')
                 WHERE NEW.household_id IS NOT (SELECT household_id FROM {parent} WHERE id = NEW.{col});
            END
        """)

    # inventory: household comes from its room, else its shelf. Removed items
    # may have neither (room deleted) and keep the household they had.
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_inventory_household_ins
        AFTER INSERT ON inventory
        BEGIN
            UPDATE inventory SET household_id = COALESCE(
                    (SELECT household_id FROM rooms WHERE id = NEW.room_id),
                    (SELECT household_id FROM shelves WHERE id = NEW.shelf_id))
             WHERE id = NEW.id AND household_id IS NULL;
            SELECT RAISE(ABORT, 'household_id required on inventory')
             WHERE (SELECT household_id FROM inventory WHERE id = NEW.id) IS NULL;
            SELECT RAISE(ABORT, 'household mismatch on inventory')
             WHERE (NEW.room_id IS NOT NULL AND (SELECT household_id FROM inventory WHERE id = NEW.id)
                        IS NOT (SELECT household_id FROM rooms WHERE id = NEW.room_id))
                OR (NEW.shelf_id IS NOT NULL AND (SELECT household_id FROM inventory WHERE id = NEW.id)
                        IS NOT (SELECT household_id FROM shelves WHERE id = NEW.shelf_id));
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_inventory_household_upd
        AFTER UPDATE OF household_id, room_id, shelf_id ON inventory
        BEGIN
            SELECT RAISE(ABORT, 'household mismatch on inventory')
             WHERE NEW.household_id IS NULL
                OR (NEW.room_id IS NOT NULL AND NEW.household_id IS NOT (SELECT household_id FROM rooms WHERE id = NEW.room_id))
                OR (NEW.shelf_id IS NOT NULL AND NEW.household_id IS NOT (SELECT household_id FROM shelves WHERE id = NEW.shelf_id));
        END
    """)


def _m4_email_accounts(conn):
    """Email accounts (SG-33): email login, verification, password reset and
    household invites.

    users.email holds a verified address, except for accounts created by
    public sign-up, which can't log in until it's verified. Emails are stored
    lowercased; the partial unique index allows many users without one.
    auth_tokens stores only a SHA-256 hash of each emailed token.
    """
    _add_column(conn, "users", "email", "TEXT")
    _add_column(conn, "users", "email_verified_at", "TEXT")
    _add_column(conn, "users", "display_name", "TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose      TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password', 'invite')),
            token_hash   TEXT NOT NULL UNIQUE,
            user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
            email        TEXT,
            household_id INTEGER REFERENCES households(id) ON DELETE CASCADE,
            role         TEXT,
            created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at   TEXT NOT NULL,
            used_at      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_lookup ON auth_tokens(purpose, email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_household ON auth_tokens(household_id)")


def _m5_links_and_keys(conn):
    """Per-household share links and scanner API keys (SG-35).

    Both tables store only a SHA-256 hash of the secret. The pre-SG-35 global
    shopping token (shopping_token.txt) and scanner key (STASHGRID_SCANNER_KEY)
    are imported once into the default household so existing links and
    scanners keep working; after that they're managed on the settings page.
    """
    import hashlib
    from stashgrid import config

    conn.execute("""
        CREATE TABLE IF NOT EXISTS share_links (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            list_id      INTEGER REFERENCES shopping_lists(id) ON DELETE CASCADE,  -- NULL = all lists
            label        TEXT NOT NULL,
            token_hash   TEXT NOT NULL UNIQUE,
            created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at   TEXT,
            last_used_at TEXT,
            revoked_at   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            label        TEXT NOT NULL,
            key_prefix   TEXT NOT NULL,
            key_hash     TEXT NOT NULL UNIQUE,
            created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT,
            revoked_at   TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_share_links_household ON share_links(household_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_household ON api_keys(household_id)")

    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
    legacy_token = config.LEGACY_SHOPPING_TOKEN
    if legacy_token:
        conn.execute(
            "INSERT OR IGNORE INTO share_links (household_id, label, token_hash) VALUES (?, ?, ?)",
            (DEFAULT_HOUSEHOLD_ID, "Original shopping link", sha(legacy_token)),
        )
        logger.info("DB: imported the existing shopping link into household %d", DEFAULT_HOUSEHOLD_ID)
    if config.LEGACY_SCANNER_KEY:
        conn.execute(
            "INSERT OR IGNORE INTO api_keys (household_id, label, key_prefix, key_hash) VALUES (?, ?, ?, ?)",
            (DEFAULT_HOUSEHOLD_ID, "Scanner (from STASHGRID_SCANNER_KEY)",
             config.LEGACY_SCANNER_KEY[:6], sha(config.LEGACY_SCANNER_KEY)),
        )
        logger.info("DB: imported STASHGRID_SCANNER_KEY into household %d", DEFAULT_HOUSEHOLD_ID)


MIGRATIONS = [
    (1, "baseline schema (rooms, shelves, inventory, shopping lists, users, audit log)", _m1_baseline),
    (2, "repair dangling references so foreign keys can be enforced", _m2_repair_references),
    (3, "household accounts: households, members, household_id on every tenant table", _m3_households),
    (4, "email accounts: users.email/display_name, auth_tokens for verify/reset/invite", _m4_email_accounts),
    (5, "per-household share links and scanner API keys", _m5_links_and_keys),
]


# ── Postgres (SG-34) ─────────────────────────────────────────────────────────
# A Postgres database starts from one baseline that equals SQLite schema
# version PG_BASELINE_VERSION; versions 1..baseline are recorded as applied.
# Every migration added after the baseline needs a Postgres version too:
# write `def _pg_m6_short_name(conn): ...` and add it to POSTGRES_MIGRATIONS.
#
# Same tables, columns and text timestamps as SQLite, so the app's SQL and the
# SQLite import tool work unchanged. Household isolation uses composite
# foreign keys ((room_id, household_id) -> rooms(id, household_id)) instead
# of SQLite's triggers; a BEFORE INSERT trigger still fills household_id from
# the parent row when it's left out, like the SQLite triggers do.

PG_BASELINE_VERSION = 5
POSTGRES_MIGRATIONS = {}          # version -> fn(conn), for versions > PG_BASELINE_VERSION
_PG_LOCK_ID = 7_445_301           # advisory lock: one migrator at a time

_PG_NOW = "to_char(timezone('UTC', now()), 'YYYY-MM-DD HH24:MI:SS')"
_PG_ID = "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"

_PG_BASELINE = f"""
CREATE TABLE households (
    id                 {_PG_ID},
    name               TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT {_PG_NOW},
    plan               TEXT,
    plan_status        TEXT,
    trial_ends_at      TEXT,
    current_period_end TEXT
);
CREATE TABLE users (
    id                   {_PG_ID},
    username             TEXT UNIQUE NOT NULL,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'user',
    must_change_password INTEGER NOT NULL DEFAULT 0,
    session_version      INTEGER NOT NULL DEFAULT 0,
    is_superuser         INTEGER NOT NULL DEFAULT 0,
    email                TEXT,
    email_verified_at    TEXT,
    display_name         TEXT
);
CREATE UNIQUE INDEX idx_users_email ON users(email) WHERE email IS NOT NULL;
CREATE TABLE household_members (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    created_at   TEXT NOT NULL DEFAULT {_PG_NOW},
    PRIMARY KEY (household_id, user_id)
);
CREATE INDEX idx_household_members_user ON household_members(user_id);

CREATE TABLE rooms (
    id           {_PG_ID},
    name         TEXT NOT NULL,
    is_default   INTEGER DEFAULT 0,
    household_id INTEGER NOT NULL REFERENCES households(id),
    UNIQUE (id, household_id)
);
CREATE TABLE shelves (
    id           {_PG_ID},
    room_id      INTEGER NOT NULL,
    shelf_number INTEGER NOT NULL,
    household_id INTEGER NOT NULL REFERENCES households(id),
    UNIQUE (id, household_id),
    FOREIGN KEY (room_id, household_id) REFERENCES rooms(id, household_id) ON DELETE CASCADE
);
CREATE TABLE inventory (
    id                  {_PG_ID},
    barcode             TEXT NOT NULL,
    name                TEXT,
    room_id             INTEGER,
    shelf_id            INTEGER,
    quantity            INTEGER DEFAULT 1,
    notes               TEXT,
    updated_at          TEXT DEFAULT {_PG_NOW},
    is_active           INTEGER DEFAULT 1,
    removed_at          TEXT,
    removed_reason      TEXT,
    photo_path          TEXT,
    low_stock_threshold INTEGER DEFAULT 0,
    preferred_store     TEXT DEFAULT 'Costco',
    created_by          INTEGER,
    household_id        INTEGER NOT NULL REFERENCES households(id),
    FOREIGN KEY (room_id, household_id) REFERENCES rooms(id, household_id),
    FOREIGN KEY (shelf_id, household_id) REFERENCES shelves(id, household_id)
);
CREATE TABLE shopping_lists (
    id           {_PG_ID},
    name         TEXT NOT NULL,
    store_name   TEXT,
    is_default   INTEGER DEFAULT 0,
    household_id INTEGER NOT NULL REFERENCES households(id),
    UNIQUE (id, household_id)
);
CREATE TABLE shopping_list_items (
    id            {_PG_ID},
    list_id       INTEGER,
    barcode       TEXT,
    name          TEXT,
    requested_qty INTEGER DEFAULT 1,
    is_completed  INTEGER DEFAULT 0,
    household_id  INTEGER NOT NULL REFERENCES households(id),
    FOREIGN KEY (list_id, household_id) REFERENCES shopping_lists(id, household_id) ON DELETE CASCADE
);
CREATE TABLE audit_logs (
    id           {_PG_ID},
    user_id      INTEGER REFERENCES users(id),
    action       TEXT,
    target_type  TEXT,
    target_id    INTEGER,
    timestamp    TEXT DEFAULT {_PG_NOW},
    household_id INTEGER REFERENCES households(id)
);
CREATE INDEX idx_rooms_household ON rooms(household_id);
CREATE INDEX idx_shelves_household ON shelves(household_id);
CREATE INDEX idx_inventory_household ON inventory(household_id);
CREATE INDEX idx_shopping_lists_household ON shopping_lists(household_id);
CREATE INDEX idx_shopping_list_items_household ON shopping_list_items(household_id);
CREATE INDEX idx_audit_logs_household ON audit_logs(household_id);
CREATE INDEX idx_inventory_barcode ON inventory(household_id, barcode);

CREATE TABLE auth_tokens (
    id           {_PG_ID},
    purpose      TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password', 'invite')),
    token_hash   TEXT NOT NULL UNIQUE,
    user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
    email        TEXT,
    household_id INTEGER REFERENCES households(id) ON DELETE CASCADE,
    role         TEXT,
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT {_PG_NOW},
    expires_at   TEXT NOT NULL,
    used_at      TEXT
);
CREATE INDEX idx_auth_tokens_lookup ON auth_tokens(purpose, email);
CREATE INDEX idx_auth_tokens_household ON auth_tokens(household_id);

CREATE TABLE share_links (
    id           {_PG_ID},
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    list_id      INTEGER REFERENCES shopping_lists(id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT {_PG_NOW},
    expires_at   TEXT,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE TABLE api_keys (
    id           {_PG_ID},
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    key_prefix   TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT {_PG_NOW},
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX idx_share_links_household ON share_links(household_id);
CREATE INDEX idx_api_keys_household ON api_keys(household_id);

-- Child rows inserted without a household take their parent's (the app always
-- passes one; this keeps hand-written and imported inserts working).
CREATE FUNCTION sg_fill_household() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.household_id IS NULL THEN
        IF TG_TABLE_NAME = 'shelves' THEN
            SELECT household_id INTO NEW.household_id FROM rooms WHERE id = NEW.room_id;
        ELSIF TG_TABLE_NAME = 'shopping_list_items' THEN
            SELECT household_id INTO NEW.household_id FROM shopping_lists WHERE id = NEW.list_id;
        ELSIF TG_TABLE_NAME = 'inventory' THEN
            SELECT household_id INTO NEW.household_id FROM rooms WHERE id = NEW.room_id;
            IF NEW.household_id IS NULL THEN
                SELECT household_id INTO NEW.household_id FROM shelves WHERE id = NEW.shelf_id;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_shelves_household BEFORE INSERT ON shelves
    FOR EACH ROW EXECUTE FUNCTION sg_fill_household();
CREATE TRIGGER trg_inventory_household BEFORE INSERT ON inventory
    FOR EACH ROW EXECUTE FUNCTION sg_fill_household();
CREATE TRIGGER trg_shopping_list_items_household BEFORE INSERT ON shopping_list_items
    FOR EACH ROW EXECUTE FUNCTION sg_fill_household();
"""


def _pg_baseline(conn):
    """Create the full schema (= SQLite version PG_BASELINE_VERSION) and seed
    the same rows a fresh SQLite install gets."""
    raw = conn.raw
    raw.execute(_PG_BASELINE)
    raw.execute("INSERT INTO households (id, name) VALUES (%s, 'Home')", (DEFAULT_HOUSEHOLD_ID,))
    raw.execute("INSERT INTO users (id, username, password_hash, role) VALUES (1, '__system__', '', 'system')")
    raw.execute(
        "INSERT INTO users (id, username, password_hash, role, must_change_password, is_superuser) "
        "VALUES (2, 'admin', %s, 'admin', 1, 1)", (_ph.hash("admin"),))
    raw.execute("INSERT INTO household_members (household_id, user_id, role) VALUES (%s, 2, 'owner')",
                (DEFAULT_HOUSEHOLD_ID,))
    for table in ("households", "users"):
        raw.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT MAX(id) FROM {table}))")
    logger.info("Auth: seeded __system__ (id=1) and default admin user (must change password)")


def _run_postgres():
    """Postgres migration runner: one transaction, guarded by an advisory lock
    so several app processes starting together don't race."""
    from stashgrid import db

    conn = db.connect()
    raw = conn.raw
    applied = []
    try:
        with raw.transaction():
            raw.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_ID,))
            raw.execute(f"""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    name       TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT {_PG_NOW}
                )
            """)
            have = raw.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
            if have == 0:
                logger.info("DB: creating Postgres schema (baseline = version %d)", PG_BASELINE_VERSION)
                _pg_baseline(conn)
                for version, name, _fn in MIGRATIONS:
                    if version <= PG_BASELINE_VERSION:
                        raw.execute("INSERT INTO schema_version (version, name) VALUES (%s, %s)", (version, name))
                        applied.append(version)
                have = PG_BASELINE_VERSION
            for version, name, _fn in MIGRATIONS:
                if version <= have:
                    continue
                fn = POSTGRES_MIGRATIONS.get(version)
                if fn is None:
                    raise RuntimeError(f"migration {version} ({name}) has no Postgres version; "
                                       "add one to POSTGRES_MIGRATIONS")
                logger.info("DB: applying migration %d: %s", version, name)
                fn(conn)
                raw.execute("INSERT INTO schema_version (version, name) VALUES (%s, %s)", (version, name))
                applied.append(version)
        return applied
    except Exception:
        logger.exception("DB: Postgres migration failed; nothing was changed")
        raise
    finally:
        conn.close()


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
    """Apply pending migrations in order. Returns the list of versions applied.
    `path` forces a specific SQLite file (tests, the import tool)."""
    if path is None and _db.backend() == "postgres":
        return _run_postgres()
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
