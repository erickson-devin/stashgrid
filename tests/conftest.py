"""Shared pytest fixtures (SG-25).

Every test gets its own empty, fully migrated database in a temp folder, so
tests can run in any order. The Flask app itself is built once per session.

    def test_something(app, client, db, make_user, login):
        uid = make_user("bob", role="member")
        c = login("bob")
        assert c.get("/").status_code == 200

The test client adds a valid CSRF token to every POST/PUT/PATCH/DELETE, the
way the real pages do. Pass csrf=False to send a request without one.
"""
import hashlib
import os
import re
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# stashgrid.config reads the environment at import time, so point it at a
# throwaway folder before anything imports the app.
_SESSION_DIR = tempfile.mkdtemp(prefix="stashgrid-tests-")
os.environ["STASHGRID_LOG_DIR"] = os.path.join(_SESSION_DIR, "logs")
os.environ["STASHGRID_DB_PATH"] = os.path.join(_SESSION_DIR, "boot.db")
os.environ["STASHGRID_BACKUP_DIR"] = os.path.join(_SESSION_DIR, "backups")
for _name in ("STASHGRID_SCANNER_KEY", "STASHGRID_SIGNUP_ENABLED", "STASHGRID_PUBLIC_BASE_URL",
              "STASHGRID_MAIL_BACKEND", "STASHGRID_SECRET_KEY"):
    os.environ.pop(_name, None)

from flask.testing import FlaskClient                     # noqa: E402
from itsdangerous import URLSafeTimedSerializer           # noqa: E402

from stashgrid import config                              # noqa: E402

# A developer's real shopping_token.txt must never leak into test databases.
config.LEGACY_SHOPPING_TOKEN = None
config.LEGACY_SCANNER_KEY = ""

import stashgrid                                          # noqa: E402
import stashgrid.db as db_module                          # noqa: E402
import stashgrid.migrations as migrations                 # noqa: E402
import stashgrid.core.accounts as accounts                # noqa: E402
import stashgrid.core.security as security                # noqa: E402
import stashgrid.inventory.routes as inv_routes           # noqa: E402
from stashgrid.extensions import password_hasher         # noqa: E402
from stashgrid.services.mail import OUTBOX                # noqa: E402

PASSWORD = "Strong-Test-Pass-1"
_PASSWORD_HASH = password_hasher.hash(PASSWORD)           # Argon2 is slow; hash once


class CsrfClient(FlaskClient):
    """Test client that sends the session's CSRF token like the real UI does."""

    def csrf_token(self):
        with self.session_transaction() as sess:
            raw = sess.get("csrf_token")
            if not raw:
                raw = sess["csrf_token"] = hashlib.sha1(os.urandom(64)).hexdigest()
        s = URLSafeTimedSerializer(self.application.secret_key, salt="wtf-csrf-token")
        return s.dumps(raw)

    def open(self, *args, csrf=True, **kwargs):
        method = (kwargs.get("method") or "GET").upper()
        if csrf and method in ("POST", "PUT", "PATCH", "DELETE"):
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("X-CSRFToken", self.csrf_token())
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)


@pytest.fixture(scope="session")
def _app():
    app = stashgrid.create_app()
    app.config.update(TESTING=True)
    app.test_client_class = CsrfClient
    return app


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "inventory.db")


