"""Single-use, expiring tokens for emailed links (SG-33).

The raw token only ever exists in the email; the database stores its SHA-256
hash. A token is valid while unused and unexpired, and is consumed atomically
(two clicks on the same link can't both succeed).
"""
import hashlib
import secrets
from datetime import timedelta

from stashgrid import db

VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(hours=1)
INVITE_TTL = timedelta(days=7)


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def create(conn, purpose, ttl, *, user_id=None, email=None, household_id=None, role=None, created_by=None):
    raw = secrets.token_urlsafe(32)
    expires = db.now_plus(seconds=ttl.total_seconds())
    conn.execute(
        "INSERT INTO auth_tokens (purpose, token_hash, user_id, email, household_id, role, created_by, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (purpose, _hash(raw), user_id, email, household_id, role, created_by, expires),
    )
    return raw


def find(conn, raw, purpose):
    """Return the token row as a dict if it's valid (right purpose, unused, unexpired)."""
    if not raw or len(raw) > 200:
        return None
    row = conn.execute(
        "SELECT id, user_id, email, household_id, role, created_by, expires_at FROM auth_tokens "
        "WHERE token_hash = ? AND purpose = ? AND used_at IS NULL AND expires_at > ?",
        (_hash(raw), purpose, db.now()),
    ).fetchone()
    if not row:
        return None
    keys = ("id", "user_id", "email", "household_id", "role", "created_by", "expires_at")
    return dict(zip(keys, row))


def consume(conn, token_id):
    """Mark a token used. False if it was already used (lost a race)."""
    cur = conn.execute(
        "UPDATE auth_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL", (db.now(), token_id)
    )
    return cur.rowcount == 1


def revoke(conn, purpose, *, user_id=None, email=None, household_id=None):
    """Invalidate outstanding tokens, e.g. older reset links once a new one is sent."""
    sql = "UPDATE auth_tokens SET used_at = ? WHERE purpose = ? AND used_at IS NULL"
    params = [db.now(), purpose]
    if user_id is not None:
        sql += " AND user_id = ?"; params.append(user_id)
    if email is not None:
        sql += " AND email = ?"; params.append(email)
    if household_id is not None:
        sql += " AND household_id = ?"; params.append(household_id)
    conn.execute(sql, params)
