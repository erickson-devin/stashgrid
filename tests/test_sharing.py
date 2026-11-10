"""Share links and scanner keys (SG-35), and token masking in request logs."""
import hashlib
import logging
import re

import pytest

from stashgrid import config
from stashgrid.core.sharing import create_share_link
from stashgrid.db import get_db

TUNNEL_PATH = re.compile(r"^/shop/[a-f0-9]{32}$")   # cloudflared_config.yml ingress rule
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture
def setup(app, db, admin_id, make_household, make_user, login, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://stash.example.test")
    make_household("Other")
    make_user("bob", role="owner", household=2)
    make_user("mem", role="member", household=1)
    t = {}
    t["A_room"] = db.run("INSERT INTO rooms (name, is_default, household_id) VALUES ('A-Room', 1, 1)")
    t["A_shelf"] = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", t["A_room"])
    t["A_l1"] = db.run("INSERT INTO shopping_lists (name, is_default, household_id) VALUES ('A-Costco', 1, 1)")
    t["A_l2"] = db.run("INSERT INTO shopping_lists (name, is_default, household_id) VALUES ('A-Hardware', 0, 1)")
    t["A_i1"] = db.run("INSERT INTO shopping_list_items (list_id, name) VALUES (?, 'A-Milk')", t["A_l1"])
    t["A_i2"] = db.run("INSERT INTO shopping_list_items (list_id, name) VALUES (?, 'A-Screws')", t["A_l2"])
    t["B_room"] = db.run("INSERT INTO rooms (name, is_default, household_id) VALUES ('B-Room', 1, 2)")
    t["B_shelf"] = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", t["B_room"])
    t["B_l1"] = db.run("INSERT INTO shopping_lists (name, is_default, household_id) VALUES ('B-List', 1, 2)")
    t["B_i1"] = db.run("INSERT INTO shopping_list_items (list_id, name) VALUES (?, 'B-Eggs')", t["B_l1"])
    t["a"], t["b"], t["m"] = login("admin"), login("bob"), login("mem")
    t["p"] = app.test_client()
    return t


def new_link(c, **form):
    r = c.post("/settings/links", data={"label": "x", "scope": "all", "expires": "never", **form})
    m = re.search(r'id="new-link-value">https://stash\.example\.test(/shop/([a-f0-9]{32}))</code>', r.get_data(as_text=True))
    return r, (m.group(1) if m else None), (m.group(2) if m else None)


def lists(p, token):
    return p.get(f"/api/public/shopping-lists/{token}").get_json()["lists"]


def test_settings_access(app, setup):
    assert setup["a"].get("/settings").status_code == 200
    assert setup["m"].get("/settings").status_code == 403
    assert app.test_client().get("/settings").status_code == 302


def test_all_lists_link(db, setup):
    b, p = setup["b"], setup["p"]
    r, path, token = new_link(b, label="Family chat")
    assert r.status_code == 200 and TUNNEL_PATH.match(path)
    assert 'data-qr="https://stash.example.test/shop/' in r.get_data(as_text=True)
    assert not db.all("SELECT 1 FROM share_links WHERE token_hash=?", token)
    assert db.value("SELECT household_id FROM share_links WHERE token_hash=?", sha(token)) == 2
    assert token not in b.get("/settings").get_data(as_text=True), "shown once"

    assert [l["name"] for l in lists(p, token)] == ["B-List"]
    assert p.post(f"/api/public/shopping-lists/toggle/{token}/{setup['A_i1']}").status_code == 404
    assert p.post(f"/api/public/shopping-lists/add/{token}", data={"list_id": setup["A_l1"], "name": "x"}).status_code == 404
    assert p.post(f"/api/public/shopping-lists/toggle/{token}/{setup['B_i1']}").status_code == 200
    assert p.post(f"/api/public/shopping-lists/create/{token}", data={"name": "B-New"}).status_code == 200
    assert db.value("SELECT household_id FROM shopping_lists WHERE name='B-New'") == 2
    assert db.value("SELECT last_used_at IS NOT NULL FROM share_links WHERE household_id=2") == 1


def test_single_list_link(setup):
    a, p = setup["a"], setup["p"]
    _, _, token = new_link(a, scope=str(setup["A_l1"]))
    result = lists(p, token)
    assert [l["name"] for l in result] == ["A-Costco"]
    assert [i["name"] for l in result for i in l["items"]] == ["A-Milk"]
    assert p.post(f"/api/public/shopping-lists/toggle/{token}/{setup['A_i2']}").status_code == 404
    assert p.post(f"/api/public/shopping-lists/add/{token}", data={"list_id": setup["A_l2"], "name": "x"}).status_code == 404
    assert p.post(f"/api/public/shopping-lists/add/{token}", data={"list_id": setup["A_l1"], "name": "Bread"}).status_code == 200
    assert p.post(f"/api/public/shopping-lists/create/{token}", data={"name": "nope"}).status_code == 404


def test_cannot_scope_link_to_another_households_list(setup):
    r, _, _ = new_link(setup["a"], scope=str(setup["B_l1"]))
    assert r.status_code == 404


def test_link_expiry(db, setup):
    a, p = setup["a"], setup["p"]
    _, _, token = new_link(a, label="Week", expires="7")
    assert db.value("SELECT CAST(julianday(expires_at) - julianday('now') + 0.5 AS INT) FROM share_links WHERE label='Week'") == 7
    assert p.get(f"/shop/{token}").status_code == 200
    db.run("UPDATE share_links SET expires_at = datetime('now', '-1 minute') WHERE label='Week'")
    assert p.get(f"/shop/{token}").status_code == 404
    assert p.get(f"/api/public/shopping-lists/{token}").status_code == 404
    assert b"expired" in a.get("/settings").data


def test_link_revocation(db, setup):
    a, p = setup["a"], setup["p"]
    _, _, token = new_link(a, label="Fridge")
    link_id = db.value("SELECT id FROM share_links WHERE label='Fridge'")
    assert setup["b"].post(f"/settings/links/{link_id}/revoke").status_code == 404
    assert setup["m"].post(f"/settings/links/{link_id}/revoke").status_code == 403
    assert p.get(f"/shop/{token}").status_code == 200
    a.post(f"/settings/links/{link_id}/revoke")
    assert p.get(f"/shop/{token}").status_code == 404
    assert p.get(f"/api/public/shopping-lists/{token}").status_code == 404
    assert p.post(f"/api/public/shopping-lists/toggle/{token}/{setup['A_i1']}").status_code == 404
    assert b"Fridge" not in a.get("/settings").data


def test_scanner_keys(db, setup):
    b, p = setup["b"], setup["p"]
    r = b.post("/settings/keys", data={"label": "B scanner"})
    key = re.search(r'id="new-key-value">(sgk_[a-f0-9]+)</code>', r.get_data(as_text=True)).group(1)
    assert key not in b.get("/settings").get_data(as_text=True)
    assert db.value("SELECT key_prefix FROM api_keys WHERE household_id=2") == key[:10]
    assert not db.all("SELECT 1 FROM api_keys WHERE key_hash=?", key)

    hdr = {"X-StashGrid-Key": key}
    scan = lambda bc, room, shelf: p.post("/api/scan", data={"barcode": bc, "room_id": setup[room], "shelf_id": setup[shelf]}, headers=hdr)
    assert scan("BK1", "B_room", "B_shelf").status_code == 200
    assert db.value("SELECT household_id FROM inventory WHERE barcode='BK1'") == 2
    assert scan("BK2", "A_room", "A_shelf").status_code == 404
    assert p.get("/api/items", headers=hdr).status_code == 401
    assert db.value("SELECT last_used_at IS NOT NULL FROM api_keys WHERE household_id=2") == 1

    key_id = db.value("SELECT id FROM api_keys WHERE household_id=2")
    assert setup["a"].post(f"/settings/keys/{key_id}/revoke").status_code == 404
    b.post(f"/settings/keys/{key_id}/revoke")
    assert scan("BK3", "B_room", "B_shelf").status_code == 401
    hdr["X-StashGrid-Key"] = "sgk_nope"
    assert scan("Z", "A_room", "A_shelf").status_code == 401


# ── legacy import (migration 5) ─────────────────────────────────────────────

LEGACY_TOKEN = "0123456789abcdef0123456789abcdef"
LEGACY_KEY = "legacy-scanner-key-xyz"


def test_legacy_link_and_key_imported_into_home(app, tmp_path, monkeypatch):
    from stashgrid import migrations
    import stashgrid.db as db_module
    path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(config, "LEGACY_SHOPPING_TOKEN", LEGACY_TOKEN)
    monkeypatch.setattr(config, "LEGACY_SCANNER_KEY", LEGACY_KEY)
    monkeypatch.setattr(db_module, "DB", path)
    migrations.run_migrations(path)
    from conftest import DB
    db = DB(path)
    assert db.all("SELECT household_id, label, token_hash FROM share_links") == [(1, "Original shopping link", sha(LEGACY_TOKEN))]
    assert db.all("SELECT household_id, key_prefix, key_hash FROM api_keys") == [(1, LEGACY_KEY[:6], sha(LEGACY_KEY))]
    room = db.run("INSERT INTO rooms (name, household_id) VALUES ('R', 1)")
    shelf = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", room)
    p = app.test_client()
    assert p.get(f"/shop/{LEGACY_TOKEN}").status_code == 200
    assert p.post("/api/scan", data={"barcode": "K", "room_id": room, "shelf_id": shelf},
                  headers={"X-StashGrid-Key": LEGACY_KEY}).status_code == 200


def test_fresh_install_creates_no_links_or_keys(db):
    assert db.value("SELECT COUNT(*) FROM share_links") == 0
    assert db.value("SELECT COUNT(*) FROM api_keys") == 0


# ── request-log masking ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/shop/0123456789abcdef0123456789abcdef",
    "/api/public/shopping-lists/toggle/0123456789abcdef0123456789abcdef/5",
    "/reset/AbCdEfGhIjKlMnOpQrStUvWxYz_-12345",
    "/invite/AbCdEfGhIjKlMnOpQrStUvWxYz_-12345",
    "/verify/AbCdEfGhIjKlMnOpQrStUvWxYz_-12345",
])
def test_werkzeug_log_masks_tokens(app, path):
    record = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                               '127.0.0.1 - - "GET %s HTTP/1.1" 200 -', (path,), None)
    for f in logging.getLogger("werkzeug").filters:
        f.filter(record)
    msg = record.getMessage()
    token = re.search(r"[A-Za-z0-9_\-]{16,}", path).group(0)
    assert token not in msg and "<redacted>" in msg


def test_create_share_link_tokens_match_tunnel_rule(db):
    with get_db() as conn:
        tokens = {create_share_link(conn, 1, "t") for _ in range(20)}
    assert len(tokens) == 20 and all(TUNNEL_PATH.match("/shop/" + t) for t in tokens)
