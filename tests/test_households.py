"""Household isolation (SG-32): one household can never read or change
another's data, through the app or directly in the database."""
import ast
import io
import json
import os
import pathlib
import re
import sqlite3

import pytest

from conftest import ROOT

import stashgrid.inventory.routes as inv_routes
from stashgrid.core.sharing import create_api_key, create_share_link
from stashgrid.db import get_db


@pytest.fixture
def two(db, admin_id, make_household, make_user, login):
    """Household 1 (admin) with data seeded directly; household 2 (bob) with
    data created through the app."""
    ids = {"admin_id": admin_id}
    hh2 = make_household("Other")
    assert hh2 == 2
    make_user("bob", role="owner", household=2)
    ids["A_room"] = db.run("INSERT INTO rooms (name, is_default, household_id) VALUES ('A-Basement', 1, 1)")
    ids["A_shelf"] = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", ids["A_room"])
    ids["A_item"] = db.run("INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity) "
                           "VALUES ('AAA', 'A-Secret-Beans', ?, ?, 5)", ids["A_room"], ids["A_shelf"])
    ids["A_list"] = db.run("INSERT INTO shopping_lists (name, store_name, is_default, household_id) "
                           "VALUES ('A-Costco', 'Costco', 1, 1)")
    ids["A_li"] = db.run("INSERT INTO shopping_list_items (list_id, barcode, name) VALUES (?, 'AAA', 'A-Secret-Beans')",
                         ids["A_list"])

    a, b = login("admin"), login("bob")
    b.post("/api/rooms", data={"name": "B-Garage", "shelf_count": "2"})
    ids["B_room"] = db.value("SELECT id FROM rooms WHERE name='B-Garage'")
    ids["B_shelf"] = db.value("SELECT id FROM shelves WHERE room_id=? ORDER BY shelf_number", ids["B_room"])
    assert b.post("/api/scan", data={"barcode": "BBB", "shelf_id": ids["B_shelf"], "room_id": ids["B_room"]}).status_code == 200
    ids["B_item"] = db.value("SELECT id FROM inventory WHERE barcode='BBB'")
    ids["B_list"] = b.post("/api/shopping-lists/create", data={"name": "B-List"}).get_json()["list_id"]
    b.post("/api/shopping-lists/add", data={"list_id": ids["B_list"], "barcode": "BBB", "name": "B-Thing", "qty": 1})
    ids["B_li"] = db.value("SELECT id FROM shopping_list_items WHERE list_id=?", ids["B_list"])
    ids["a"], ids["b"] = a, b
    return ids


def test_child_rows_inherit_household(db, two):
    assert db.value("SELECT household_id FROM shelves WHERE id=?", two["A_shelf"]) == 1
    assert db.value("SELECT household_id FROM inventory WHERE id=?", two["A_item"]) == 1
    assert db.value("SELECT household_id FROM shopping_list_items WHERE id=?", two["A_li"]) == 1


def test_app_created_rows_land_in_own_household(db, two):
    assert db.one("SELECT household_id, is_default FROM rooms WHERE id=?", two["B_room"]) == (2, 1)
    assert db.value("SELECT is_default FROM rooms WHERE id=?", two["A_room"]) == 1
    assert {r[0] for r in db.all("SELECT household_id FROM shelves WHERE room_id=?", two["B_room"])} == {2}
    assert db.one("SELECT household_id, is_default FROM shopping_lists WHERE id=?", two["B_list"]) == (2, 1)
    assert db.value("SELECT is_default FROM shopping_lists WHERE id=?", two["A_list"]) == 1


