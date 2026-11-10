"""Importing a self-hosted SQLite household into the configured database
(SG-34). Runs against whichever backend the suite is using."""
import hashlib
import os
import sqlite3

import pytest

from conftest import PASSWORD, _PASSWORD_HASH

from stashgrid import migrations
from stashgrid.core.sharing import create_api_key, create_share_link
from stashgrid.tools import import_sqlite
from stashgrid.tools.import_sqlite import SourceError, import_household

SOURCE_LINK = "fedcba9876543210fedcba9876543210"


@pytest.fixture
def source(app, tmp_path, monkeypatch):
    """A Pi database: 'Lake House' with two members, a room, shelves, items
    (one with a photo, one removed), a list, a share link, a scanner key."""
    path = str(tmp_path / "pi" / "inventory.db")
    os.makedirs(os.path.dirname(path))
    monkeypatch.setattr(migrations, "BACKUP_DIR", str(tmp_path / "pi" / "backups"))
    migrations.run_migrations(path)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    x = lambda sql, *a: c.execute(sql, a).lastrowid
    x("UPDATE households SET name='Lake House' WHERE id=1")
    x("UPDATE users SET password_hash=?, must_change_password=0, email='pi-admin@example.com', "
      "email_verified_at='2026-01-01 00:00:00', display_name='Pi Admin' WHERE username='admin'", _PASSWORD_HASH)
    kid = x("INSERT INTO users (username, password_hash, email, email_verified_at) VALUES ('kid', ?, 'kid@example.com', '2026-01-01 00:00:00')",
            _PASSWORD_HASH)
    x("INSERT INTO household_members (household_id, user_id, role) VALUES (1, ?, 'member')", kid)
    room = x("INSERT INTO rooms (name, is_default, household_id) VALUES ('Pantry', 1, 1)")
    s1 = x("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", room)
    s2 = x("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 2)", room)
    beans = x("INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity, low_stock_threshold, photo_path) "
              "VALUES ('111', 'Beans', ?, ?, 4, 2, '/static/photos/3.jpg')", room, s1)
    x("INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity) VALUES ('222', 'Rice', ?, ?, 2)", room, s2)
    x("INSERT INTO inventory (barcode, name, quantity, is_active, removed_reason, household_id) "
      "VALUES ('333', 'Gone', 0, 0, 'used up', 1)")
    lst = x("INSERT INTO shopping_lists (name, store_name, is_default, household_id) VALUES ('Costco', 'Costco', 1, 1)")
    x("INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty) VALUES (?, '111', 'Beans', 3)", lst)
    x("INSERT INTO share_links (household_id, list_id, label, token_hash) VALUES (1, ?, 'Fridge QR', ?)",
      lst, hashlib.sha256(SOURCE_LINK.encode()).hexdigest())
    x("INSERT INTO api_keys (household_id, label, key_prefix, key_hash) VALUES (1, 'Pi scanner', 'sgk_ab', ?)",
      hashlib.sha256(b"sgk_pi_scanner_key").hexdigest())
    x("INSERT INTO audit_logs (user_id, action, target_type, target_id, household_id) VALUES (?, 'edit_item', 'item', ?, 1)",
      kid, beans)
    c.commit()
    c.close()
    photos = tmp_path / "pi" / "photos"
    photos.mkdir()
    (photos / "3.jpg").write_bytes(b"\xff\xd8 fake jpeg")
    return {"path": path, "photos": str(photos), "photos_to": str(tmp_path / "hosted-photos")}


def sha_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def run(source, **kw):
    return import_household(source["path"], photos_from=source["photos"], photos_to=source["photos_to"], **kw)


