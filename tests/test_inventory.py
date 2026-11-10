"""Rooms, scanning, removal, low-stock auto-add, restock, escaping."""
import pytest

from conftest import POSTGRES

from stashgrid.config import SYSTEM_USER_ID


def scan(c, home, barcode, shelf="shelf"):
    return c.post("/api/scan", data={"barcode": barcode, "shelf_id": home[shelf], "room_id": home["room"]})


def add_item(db, home, barcode="ABC", name="Thing", qty=3, threshold=0, store="Costco", shelf="shelf", active=1):
    return db.run(
        "INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity, low_stock_threshold, preferred_store, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        barcode, name, home["room"], home[shelf], qty, threshold, store, active)


# ── rooms ───────────────────────────────────────────────────────────────────

def test_create_room_with_shelves(admin, db):
    admin.post("/api/rooms", data={"name": "Garage", "shelf_count": "3"})
    room = db.one("SELECT id, household_id, is_default FROM rooms WHERE name='Garage'")
    assert room[1] == 1 and room[2] == 1     # first room becomes the default
    assert db.value("SELECT COUNT(*) FROM shelves WHERE room_id=?", room[0]) == 3


def test_delete_room_keeps_items_as_removed(admin, db, home):
    other = db.run("INSERT INTO rooms (name, household_id) VALUES ('Garage', 1)")
    other_shelf = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", other)
    item = add_item(db, home)
    r = admin.post(f"/api/rooms/{home['room']}/delete", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert [r[0] for r in db.all("SELECT id FROM rooms")] == [other]
    assert db.one("SELECT is_active, room_id, shelf_id FROM inventory WHERE id=?", item) == (0, None, None)
    assert db.value("SELECT COUNT(*) FROM shelves WHERE id=?", other_shelf) == 1
    if not POSTGRES:
        assert not db.all("PRAGMA foreign_key_check")


# ── scanning ────────────────────────────────────────────────────────────────

def test_scan_new_then_increment(admin, db, home):
    r = scan(admin, home, "x1")
    assert r.status_code == 200 and r.get_json()["is_new"] is True and r.get_json()["name"] == "Test Item"
    r = scan(admin, home, "X1")
    assert r.get_json() == {**r.get_json(), "is_new": False, "quantity": 2}
    assert db.value("SELECT COUNT(*) FROM inventory WHERE barcode='X1'") == 1


def test_same_barcode_on_another_shelf_is_a_separate_row(admin, db, home):
    scan(admin, home, "X1")
    scan(admin, home, "X1", shelf="shelf2")
    assert db.value("SELECT COUNT(*) FROM inventory WHERE barcode='X1'") == 2


def test_scan_audit_attribution(app, admin, admin_id, db, home):
    scan(admin, home, "X1")
    assert db.value("SELECT user_id FROM audit_logs WHERE action='scan_new'") == admin_id


@pytest.mark.parametrize("data,status", [
    ({"barcode": "", "shelf_id": 1, "room_id": 1}, 400),
    ({"barcode": "Z", "shelf_id": 999, "room_id": None}, 404),
])
def test_scan_rejects_bad_input(admin, db, home, data, status):
    data = {**data, "room_id": data["room_id"] or home["room"]}
    assert admin.post("/api/scan", data=data).status_code == status
    assert db.value("SELECT COUNT(*) FROM inventory") == 0


def test_scan_shelf_must_belong_to_room(admin, db, home):
    other = db.run("INSERT INTO rooms (name, household_id) VALUES ('Garage', 1)")
    r = admin.post("/api/scan", data={"barcode": "Z", "shelf_id": home["shelf"], "room_id": other})
    assert r.status_code == 404


# ── removing stock ──────────────────────────────────────────────────────────

def test_remove_quantity(admin, db, home):
    item = add_item(db, home, qty=3)
    admin.post(f"/remove/{item}", data={"amount": "1"})
    assert db.one("SELECT quantity, is_active FROM inventory WHERE id=?", item) == (2, 1)
    admin.post(f"/remove/{item}", data={"amount": "5"})
    assert db.one("SELECT quantity, is_active FROM inventory WHERE id=?", item) == (0, 0)


def test_low_stock_auto_adds_to_matching_store_list(admin, db, home):
    hw = db.run("INSERT INTO shopping_lists (name, store_name, household_id) VALUES ('HW', 'Home Depot', 1)")
    item = add_item(db, home, barcode="SCREW", name="Screws", qty=3, threshold=3, store="home depot")
    admin.post(f"/remove/{item}", data={"amount": "1"})
    assert db.all("SELECT list_id, name FROM shopping_list_items") == [(hw, "Screws")]
    admin.post(f"/remove/{item}", data={"amount": "1"})
    assert db.value("SELECT COUNT(*) FROM shopping_list_items") == 1, "not added twice"


def test_low_stock_falls_back_to_default_list(admin, db, home):
    item = add_item(db, home, qty=2, threshold=2, store="Nowhere")
    admin.post(f"/remove/{item}", data={"amount": "1"})
    assert db.all("SELECT list_id FROM shopping_list_items") == [(home["list"],)]


def test_no_auto_add_above_threshold_or_without_one(admin, db, home):
    a = add_item(db, home, barcode="A", qty=5, threshold=2)
    b = add_item(db, home, barcode="B", qty=1, threshold=0)
    admin.post(f"/remove/{a}", data={"amount": "1"})
    admin.post(f"/remove/{b}", data={"amount": "1"})
    assert db.value("SELECT COUNT(*) FROM shopping_list_items") == 0


# ── restock from a shopping list ────────────────────────────────────────────

def _completed(db, home, barcode, qty=2):
    return db.run("INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty, is_completed) "
                  "VALUES (?, ?, 'x', ?, 1)", home["list"], barcode, qty)


def test_restock_adds_completed_items_to_stock(admin, db, home):
    item = add_item(db, home, barcode="BEANS", qty=1)
    _completed(db, home, "BEANS", qty=4)
    open_item = db.run("INSERT INTO shopping_list_items (list_id, barcode, name) VALUES (?, 'BEANS', 'x')", home["list"])
    r = admin.post("/api/shopping-lists/commit", data={"list_id": home["list"]})
    assert r.status_code == 200 and [x["qty"] for x in r.get_json()["restocked"]] == [4]
    assert db.value("SELECT quantity FROM inventory WHERE id=?", item) == 5
    assert [r[0] for r in db.all("SELECT id FROM shopping_list_items")] == [open_item]


def test_restock_reports_unknown_barcodes(admin, db, home):
    _completed(db, home, "NEVER-SEEN")
    r = admin.post("/api/shopping-lists/commit", data={"list_id": home["list"]})
    assert [x["barcode"] for x in r.get_json()["unknown_barcodes"]] == ["NEVER-SEEN"]


def _tick_all_and_restock(admin, db, home):
    for (item_id,) in db.all("SELECT id FROM shopping_list_items WHERE is_completed = 0"):
        admin.post(f"/api/shopping-lists/toggle-complete/{item_id}")
    r = admin.post("/api/shopping-lists/commit", data={"list_id": home["list"]})
    assert r.status_code == 200
    return r.get_json()


def test_last_unit_out_then_restock_brings_it_back(admin, db, home):
    """SG-13 acceptance: scan out the last unit -> auto-listed -> tick -> restock."""
    item = add_item(db, home, barcode="RICE", qty=1, threshold=1, shelf="shelf2")
    admin.post(f"/remove/{item}", data={"amount": "1"})
    assert db.one("SELECT quantity, is_active FROM inventory WHERE id=?", item) == (0, 0)
    assert db.one("SELECT barcode, inventory_id FROM shopping_list_items") == ("RICE", item)
    db.run("UPDATE shopping_list_items SET requested_qty = 3")
    data = _tick_all_and_restock(admin, db, home)
    assert [x["inventory_id"] for x in data["restocked"]] == [item] and data["unknown_barcodes"] == []
    assert db.one("SELECT quantity, is_active, removed_at, room_id, shelf_id FROM inventory WHERE id=?", item) == \
        (3, 1, None, home["room"], home["shelf2"])
    assert db.value("SELECT COUNT(*) FROM shopping_list_items") == 0


def test_restock_goes_back_to_the_shelf_it_was_taken_from(admin, db, home):
    """SG-14 acceptance: stock on two shelves; the one that ran low gets the restock."""
    low = add_item(db, home, barcode="OIL", qty=2, threshold=2, shelf="shelf")    # older row
    full = add_item(db, home, barcode="OIL", qty=9, shelf="shelf2")               # newer row
    admin.post(f"/remove/{low}", data={"amount": "1"})
    _tick_all_and_restock(admin, db, home)
    assert db.value("SELECT quantity FROM inventory WHERE id=?", low) == 2
    assert db.value("SELECT quantity FROM inventory WHERE id=?", full) == 9


def test_hand_added_item_restocks_most_recently_updated_location(admin, db, home):
    older = add_item(db, home, barcode="SOAP", qty=1, shelf="shelf")
    newer = add_item(db, home, barcode="SOAP", qty=1, shelf="shelf2")
    db.run("UPDATE inventory SET updated_at = '2026-01-01 00:00:00' WHERE id = ?", older)
    db.run("UPDATE inventory SET updated_at = '2026-06-01 00:00:00' WHERE id = ?", newer)
    _completed(db, home, "SOAP", qty=2)
    admin.post("/api/shopping-lists/commit", data={"list_id": home["list"]})
    assert (db.value("SELECT quantity FROM inventory WHERE id=?", older),
            db.value("SELECT quantity FROM inventory WHERE id=?", newer)) == (1, 3)


def test_hand_added_item_that_ran_out_is_brought_back(admin, db, home):
    item = add_item(db, home, barcode="RICE", qty=0, active=0)
    _completed(db, home, "RICE", qty=3)
    admin.post("/api/shopping-lists/commit", data={"list_id": home["list"]})
    assert db.one("SELECT quantity, is_active FROM inventory WHERE id=?", item) == (3, 1)


def test_restock_does_not_duplicate_a_rescanned_item(admin, db, home):
    """Ran out, then scanned back onto the same shelf before restocking: the
    bought stock joins the active row instead of reviving the old one."""
    old = add_item(db, home, barcode="TEA", qty=1, threshold=1)
    admin.post(f"/remove/{old}", data={"amount": "1"})
    scan(admin, home, "TEA")
    new = db.value("SELECT id FROM inventory WHERE barcode='TEA' AND is_active=1")
    _tick_all_and_restock(admin, db, home)
    assert db.value("SELECT COUNT(*) FROM inventory WHERE barcode='TEA' AND is_active=1") == 1
    assert db.value("SELECT quantity FROM inventory WHERE id=?", new) == 2
    assert db.value("SELECT is_active FROM inventory WHERE id=?", old) == 0


def test_restock_skips_rows_whose_room_was_deleted(admin, db, home):
    """A remembered row with no location left falls back to the barcode's other rows."""
    garage = db.run("INSERT INTO rooms (name, household_id) VALUES ('Garage', 1)")
    gshelf = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", garage)
    gone = db.run("INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity, low_stock_threshold) "
                  "VALUES ('NAILS', 'Nails', ?, ?, 1, 1)", garage, gshelf)
    admin.post(f"/remove/{gone}", data={"amount": "1"})
    admin.post(f"/api/rooms/{garage}/delete")
    data = _tick_all_and_restock(admin, db, home)
    assert data["restocked"] == [] and [x["barcode"] for x in data["unknown_barcodes"]] == ["NAILS"]
    assert db.value("SELECT is_completed FROM shopping_list_items WHERE barcode='NAILS'") == 1, "stays on the list"
    kept = add_item(db, home, barcode="NAILS", qty=5)
    _tick_all_and_restock(admin, db, home)
    assert db.value("SELECT quantity FROM inventory WHERE id=?", kept) == 6


def test_restock_never_touches_another_households_row(admin, db, home, make_household):
    other = make_household("Other")
    room = db.run("INSERT INTO rooms (name, household_id) VALUES ('Theirs', ?)", other)
    shelf = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", room)
    theirs = db.run("INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity) VALUES ('X', 'x', ?, ?, 1)",
                    room, shelf)
    db.run("INSERT INTO shopping_list_items (list_id, barcode, name, requested_qty, is_completed, inventory_id) "
           "VALUES (?, 'X', 'x', 5, 1, ?)", home["list"], theirs)
    data = admin.post("/api/shopping-lists/commit", data={"list_id": home["list"]}).get_json()
    assert data["restocked"] == [] and db.value("SELECT quantity FROM inventory WHERE id=?", theirs) == 1


# ── shopping lists ──────────────────────────────────────────────────────────

def test_add_to_missing_list_is_404(admin, db, home):
    assert admin.post("/api/shopping-lists/add", data={"list_id": 999, "barcode": "Q", "name": "q"}).status_code == 404
    assert admin.post("/api/shopping-lists/add", data={"list_id": home["list"], "barcode": "Q", "name": "q"}).status_code == 200
    assert db.value("SELECT COUNT(*) FROM shopping_list_items") == 1


# ── escaping ────────────────────────────────────────────────────────────────

XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def test_item_names_are_escaped(admin, db, home):
    add_item(db, home, name=XSS)
    db.run("UPDATE rooms SET name=? WHERE id=?", XSS, home["room"])
    for html in (admin.get(f"/?room_id={home['room']}").get_data(as_text=True),
                 admin.get(f"/api/items?room_id={home['room']}").get_json()["html"]):
        assert XSS not in html
        assert "<img src=x onerror" not in html


def test_public_page_escapes_list_names(client, db, home):
    from stashgrid.core.sharing import create_share_link
    from stashgrid.db import get_db
    db.run("UPDATE shopping_lists SET name=? WHERE id=?", XSS, home["list"])
    with get_db() as conn:
        token = create_share_link(conn, 1, "t")
    assert XSS not in client.get(f"/shop/{token}").get_data(as_text=True)


def test_scanner_key_scans_are_attributed_to_system(app, db, home):
    from stashgrid.core.sharing import create_api_key
    from stashgrid.db import get_db
    with get_db() as conn:
        key = create_api_key(conn, 1, "k")
    app.test_client().post("/api/scan", data={"barcode": "K", "shelf_id": home["shelf"], "room_id": home["room"]},
                           headers={"X-StashGrid-Key": key})
    assert db.value("SELECT user_id FROM audit_logs WHERE action='scan_new'") == SYSTEM_USER_ID