@pytest.fixture
def app(_app, tmp_path, db_path, monkeypatch):
    """The app, pointed at a fresh migrated database for this test."""
    backups = str(tmp_path / "backups")
    photos = tmp_path / "photos"
    photos.mkdir()
    for module, name, value in [
        (db_module, "DB", db_path),
        (migrations, "DB", db_path),
        (migrations, "BACKUP_DIR", backups),
        (config, "DB", db_path),
        (config, "BACKUP_DIR", backups),
        (inv_routes, "PHOTOS_DIR", str(photos)),
        (inv_routes, "SCANNER_STATE_FILE", str(tmp_path / "scanner_state.json")),
        (config, "SIGNUP_ENABLED", False),
        (config, "PUBLIC_BASE_URL", ""),
        (config, "MAIL_BACKEND", "console"),
        (config, "LEGACY_SHOPPING_TOKEN", None),
        (config, "LEGACY_SCANNER_KEY", ""),
    ]:
        monkeypatch.setattr(module, name, value)
    # No network: barcode lookups return a fixed name.
    monkeypatch.setattr(inv_routes, "_web_lookup_name", lambda barcode: "Test Item")

    security._login_failures.clear()
    for throttle in (accounts.signup_ip_throttle, accounts.email_ip_throttle,
                     accounts.email_addr_throttle, accounts.invite_throttle):
        throttle.reset()
    OUTBOX.clear()

    migrations.run_migrations()
    return _app


@pytest.fixture
def client(app):
    return app.test_client()


class DB:
    """Direct SQL access to the test database (bypasses the app)."""

    def __init__(self, path):
        self.path = path

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def all(self, sql, *args):
        conn = self._conn()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def one(self, sql, *args):
        rows = self.all(sql, *args)
        return rows[0] if rows else None

    def value(self, sql, *args):
        row = self.one(sql, *args)
        return row[0] if row else None

    def run(self, sql, *args):
        """Execute and commit; returns lastrowid."""
        conn = self._conn()
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


@pytest.fixture
def db(app, db_path):
    return DB(db_path)


@pytest.fixture
def make_household(db):
    def make(name="Other"):
        return db.run("INSERT INTO households (name) VALUES (?)", name)
    return make


@pytest.fixture
def make_user(db):
    """Create a user who can log in with PASSWORD. household=None for no membership."""
    def make(username, *, role="member", household=1, email=None, must_change=False):
        uid = db.run(
            "INSERT INTO users (username, password_hash, role, must_change_password, email, email_verified_at) "
            "VALUES (?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END)",
            username, _PASSWORD_HASH, "admin" if role in ("owner", "admin") else "user",
            int(must_change), email, email,
        )
        if household is not None:
            db.run("INSERT INTO household_members (household_id, user_id, role) VALUES (?, ?, ?)",
                   household, uid, role)
        return uid
    return make


@pytest.fixture
def admin_id(db):
    """The seeded 'admin' (owner of household 1), with PASSWORD and no forced change."""
    db.run("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = 'admin'",
           _PASSWORD_HASH)
    return db.value("SELECT id FROM users WHERE username = 'admin'")


@pytest.fixture
def login(app):
    """login(username, password=PASSWORD) -> a logged-in client."""
    def do(username, password=PASSWORD, *, expect=302):
        c = app.test_client()
        r = c.post("/login", data={"username": username, "password": password})
        assert r.status_code == expect, (username, r.status_code)
        return c
    return do


@pytest.fixture
def admin(admin_id, login):
    return login("admin")


@pytest.fixture
def home(db):
    """A room with two shelves and a default shopping list in household 1."""
    room = db.run("INSERT INTO rooms (name, is_default, household_id) VALUES ('Basement', 1, 1)")
    shelf1 = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 1)", room)
    shelf2 = db.run("INSERT INTO shelves (room_id, shelf_number) VALUES (?, 2)", room)
    lst = db.run("INSERT INTO shopping_lists (name, store_name, is_default, household_id) "
                 "VALUES ('Costco', 'Costco', 1, 1)")
    return {"room": room, "shelf": shelf1, "shelf2": shelf2, "list": lst}


def last_email_link(to, kind):
    """Path of the newest /<kind>/<token> link emailed to `to`, or None."""
    for msg in reversed(OUTBOX):
        if msg["to"] == to:
            m = re.search(r"https?://[^/\s]+(/" + kind + r"/[A-Za-z0-9_\-]+)", msg["body"])
            if m:
                return m.group(1)
    return None
