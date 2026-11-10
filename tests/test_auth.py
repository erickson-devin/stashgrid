"""Login, the auth allow-list, forced password change, user admin, lockout
(SG-02, SG-03, SG-32 roles)."""
import pytest

from conftest import PASSWORD

import stashgrid.core.security as security


def chpw(c, current, new, confirm=None):
    return c.post("/account/password", data={
        "current_password": current, "new_password": new,
        "confirm_password": new if confirm is None else confirm})


# ── allow-list (SG-02) ──────────────────────────────────────────────────────

def test_pages_redirect_to_login(client):
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert client.get("/nope").status_code == 302


@pytest.mark.parametrize("path", ["/api/items", "/api/shopping-lists", "/api/inventory-hash",
                                  "/api/scanner-state"])
def test_api_returns_401_when_logged_out(client, path):
    assert client.get(path).status_code == 401


def test_logged_out_writes_do_nothing(client, db, home):
    item = db.run("INSERT INTO inventory (barcode, name, room_id, shelf_id, quantity) VALUES ('ABC','Thing',?,?,3)",
                  home["room"], home["shelf"])
    assert client.post(f"/delete/{item}").status_code == 302
    assert db.value("SELECT is_active FROM inventory WHERE id=?", item) == 1
    assert client.post(f"/api/rooms/{home['room']}/delete").status_code == 401
    assert db.value("SELECT COUNT(*) FROM rooms") == 1


def test_login_page_is_public(client):
    assert client.get("/login").status_code == 200


def test_open_redirect_blocked(admin_id, client):
    r = client.post("/login?next=//evil.com", data={"username": "admin", "password": PASSWORD})
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_next_is_honoured_through_login_form(admin_id, client):
    loc = client.get("/?room_id=1").headers["Location"]
    assert "next=" in loc
    r = client.post(loc, data={"username": "admin", "password": PASSWORD})
    assert r.headers["Location"] == "/?room_id=1"
    assert b'action="/login"' not in client.get("/login").data


def test_logout(admin):
    assert admin.get("/").status_code == 200
    admin.get("/logout")
    assert admin.get("/").status_code == 302


def test_system_user_cannot_log_in(app, client):
    r = client.post("/login", data={"username": "__system__", "password": ""})
    assert r.status_code == 200


def test_member_cannot_do_admin_actions(home, make_user, login):
    make_user("bob", role="member")
    b = login("bob")
    assert b.get("/").status_code == 200
    assert b.post(f"/api/rooms/{home['room']}/delete").status_code == 403
    assert b.post(f"/api/shopping-lists/{home['list']}/delete").status_code == 403
    assert b.get("/admin/users").status_code == 403
    assert b"/admin/users" not in b.get("/").data


def test_user_without_household_cannot_log_in(make_user, client):
    make_user("nohome", household=None)
    r = client.post("/login", data={"username": "nohome", "password": PASSWORD})
    assert r.status_code == 403 and b"HOUSEHOLD" in r.data


def test_removed_member_locked_out_on_next_request(db, make_user, login):
    uid = make_user("carol")
    c = login("carol")
    db.run("DELETE FROM household_members WHERE user_id=?", uid)
    assert c.get("/").status_code == 403


# ── forced password change (SG-03) ──────────────────────────────────────────

def test_fresh_install_forces_admin_password_change(client, db):
    assert db.value("SELECT must_change_password FROM users WHERE username='admin'") == 1
    r = client.post("/login", data={"username": "admin", "password": "admin"})
    assert r.status_code == 302 and "/account/password" in r.headers["Location"]
    assert "/account/password" in client.get("/").headers["Location"]
    assert client.get("/api/items").status_code == 403
    assert b"Password Change Required" in client.get("/account/password").data
    assert client.get("/logout").status_code == 302


@pytest.mark.parametrize("current,new,confirm,message", [
    ("nope", "GoodPass123", None, b"CURRENT PASSWORD INCORRECT"),
    ("admin", "GoodPass123", "Other1234", b"DO NOT MATCH"),
    ("admin", "short", None, b"TOO SHORT"),
    ("admin", "password", None, b"TOO WEAK"),
])
def test_password_rules(client, current, new, confirm, message):
    client.post("/login", data={"username": "admin", "password": "admin"})
    assert message in chpw(client, current, new, confirm).data


