"""Per-household share links and scanner API keys (SG-35).

Share links open a household's shopping lists without logging in (all lists,
or a single list). Scanner keys let a headless client (the USB scanner, a
Pi hub) call /api/scan for one household. Only a SHA-256 hash of each secret
is stored; the secret is shown once when it's created.

Share-link tokens stay 32 lowercase hex characters: the Pi's Cloudflare
tunnel only forwards paths matching /shop/[a-f0-9]{32}.
"""
import hashlib
import secrets

from stashgrid import db

KEY_PREFIX = "sgk_"


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


# ── share links ──────────────────────────────────────────────────────────────

def create_share_link(conn, household_id, label, *, list_id=None, expires_days=None, created_by=None):
    token = secrets.token_hex(16)
    expires_at = db.now_plus(days=int(expires_days)) if expires_days else None
    conn.execute(
        "INSERT INTO share_links (household_id, list_id, label, token_hash, created_by, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (household_id, list_id, label, _sha(token), created_by, expires_at),
    )
    return token


def resolve_share_token(conn, token):
    """(household_id, list_id or None) for a live link, else None.
    Updates last_used_at at most once a minute."""
    if not token or len(token) > 128:
        return None
    row = conn.execute(
        "SELECT id, household_id, list_id FROM share_links WHERE token_hash = ? AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (_sha(token), db.now()),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE share_links SET last_used_at = ? WHERE id = ? "
        "AND (last_used_at IS NULL OR last_used_at < ?)", (db.now(), row[0], db.now_plus(minutes=-1))
    )
    return row[1], row[2]


# ── scanner API keys ─────────────────────────────────────────────────────────

def create_api_key(conn, household_id, label, *, created_by=None):
    key = KEY_PREFIX + secrets.token_hex(24)
    conn.execute(
        "INSERT INTO api_keys (household_id, label, key_prefix, key_hash, created_by) VALUES (?, ?, ?, ?, ?)",
        (household_id, label, key[: len(KEY_PREFIX) + 6], _sha(key), created_by),
    )
    return key


def resolve_api_key(conn, key):
    """household_id for a live key, else None. Updates last_used_at at most once a minute."""
    if not key or len(key) > 200:
        return None
    row = conn.execute(
        "SELECT id, household_id FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (_sha(key),)
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE api_keys SET last_used_at = ? WHERE id = ? "
        "AND (last_used_at IS NULL OR last_used_at < ?)", (db.now(), row[0], db.now_plus(minutes=-1))
    )
    return row[1]
