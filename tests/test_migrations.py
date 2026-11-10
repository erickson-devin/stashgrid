"""Versioned migrations (SG-07), including upgrades of old schemas."""
import glob
import os
import sqlite3

import pytest

from conftest import DB, POSTGRES, sqlite_only

from stashgrid import migrations
from stashgrid.db import sqlite_connect

LATEST = migrations.MIGRATIONS[-1][0]
ALL_VERSIONS = [m[0] for m in migrations.MIGRATIONS]


def backups():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(migrations.BACKUP_DIR, "*.db")))


def snapshot(path):
    c = sqlite3.connect(path)
    try:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name NOT LIKE 'sqlite_%' AND name != 'schema_version'")]
        return {t: sorted(map(repr, c.execute(f"SELECT * FROM {t}"))) for t in tables}
    finally:
        c.close()


def test_versions_are_sequential():
    assert ALL_VERSIONS == list(range(1, len(ALL_VERSIONS) + 1))


def test_fresh_database(db):
    assert [r[0] for r in db.all("SELECT version FROM schema_version ORDER BY version")] == ALL_VERSIONS
    assert [r[0] for r in db.all("SELECT username FROM users ORDER BY id")] == ["__system__", "admin"]
    assert db.all("SELECT id, name FROM households") == [(1, "Home")]
    assert db.value("SELECT m.role FROM household_members m JOIN users u ON u.id=m.user_id WHERE u.username='admin'") == "owner"
    assert db.value("SELECT must_change_password FROM users WHERE username='admin'") == 1
    assert backups() == [], "no backup for an empty database"
    # New rows get ids after the seeded ones.
    assert db.run("INSERT INTO households (name) VALUES ('Next')") == 2


@sqlite_only
def test_sqlite_connection_settings(app, db_path):
    conn = sqlite_connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


@sqlite_only
def test_rerun_is_a_noop(db_path):
    before = snapshot(db_path)
    assert migrations.run_migrations() == []
    assert snapshot(db_path) == before and backups() == []


LEGACY_SCHEMA = """
CREATE TABLE rooms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, is_default INTEGER DEFAULT 0);
CREATE TABLE shelves (id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL, shelf_number INTEGER NOT NULL,
  FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE);
CREATE TABLE inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT NOT NULL, name TEXT, room_id INTEGER, shelf_id INTEGER,
  quantity INTEGER DEFAULT 1, notes TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, is_active INTEGER DEFAULT 1, removed_at TEXT, removed_reason TEXT,
  FOREIGN KEY(room_id) REFERENCES rooms(id), FOREIGN KEY(shelf_id) REFERENCES shelves(id));
CREATE TABLE shopping_lists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, store_name TEXT, is_default INTEGER DEFAULT 0);
CREATE TABLE shopping_list_items (id INTEGER PRIMARY KEY AUTOINCREMENT, list_id INTEGER, barcode TEXT, name TEXT,
  requested_qty INTEGER DEFAULT 1, is_completed INTEGER DEFAULT 0,
  FOREIGN KEY(list_id) REFERENCES shopping_lists(id) ON DELETE CASCADE);
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user');
CREATE TABLE audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, target_type TEXT, target_id INTEGER,
  timestamp TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id));
INSERT INTO rooms VALUES (1,'Basement',1);
INSERT INTO shelves VALUES (1,1,1),(2,1,2),(3,99,1);              -- shelf 3: room 99 doesn't exist
INSERT INTO inventory (id,barcode,name,room_id,shelf_id,quantity) VALUES
  (1,'111','Beans',1,1,4),(2,'222','Rice',1,2,2),
  (3,'333','Ghost room',42,1,1),                                    -- room 42 gone
  (4,'444','On orphan shelf',1,3,1),                                -- shelf 3 orphaned
  (5,'555','Ghost shelf',1,77,1);                                   -- shelf 77 gone
INSERT INTO shopping_lists VALUES (1,'Costco','Costco',1);
INSERT INTO shopping_list_items (id,list_id,barcode,name) VALUES (1,1,'111','Beans'),(2,9,'999','Orphan item');
INSERT INTO users (id,username,password_hash,role) VALUES (1,'__system__','','system'),(2,'admin','x','admin'),(3,'kid','x','user');
INSERT INTO audit_logs (id,user_id,action) VALUES (1,2,'login'),(2,55,'ghost');
"""