def test_password_change_ends_other_sessions(app, db):
    a, other = app.test_client(), app.test_client()
    a.post("/login", data={"username": "admin", "password": "admin"})
    other.post("/login", data={"username": "admin", "password": "admin"})
    r = chpw(a, "admin", "GoodPass123")
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert db.value("SELECT must_change_password FROM users WHERE username='admin'") == 0
    page = a.get("/")
    assert page.status_code == 200 and b"PASSWORD UPDATED" in page.data
    assert "/login" in other.get("/").headers["Location"]
    r = app.test_client().post("/login", data={"username": "admin", "password": "admin"})
    assert r.status_code == 200


def test_legacy_admin_admin_is_forced_to_change(admin_id, db, client):
    from stashgrid.extensions import password_hasher
    db.run("UPDATE users SET password_hash=? WHERE id=?", password_hasher.hash("admin"), admin_id)
    r = client.post("/login", data={"username": "admin", "password": "admin"})
    assert "/account/password" in r.headers["Location"]
    assert db.value("SELECT must_change_password FROM users WHERE id=?", admin_id) == 1


# ── user admin ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("username,password,role", [
    ("bob", "password", "member"),        # weak password
    ("x", "BobTemp1234", "member"),       # username too short
    ("__system2", "BobTemp1234", "member"),
    ("bob", "BobTemp1234", "superuser"),  # bad role
])
def test_create_user_validation(admin, db, username, password, role):
    admin.post("/admin/users/create", data={"username": username, "password": password, "role": role})
    assert not db.all("SELECT 1 FROM users WHERE username=?", username)


def test_create_user_joins_household_and_must_change(admin, db, login):
    admin.post("/admin/users/create", data={"username": "bob", "password": "BobTemp1234", "role": "member"})
    assert db.all("SELECT m.household_id, m.role, u.must_change_password FROM users u "
                  "JOIN household_members m ON m.user_id=u.id WHERE username='bob'") == [(1, "member", 1)]
    admin.post("/admin/users/create", data={"username": "bob", "password": "BobTemp1234", "role": "member"})
    assert b"ALREADY EXISTS" in admin.get("/admin/users").data
    assert b"__system__" not in admin.get("/admin/users").data
    b = login("bob", "BobTemp1234")
    assert "/account/password" in b.get("/").headers["Location"]


def test_admin_reset_password_ends_session(admin, db, make_user, login):
    bob = make_user("bob")
    b = login("bob")
    admin.post(f"/admin/users/{bob}/reset-password", data={"password": "short"})
    assert db.value("SELECT must_change_password FROM users WHERE id=?", bob) == 0
    admin.post(f"/admin/users/{bob}/reset-password", data={"password": "ResetTemp99"})
    assert db.value("SELECT must_change_password FROM users WHERE id=?", bob) == 1
    assert "/login" in b.get("/").headers["Location"]
    assert admin.post("/admin/users/1/reset-password", data={"password": "ResetTemp99"}).status_code == 404


def test_role_rules(admin, admin_id, db, make_user, login):
    role = lambda uid: db.value("SELECT role FROM household_members WHERE user_id=? AND household_id=1", uid)
    admin.post(f"/admin/users/{admin_id}/role", data={"role": "member"})
    assert role(admin_id) == "owner", "last owner can't be demoted"

    carol = make_user("carol", role="admin")
    c = login("carol")
    c.post("/admin/users/create", data={"username": "dave", "password": "Dave-Temp-1234", "role": "owner"})
    assert not db.all("SELECT 1 FROM users WHERE username='dave'"), "an admin can't create an owner"
    c.post(f"/admin/users/{admin_id}/role", data={"role": "member"})
    assert role(admin_id) == "owner", "an admin can't demote an owner"

    admin.post(f"/admin/users/{carol}/role", data={"role": "member"})
    assert role(carol) == "member"
    assert login("carol").get("/admin/users").status_code == 403

    admin.post(f"/admin/users/{carol}/role", data={"role": "owner"})
    r = admin.post(f"/admin/users/{admin_id}/role", data={"role": "member"})
    assert role(admin_id) == "member" and "/login" in r.headers["Location"]
    assert admin.get("/").status_code == 302


# ── lockout ─────────────────────────────────────────────────────────────────

def test_login_lockout(app, admin_id, make_user):
    make_user("bob")
    t = app.test_client()
    for i in range(5):
        t.post("/login", data={"username": "admin", "password": f"wrong{i}"})
    r = t.post("/login", data={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429 and b"TOO MANY" in r.data
    assert app.test_client().post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 302
    security._login_failures.clear()
    assert app.test_client().post("/login", data={"username": "admin", "password": PASSWORD}).status_code == 302
