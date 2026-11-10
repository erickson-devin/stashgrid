"""CSRF protection (SG-04) and security headers (SG-05)."""
from conftest import PASSWORD

from stashgrid.core.sharing import create_api_key, create_share_link
from stashgrid.db import get_db

HTML = {"Accept": "text/html,application/xhtml+xml"}


def test_csrf_enabled(app):
    assert app.config.get("WTF_CSRF_ENABLED", True) is True


def test_login_needs_token(admin_id, client):
    r = client.post("/login", data={"username": "admin", "password": PASSWORD}, csrf=False)
    assert r.status_code == 400
    assert "/login" in client.get("/").headers["Location"]
    assert b'name="csrf_token"' in client.get("/login").data


def test_password_change_needs_token(client, db):
    client.post("/login", data={"username": "admin", "password": "admin"})
    data = {"current_password": "admin", "new_password": "GoodPass123", "confirm_password": "GoodPass123"}
    client.post("/account/password", data=data, csrf=False)
    assert db.value("SELECT must_change_password FROM users WHERE username='admin'") == 1
    client.post("/account/password", data=data)
    assert db.value("SELECT must_change_password FROM users WHERE username='admin'") == 0


def test_pages_render_tokens(admin, db, home):
    html = admin.get("/").get_data(as_text=True)
    assert 'name="csrf-token"' in html
    assert html.count('name="csrf_token"') >= 2
    db.run("INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity) VALUES ('ABC','Thing',?,?,3)",
           home["room"], home["shelf"])
    cards = admin.get(f"/api/items?room_id={home['room']}").get_json()["html"]
    assert 'name="csrf_token"' in cards


def test_form_post_without_token_is_refused(admin, db):
    r = admin.post("/api/rooms", data={"name": "Garage", "shelf_count": "2"}, headers=HTML, csrf=False)
    assert r.status_code == 302 and not db.all("SELECT 1 FROM rooms WHERE name='Garage'")
    assert b"SECURITY TOKEN EXPIRED" in admin.get("/").data
    admin.post("/api/rooms", data={"name": "Garage", "shelf_count": "2"})
    assert db.all("SELECT 1 FROM rooms WHERE name='Garage'")


def test_item_delete_needs_token(admin, db, home):
    item = db.run("INSERT INTO inventory (barcode, name, room_id, shelf_id) VALUES ('ABC','Thing',?,?)",
                  home["room"], home["shelf"])
    admin.post(f"/delete/{item}", csrf=False)
    assert db.value("SELECT is_active FROM inventory WHERE id=?", item) == 1
    admin.post(f"/delete/{item}")
    assert db.value("SELECT is_active FROM inventory WHERE id=?", item) == 0


def test_fetch_post_tokens(app, admin, db):
    r = admin.post("/api/shopping-lists/create", data={"name": "A"}, csrf=False)
    assert r.status_code == 400 and r.is_json
    assert admin.post("/api/shopping-lists/create", data={"name": "B"}).status_code == 200
    r = admin.post("/api/shopping-lists/create", data={"name": "C"}, headers={"X-CSRFToken": "garbage"})
    assert r.status_code == 400
    other_token = app.test_client().csrf_token()
    r = admin.post("/api/shopping-lists/create", data={"name": "Cross"}, headers={"X-CSRFToken": other_token})
    assert r.status_code == 400 and not db.all("SELECT 1 FROM shopping_lists WHERE name='Cross'")
    assert admin.get("/api/shopping-lists").status_code == 200


def test_scan_session_needs_token_but_key_does_not(app, admin, home):
    scan = {"barcode": "X1", "shelf_id": home["shelf"], "room_id": home["room"]}
    assert admin.post("/api/scan", data=scan, csrf=False).status_code == 400
    assert admin.post("/api/scan", data=scan).status_code == 200
    with get_db() as conn:
        key = create_api_key(conn, 1, "test")
    k = app.test_client()
    assert k.post("/api/scan", data=scan, headers={"X-StashGrid-Key": key}, csrf=False).status_code == 200
    assert k.post("/api/scan", data=scan, csrf=False).status_code == 401


def test_public_token_routes_are_exempt(client, db, home):
    with get_db() as conn:
        token = create_share_link(conn, 1, "test")
    r = client.post(f"/api/public/shopping-lists/add/{token}", data={"list_id": home["list"], "name": "Milk"}, csrf=False)
    assert r.status_code == 200
    item = db.value("SELECT id FROM shopping_list_items WHERE name='Milk'")
    assert client.post(f"/api/public/shopping-lists/toggle/{token}/{item}", csrf=False).status_code == 200
    assert b"csrf-token" not in client.get(f"/shop/{token}").data


def test_after_logout_auth_comes_first(admin):
    token = admin.csrf_token()
    admin.get("/logout")
    r = admin.post("/api/shopping-lists/create", data={"name": "X"}, headers={"X-CSRFToken": token})
    assert r.status_code == 401


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in (r.headers.get("Content-Security-Policy-Report-Only") or "")