def test_import_household(app, db, source, login):
    before = sha_file(source["path"])
    report = run(source)
    hid = report["household"]["id"]
    assert hid != 1 and db.value("SELECT name FROM households WHERE id=?", hid) == "Lake House"
    assert report["counts"] == {**report["counts"], "members": 2, "rooms": 1, "shelves": 2, "inventory": 3,
                                "shopping_lists": 1, "shopping_list_items": 1, "share_links": 1, "api_keys": 1,
                                "audit_logs": 1, "photos": 1}
    assert sha_file(source["path"]) == before, "source file untouched"

    # 'admin' already exists in the target, so the Pi admin is renamed.
    assert report["renamed"] == ["admin -> admin-2"]
    assert db.all("SELECT u.username, m.role FROM household_members m JOIN users u ON u.id=m.user_id "
                  "WHERE m.household_id=? ORDER BY u.username", hid) == [("admin-2", "owner"), ("kid", "member")]
    c = login("pi-admin@example.com")                     # same password as on the Pi
    assert "Pi Admin @ Lake House" in c.get("/").get_data(as_text=True)
    items = c.get("/api/items?q=").get_json()
    body = c.get(f"/api/items?room_id={db.value('SELECT id FROM rooms WHERE household_id=?', hid)}").get_json()["html"]
    assert "Beans" in body and "Rice" in body and "Gone" not in body and items is not None

    # Rows point at the new ids, and nothing leaked into household 1.
    room = db.value("SELECT id FROM rooms WHERE household_id=?", hid)
    assert {r[0] for r in db.all("SELECT room_id FROM inventory WHERE household_id=? AND is_active=1", hid)} == {room}
    assert db.value("SELECT COUNT(*) FROM inventory WHERE household_id=1") == 0
    beans = db.value("SELECT id FROM inventory WHERE household_id=? AND barcode='111'", hid)
    assert db.value("SELECT photo_path FROM inventory WHERE id=?", beans) == f"/static/photos/{beans}.jpg"
    assert os.path.isfile(os.path.join(source["photos_to"], f"{beans}.jpg"))
    assert db.value("SELECT target_id FROM audit_logs WHERE household_id=? AND action='edit_item'", hid) == beans

    # The family's link and the Pi's scanner key keep working.
    p = app.test_client()
    lists = p.get(f"/api/public/shopping-lists/{SOURCE_LINK}").get_json()["lists"]
    assert [l["name"] for l in lists] == ["Costco"] and [i["name"] for i in lists[0]["items"]] == ["Beans"]
    shelf = db.value("SELECT id FROM shelves WHERE household_id=? AND shelf_number=1", hid)
    r = p.post("/api/scan", data={"barcode": "111", "room_id": room, "shelf_id": shelf},
               headers={"X-StashGrid-Key": "sgk_pi_scanner_key"})
    assert r.status_code == 200 and db.value("SELECT quantity FROM inventory WHERE id=?", beans) == 5


def test_existing_verified_email_is_merged(db, make_user, source):
    existing = make_user("kiddo", household=None, email="kid@example.com")
    report = run(source)
    hid = report["household"]["id"]
    assert report["merged"] == ["kid -> existing account kiddo (kid@example.com)"]
    assert db.value("SELECT role FROM household_members WHERE household_id=? AND user_id=?", hid, existing) == "member"
    assert db.value("SELECT COUNT(*) FROM users WHERE username='kid'") == 0


def test_dry_run_writes_nothing(db, source):
    before = {t: db.value(f"SELECT COUNT(*) FROM {t}") for t in ("households", "users", "inventory", "share_links")}
    report = run(source, dry_run=True)
    assert report["counts"]["inventory"] == 3
    assert {t: db.value(f"SELECT COUNT(*) FROM {t}") for t in before} == before
    assert not os.path.exists(source["photos_to"])


def test_importing_twice_fails_cleanly(db, source):
    run(source)
    households = db.value("SELECT COUNT(*) FROM households")
    photos = sorted(os.listdir(source["photos_to"]))
    with pytest.raises(SourceError, match="imported before"):
        run(source)
    assert db.value("SELECT COUNT(*) FROM households") == households
    assert sorted(os.listdir(source["photos_to"])) == photos


def test_source_with_several_households_needs_a_choice(db, source):
    c = sqlite3.connect(source["path"])
    c.execute("INSERT INTO households (name) VALUES ('Second')")
    c.commit()
    c.close()
    with pytest.raises(SourceError, match="--household"):
        run(source)
    assert run(source, household=1)["household"]["name"] == "Lake House"


def test_missing_photos_are_reported(db, source):
    os.remove(os.path.join(source["photos"], "3.jpg"))
    report = run(source)
    assert any("photo missing" in w for w in report["warnings"])
    assert report["counts"]["photos"] == 0


def test_cli(db, source, capsys):
    assert import_sqlite.main([source["path"], "--name", "Cabin", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Cabin" in out and "renamed: admin -> admin-2" in out
    assert import_sqlite.main([str(source["path"]) + ".nope"]) == 1
