"""Email accounts (SG-33): sign-up, verification, password reset, invites,
account page, household switching."""
import re

import pytest

from conftest import PASSWORD, last_email_link

from stashgrid import config
from stashgrid.services.mail import OUTBOX


@pytest.fixture
def signup(app, monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_ENABLED", True)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://app.example.test")


def sign_up(c, email="ann@example.com", name="Ann Smith", household="Smith House", password=PASSWORD, confirm=None):
    return c.post("/signup", data={"display_name": name, "household_name": household, "email": email,
                                   "password": password, "confirm_password": confirm or password})


@pytest.fixture
def ann(app, signup, db):
    """A verified sign-up: Ann owns 'Smith House'."""
    sign_up(app.test_client(), email="Ann@Example.com")
    app.test_client().get(last_email_link("ann@example.com", "verify"))
    uid = db.value("SELECT id FROM users WHERE email='ann@example.com'")
    hid = db.value("SELECT household_id FROM household_members WHERE user_id=?", uid)
    return {"id": uid, "household": hid}


# ── sign-up ─────────────────────────────────────────────────────────────────

def test_signup_off_by_default(client):
    assert client.get("/signup").status_code == 404
    page = client.get("/login").data
    assert b"Create an account" not in page and b"Forgot password?" in page


def test_signup_on(client, signup):
    assert client.get("/signup").status_code == 200
    assert b"Create an account" in client.get("/login").data


def test_signup_without_csrf_creates_nothing(client, signup, db):
    r = client.post("/signup", data={"display_name": "Ann", "email": "ann@example.com", "password": PASSWORD,
                                     "confirm_password": PASSWORD}, headers={"Accept": "text/html"}, csrf=False)
    assert r.status_code == 302 and not db.all("SELECT 1 FROM users WHERE email='ann@example.com'")


@pytest.mark.parametrize("kwargs,message", [
    ({"email": "not-an-email"}, b"VALID EMAIL"),
    ({"password": "short"}, b"TOO SHORT"),
    ({"confirm": PASSWORD + "x"}, b"DO NOT MATCH"),
    ({"name": ""}, b"ENTER YOUR NAME"),
])
def test_signup_validation(client, signup, db, kwargs, message):
    before = db.value("SELECT COUNT(*) FROM users")
    assert message in sign_up(client, **kwargs).data
    assert db.value("SELECT COUNT(*) FROM users") == before


def test_signup_verify_login(app, signup, db):
    c = app.test_client()
    r = sign_up(c, email="Ann@Example.com")
    assert r.status_code == 200 and b"Check Your Email" in r.data
    row = db.one("SELECT id, username, email_verified_at FROM users WHERE email='ann@example.com'")
    assert row and row[2] is None
    assert db.one("SELECT h.name, m.role FROM household_members m JOIN households h ON h.id=m.household_id "
                  "WHERE m.user_id=?", row[0]) == ("Smith House", "owner")

    verify = last_email_link("ann@example.com", "verify")
    assert verify and not db.all("SELECT 1 FROM auth_tokens WHERE token_hash=?", verify.rsplit("/", 1)[1])
    r = c.post("/login", data={"username": "ann@example.com", "password": PASSWORD})
    assert r.status_code == 403 and b"NOT VERIFIED" in r.data and b"Resend" in r.data

    r = c.get(verify)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert c.get(verify).status_code == 400, "single use"

    a = app.test_client()
    r = a.post("/login", data={"username": "ANN@example.com", "password": PASSWORD})
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert "Ann Smith @ Smith House" in a.get("/").get_data(as_text=True)
    assert re.findall(r'class="user-name">([^<]+)<', a.get("/admin/users").get_data(as_text=True)) == [row[1]]
    assert app.test_client().post("/login", data={"username": row[1], "password": PASSWORD}).status_code == 302