def _attacks(t):
    return [
        ("POST", f"/api/rooms/{t['A_room']}/set-default", {}),
        ("POST", f"/api/rooms/{t['A_room']}/add-shelves", {"add_count": "3"}),
        ("POST", f"/api/rooms/{t['A_room']}/delete", {}),
        ("POST", "/api/scan", {"barcode": "EVIL", "shelf_id": t["A_shelf"], "room_id": t["A_room"]}),
        ("POST", "/api/scan", {"barcode": "EVIL", "shelf_id": t["A_shelf"], "room_id": t["B_room"]}),
        ("POST", f"/api/items/{t['A_item']}/photo", "PHOTO"),
        ("POST", f"/edit/{t['A_item']}", {"name": "pwned", "notes": "", "low_stock_threshold": "0"}),
        ("POST", f"/edit/{t['B_item']}", {"name": "moved", "notes": "", "new_shelf_id": str(t["A_shelf"])}),
        ("POST", f"/remove/{t['A_item']}", {"amount": "1"}),
        ("POST", f"/delete/{t['A_item']}", {}),
        ("POST", "/api/shopping-lists/add", {"list_id": t["A_list"], "barcode": "EVIL", "name": "evil"}),
        ("POST", f"/api/shopping-lists/toggle-complete/{t['A_li']}", {}),
        ("POST", "/api/shopping-lists/commit", {"list_id": t["A_list"]}),
        ("POST", f"/api/shopping-lists/{t['A_list']}/set-default", {}),
        ("POST", f"/api/shopping-lists/{t['A_list']}/rename", {"name": "pwned"}),
        ("POST", f"/api/shopping-lists/{t['A_list']}/delete", {}),
        ("POST", f"/admin/users/{t['admin_id']}/reset-password", {"password": "Another-Strong-9"}),
        ("POST", f"/admin/users/{t['admin_id']}/role", {"role": "member"}),
    ]


def _snapshot(db, t):
    return {
        "room": db.all("SELECT * FROM rooms WHERE id=?", t["A_room"]),
        "shelves": db.all("SELECT * FROM shelves WHERE room_id=?", t["A_room"]),
        "item": db.all("SELECT * FROM inventory WHERE id=?", t["A_item"]),
        "list": db.all("SELECT * FROM shopping_lists WHERE id=?", t["A_list"]),
        "list_items": db.all("SELECT * FROM shopping_list_items WHERE list_id=?", t["A_list"]),
        "admin": db.all("SELECT password_hash, must_change_password, session_version FROM users WHERE username='admin'"),
        "members": db.all("SELECT * FROM household_members WHERE household_id=1"),
        "inventory_count": db.value("SELECT COUNT(*) FROM inventory"),
    }


def test_every_write_route_refuses_other_households_ids(db, two):
    before = _snapshot(db, two)
    for method, path, data in _attacks(two):
        if data == "PHOTO":
            photo = (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "x.png")
            r = two["b"].post(path, data={"photo": photo}, content_type="multipart/form-data")
        else:
            r = two["b"].open(path, method=method, data=data)
        assert r.status_code == 404, (path, data, r.status_code)
    assert _snapshot(db, two) == before
    assert not os.listdir(inv_routes.PHOTOS_DIR)
    assert db.one("SELECT room_id, shelf_id FROM inventory WHERE id=?", two["B_item"]) == (two["B_room"], two["B_shelf"])


def test_reads_never_show_other_households_data(db, two):
    b, A_room = two["b"], two["A_room"]
    for path in ["/", f"/?room_id={A_room}", f"/api/items?room_id={A_room}", "/api/items?q=Secret",
                 "/api/items?q=A-", "/api/shopping-lists", "/admin/users"]:
        body = b.get(path).get_data(as_text=True)
        assert not [s for s in ("A-Secret-Beans", "A-Basement", "A-Costco") if s in body], path
    names = re.findall(r'class="user-name">([^<]+)<', b.get("/admin/users").get_data(as_text=True))
    assert names == ["bob"]

    h1 = b.get(f"/api/inventory-hash?room_id={A_room}").get_json()["hash"]
    db.run("UPDATE inventory SET quantity = quantity + 1 WHERE id=?", two["A_item"])
    assert b.get(f"/api/inventory-hash?room_id={A_room}").get_json()["hash"] == h1

    a = two["a"]
    assert "A-Secret-Beans" in a.get(f"/api/items?room_id={A_room}").get_data(as_text=True)
    assert "B-List" not in a.get("/api/shopping-lists").get_data(as_text=True)