@pytest.fixture
def legacy(app, tmp_path):
    path = str(tmp_path / "legacy.db")
    c = sqlite3.connect(path)
    c.executescript(LEGACY_SCHEMA)
    c.commit()
    c.close()
    return path


@sqlite_only
def test_legacy_database_upgrade(legacy):
    applied = migrations.run_migrations(legacy)
    assert applied == ALL_VERSIONS
    db = DB(legacy)
    users_cols = {r[1] for r in db.all("PRAGMA table_info(users)")}
    inv_cols = {r[1] for r in db.all("PRAGMA table_info(inventory)")}
    assert {"must_change_password", "session_version", "email", "is_superuser"} <= users_cols
    assert {"photo_path", "low_stock_threshold", "preferred_store", "created_by", "household_id"} <= inv_cols
    assert db.value("SELECT COUNT(*) FROM inventory") == 5, "no items lost"
    assert [r[0] for r in db.all("SELECT username FROM users ORDER BY id")] == ["__system__", "admin", "kid"]
    assert db.value("SELECT COUNT(*) FROM shelves") == 2 and db.value("SELECT COUNT(*) FROM shopping_list_items") == 1
    assert db.value("SELECT COUNT(*) FROM audit_logs") == 2
    assert not db.all("PRAGMA foreign_key_check")
    assert dict((r[0], r[1:]) for r in db.all("SELECT id, room_id, shelf_id FROM inventory")) == \
        {1: (1, 1), 2: (1, 2), 3: (None, 1), 4: (1, None), 5: (1, None)}
    for table in ("rooms", "shelves", "inventory", "shopping_lists", "shopping_list_items"):
        assert db.value(f"SELECT COUNT(*) FROM {table} WHERE household_id IS NULL") == 0, table
    assert db.all("SELECT u.username, m.role FROM household_members m JOIN users u ON u.id=m.user_id ORDER BY u.id") == \
        [("admin", "owner"), ("kid", "member")]

    names = backups()
    assert len(names) == 1 and names[0].endswith(f"pre-v{LATEST}.db")
    bk = DB(os.path.join(migrations.BACKUP_DIR, names[0]))
    assert bk.value("SELECT COUNT(*) FROM shelves") == 3, "backup holds the original rows"
    assert "must_change_password" not in {r[1] for r in bk.all("PRAGMA table_info(users)")}


@sqlite_only
def test_shelfgrid_branch_database(app, tmp_path):
    """A DB that ran the ShelfGrid branch: inventory tables gone, books kept."""
    path = str(tmp_path / "shelfgrid.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user');
        CREATE TABLE books (id INTEGER PRIMARY KEY, isbn TEXT, title TEXT);
        INSERT INTO users VALUES (1,'__system__','','system'),(2,'admin','x','admin');
        INSERT INTO books VALUES (1,'9780000000001','A Book');
    """)
    c.commit()
    c.close()
    migrations.run_migrations(path)
    db = DB(path)
    assert db.value("SELECT COUNT(*) FROM rooms") == 0
    assert db.value("SELECT COUNT(*) FROM books") == 1


def test_failing_migration_rolls_back(db, monkeypatch):
    def boom(conn):
        conn.execute("CREATE TABLE half_done (x)")
        conn.execute("UPDATE households SET name = 'changed'")
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [(LATEST + 1, "always fails", boom)])
    with pytest.raises(RuntimeError):
        migrations.run_migrations()
    assert db.value("SELECT MAX(version) FROM schema_version") == LATEST
    tables = ("SELECT 1 FROM information_schema.tables WHERE table_name='half_done'" if POSTGRES
              else "SELECT 1 FROM sqlite_master WHERE name='half_done'")
    assert not db.all(tables)
    assert db.value("SELECT name FROM households WHERE id=1") == "Home"


@sqlite_only
def test_old_backups_are_pruned(app, legacy, monkeypatch):
    os.makedirs(migrations.BACKUP_DIR, exist_ok=True)
    for i in range(migrations.BACKUPS_TO_KEEP + 3):
        open(os.path.join(migrations.BACKUP_DIR, f"inventory-20200101-0000{i:02d}-pre-v1.db"), "w").close()
    migrations.run_migrations(legacy)
    assert len(backups()) == migrations.BACKUPS_TO_KEEP