def test_duplicate_signup_does_not_reveal_account(app, ann, db):
    users, households = db.value("SELECT COUNT(*) FROM users"), db.value("SELECT COUNT(*) FROM households")
    r = sign_up(app.test_client(), name="Mallory")
    assert r.status_code == 200 and b"Check Your Email" in r.data
    assert db.value("SELECT COUNT(*) FROM users") == users
    assert db.value("SELECT COUNT(*) FROM households") == households
    assert OUTBOX[-1]["to"] == "ann@example.com" and "already has an account" in OUTBOX[-1]["body"]


# ── forgot / reset ──────────────────────────────────────────────────────────

def test_forgot_password_flow(app, ann, db, login):
    a = login("ann@example.com")
    f = app.test_client()
    n = len(OUTBOX)
    unknown = f.post("/forgot", data={"email": "nobody@example.com"}).get_data(as_text=True)
    assert len(OUTBOX) == n
    known = f.post("/forgot", data={"email": "ann@example.com"}).get_data(as_text=True)
    strip = lambda s: re.sub(r'value="[^"]+"|content="[^"]+"', "", s)
    assert strip(unknown) == strip(known)

    first = last_email_link("ann@example.com", "reset")
    f.post("/forgot", data={"email": "ann@example.com"})
    second = last_email_link("ann@example.com", "reset")
    assert second and second != first
    assert f.get(first).status_code == 400, "older link revoked"
    assert f.get(second).status_code == 200
    assert b"DO NOT MATCH" in f.post(second, data={"new_password": "New-Strong-Pass-2", "confirm_password": "no"}).data
    r = f.post(second, data={"new_password": "password", "confirm_password": "password"})
    assert b"TOO WEAK" in r.data or b"TOO SHORT" in r.data
    r = f.post(second, data={"new_password": "New-Strong-Pass-2", "confirm_password": "New-Strong-Pass-2"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert f.get(second).status_code == 400, "single use"
    assert a.get("/").status_code == 302, "old session ended"
    login("ann@example.com", expect=200)
    login("ann@example.com", "New-Strong-Pass-2")


def test_expired_reset_link(app, ann, db):
    f = app.test_client()
    f.post("/forgot", data={"email": "ann@example.com"})
    link = last_email_link("ann@example.com", "reset")
    db.run("UPDATE auth_tokens SET expires_at = datetime('now', '-1 minute') WHERE purpose='reset_password'")
    assert f.get(link).status_code == 400


def test_forgot_is_rate_limited_per_address(app, ann):
    f = app.test_client()
    codes = [f.post("/forgot", data={"email": "ann@example.com"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


# ── invites ─────────────────────────────────────────────────────────────────

def test_invite_new_person(app, ann, db, login):
    a = login("ann@example.com")
    r = a.post("/admin/users/invite", data={"email": "bob@example.com", "role": "member"})
    invite = last_email_link("bob@example.com", "invite")
    assert r.status_code == 302 and invite
    assert b"bob@example.com" in a.get("/admin/users").data
    assert 'Ann Smith invited you to join the household "Smith House"' in OUTBOX[-1]["body"]

    b = app.test_client()
    page = b.get(invite)
    assert page.status_code == 200 and b"Smith House" in page.data
    r = b.post(invite, data={"action": "create", "display_name": "Bob", "password": "short", "confirm_password": "short"})
    assert b"TOO SHORT" in r.data
    r = b.post(invite, data={"action": "create", "display_name": "Bob",
                             "password": "Bob-Strong-Pass-3", "confirm_password": "Bob-Strong-Pass-3"})
    assert r.status_code == 302 and r.headers["Location"] == "/"
    bob = db.one("SELECT id, email_verified_at FROM users WHERE email='bob@example.com'")
    assert bob[1] is not None
    assert db.all("SELECT household_id, role FROM household_members WHERE user_id=?", bob[0]) == [(ann["household"], "member")]
    assert "@ Smith House" in b.get("/").get_data(as_text=True)
    assert app.test_client().get(invite).status_code == 400, "single use"
    assert b.post("/admin/users/invite", data={"email": "x@example.com"}).status_code == 403


def test_invite_existing_account_and_switch(app, ann, admin_id, db, login):
    db.run("UPDATE users SET email='admin@example.com', email_verified_at=datetime('now') WHERE id=?", admin_id)
    a = login("ann@example.com")
    a.post("/admin/users/invite", data={"email": "admin@example.com", "role": "admin"})
    invite = last_email_link("admin@example.com", "invite")
    page = app.test_client().get(invite)
    assert b"Log in to accept" in page.data and b"Create Account" not in page.data
    r = app.test_client().post(invite, data={"action": "create", "display_name": "x",
                                             "password": PASSWORD, "confirm_password": PASSWORD})
    assert b"ALREADY USES THIS EMAIL" in r.data

    ad = login("admin")
    assert ad.post(invite, data={"action": "join"}).status_code == 302
    assert sorted(db.all("SELECT household_id, role FROM household_members WHERE user_id=?", admin_id)) == \
        sorted([(1, "owner"), (ann["household"], "admin")])
    assert "@ Smith House" in ad.get("/").get_data(as_text=True)
    acct = ad.get("/account").get_data(as_text=True)
    assert "Home" in acct and "Smith House" in acct
    r = ad.post("/account/household", data={"household_id": 1})
    assert r.status_code == 302 and "@ Home" in ad.get("/").get_data(as_text=True)
    other = db.run("INSERT INTO households (name) VALUES ('Not Mine')")
    assert ad.post("/account/household", data={"household_id": other}).status_code == 404


def test_invite_rules(app, ann, admin_id, db, make_user, login):
    make_user("bobby", role="admin", household=ann["household"], email="bob@example.com")
    b = login("bobby")
    b.post("/admin/users/invite", data={"email": "carl@example.com", "role": "owner"})
    assert last_email_link("carl@example.com", "invite") is None, "an admin can't invite an owner"

    a = login("ann@example.com")
    a.post("/admin/users/invite", data={"email": "ann@example.com", "role": "member"})
    assert last_email_link("ann@example.com", "invite") is None, "already a member"

    a.post("/admin/users/invite", data={"email": "dan@example.com", "role": "member"})
    dan = last_email_link("dan@example.com", "invite")
    inv_id = db.value("SELECT id FROM auth_tokens WHERE purpose='invite' AND email='dan@example.com'")
    assert login("admin").post(f"/admin/invites/{inv_id}/revoke").status_code == 404
    a.post(f"/admin/invites/{inv_id}/revoke")
    assert app.test_client().get(dan).status_code == 400


# ── account page ────────────────────────────────────────────────────────────

def test_change_email_needs_confirmation(app, ann, admin_id, db, login, signup):
    ad = login("admin")
    r = ad.post("/account", data={"action": "email", "email": "boss@example.com", "current_password": "wrong"})
    assert b"CURRENT PASSWORD INCORRECT" in r.data
    ad.post("/account", data={"action": "email", "email": "boss@example.com", "current_password": PASSWORD})
    assert db.value("SELECT email FROM users WHERE id=?", admin_id) is None
    ad.get(last_email_link("boss@example.com", "verify"))
    assert db.one("SELECT email, email_verified_at IS NOT NULL FROM users WHERE id=?", admin_id) == ("boss@example.com", 1)

    ad.post("/account", data={"action": "email", "email": "ann@example.com", "current_password": PASSWORD})
    assert ad.get(last_email_link("ann@example.com", "verify")).status_code == 409

    ad.post("/account", data={"action": "name", "display_name": "The Boss"})
    assert "The Boss @" in ad.get("/").get_data(as_text=True)


def test_resend_verification(app, signup):
    sign_up(app.test_client(), email="eve@example.com", name="Eve")
    v1 = last_email_link("eve@example.com", "verify")
    app.test_client().post("/verify/resend", data={"email": "eve@example.com"})
    v2 = last_email_link("eve@example.com", "verify")
    assert v2 != v1 and app.test_client().get(v1).status_code == 400


def test_resend_for_verified_email_sends_nothing(app, ann):
    n = len(OUTBOX)
    app.test_client().post("/verify/resend", data={"email": "ann@example.com"})
    assert len(OUTBOX) == n


def test_username_only_accounts_still_log_in(make_user, login):
    make_user("legacy")
    login("legacy")