def test_scanner_state_only_shown_to_its_household(two):
    with open(inv_routes.SCANNER_STATE_FILE, "w") as f:
        json.dump({"mode": "IN", "room_id": two["A_room"], "shelf_id": two["A_shelf"], "shelf_number": 1}, f)
    assert two["a"].get("/api/scanner-state").get_json()["online"] is True
    assert two["b"].get("/api/scanner-state").get_json()["online"] is False


def test_share_link_and_key_stay_in_their_household(app, db, two):
    with get_db() as conn:
        token = create_share_link(conn, 1, "t")
        key = create_api_key(conn, 1, "k")
    p = app.test_client()
    names = [l["name"] for l in p.get(f"/api/public/shopping-lists/{token}").get_json()["lists"]]
    assert "A-Costco" in names and "B-List" not in names
    assert p.post(f"/api/public/shopping-lists/toggle/{token}/{two['B_li']}").status_code == 404
    assert p.post(f"/api/public/shopping-lists/add/{token}", data={"list_id": two["B_list"], "name": "x"}).status_code == 404
    p.post(f"/api/public/shopping-lists/create/{token}", data={"name": "Public-New"})
    assert db.value("SELECT household_id FROM shopping_lists WHERE name='Public-New'") == 1

    hdr = {"X-StashGrid-Key": key}
    assert p.post("/api/scan", data={"barcode": "K1", "shelf_id": two["A_shelf"], "room_id": two["A_room"]}, headers=hdr).status_code == 200
    assert p.post("/api/scan", data={"barcode": "K2", "shelf_id": two["B_shelf"], "room_id": two["B_room"]}, headers=hdr).status_code == 404


def test_audit_rows_carry_household(db, two):
    rows = dict(db.all("SELECT household_id, COUNT(*) FROM audit_logs "
                       "WHERE action IN ('create_room', 'scan_new', 'login') GROUP BY household_id"))
    assert {1, 2} <= rows.keys()


# ── database guard rails (triggers) ─────────────────────────────────────────

@pytest.mark.parametrize("sql,args", [
    ("INSERT INTO rooms (name) VALUES ('no-hh')", ()),
    ("INSERT INTO shopping_lists (name) VALUES ('no-hh')", ()),
    ("INSERT INTO inventory (barcode, room_id, shelf_id, household_id) VALUES ('X', {A_room}, {A_shelf}, 2)", ()),
    ("INSERT INTO shelves (room_id, shelf_number, household_id) VALUES ({A_room}, 9, 2)", ()),
    ("INSERT INTO shopping_list_items (list_id, name, household_id) VALUES ({A_list}, 'x', 2)", ()),
    ("UPDATE inventory SET shelf_id={A_shelf} WHERE id={B_item}", ()),
    ("UPDATE inventory SET household_id=2 WHERE id={A_item}", ()),
    ("INSERT INTO inventory (barcode) VALUES ('orphan')", ()),
])
def test_triggers_reject_cross_household_rows(db, two, sql, args):
    with pytest.raises(sqlite3.IntegrityError):
        db.run(sql.format(**two))


def test_scanner_style_insert_inherits_household(db, two):
    rid = db.run("INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity) VALUES ('SCN', 's', ?, ?, 1)",
                 two["B_room"], two["B_shelf"])
    assert db.value("SELECT household_id FROM inventory WHERE id=?", rid) == 2


# ── lint ────────────────────────────────────────────────────────────────────

def test_all_inventory_sql_on_tenant_tables_filters_by_household():
    tenant = ("rooms", "shelves", "inventory", "shopping_lists", "shopping_list_items")
    pat = re.compile(r"\b(FROM|INTO|UPDATE|JOIN)\s+(" + "|".join(tenant) + r")\b", re.I)
    offenders = []
    for f in pathlib.Path(ROOT, "stashgrid", "inventory").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and pat.search(node.value):
                if "household_id" not in node.value:
                    offenders.append(f"{f.name}:{node.lineno}")
    assert not offenders
