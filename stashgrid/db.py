"""Database access for SQLite and Postgres (SG-07, SG-34).

Every connection goes through get_db(), which commits, rolls back and closes
for you:

    with get_db() as conn:
        row = conn.execute("SELECT name FROM rooms WHERE id = ?", (room_id,)).fetchone()

The backend comes from STASHGRID_DATABASE_URL:
    (unset) or sqlite:///path   SQLite — the self-hosted Pi
    postgresql://user@host/db   Postgres — the hosted service

App code writes one dialect of SQL that runs on both:
  - `?` placeholders (translated to %s for Postgres);
  - no SQLite-only functions: pass timestamps as parameters (now(),
    now_plus()) instead of datetime('now'); use ON CONFLICT DO NOTHING
    instead of INSERT OR IGNORE; LOWER(x) instead of COLLATE NOCASE;
    LOWER(col) LIKE LOWER(?) for case-insensitive search;
  - cursor.lastrowid works on both (Postgres INSERTs get RETURNING id).

The schema is managed by numbered migrations in stashgrid/migrations.py.
init_db() runs any that are pending at startup.
"""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from stashgrid import config
from stashgrid.config import logger

try:                                    # optional: only the hosted service needs it
    import psycopg
    _PG_INTEGRITY = (psycopg.errors.IntegrityError,)
except ImportError:                     # pragma: no cover - depends on install
    psycopg = None
    _PG_INTEGRITY = ()

BUSY_TIMEOUT_MS = 5000
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"   # how every timestamp column is stored (UTC)

# SQLite file. Tests and the migration runner patch this module attribute.
DB = config.DB

# Catch database constraint violations from either backend:
#     except IntegrityError: ...
IntegrityError = (sqlite3.IntegrityError,) + _PG_INTEGRITY

# Tables without an `id` column: INSERTs into them get no RETURNING clause.
_NO_ID_TABLES = {"household_members", "schema_version"}


def backend():
    """'postgres' or 'sqlite', from STASHGRID_DATABASE_URL."""
    url = config.DATABASE_URL
    return "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"


def now():
    """Current UTC time in the stored timestamp format. Use as a query
    parameter instead of SQLite's datetime('now')."""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def now_plus(**delta):
    """UTC time offset by a timedelta, e.g. now_plus(days=7) or now_plus(minutes=-1)."""
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime(TIMESTAMP_FORMAT)


@lru_cache(maxsize=1024)
def _to_postgres(sql):
    """Translate `?` placeholders to %s (escaping literal %), skipping quoted
    strings, and add RETURNING id to INSERTs so lastrowid works."""
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    text = "".join(out)
    m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", text, re.I)
    wants_id = bool(m) and m.group(1).lower() not in _NO_ID_TABLES and not re.search(r"\bRETURNING\b", text, re.I)
    if wants_id:
        text = text.rstrip().rstrip(";") + " RETURNING id"
    return text, wants_id


class Cursor:
    """The parts of a DB-API cursor the app uses, on either backend."""

    def __init__(self, raw, lastrowid=None, rows=None):
        self._raw = raw
        self.lastrowid = lastrowid
        self._rows = rows          # pre-fetched rows (Postgres INSERT ... RETURNING)

    @property
    def rowcount(self):
        return self._raw.rowcount

    def fetchone(self):
        if self._rows is not None:
            return self._rows.pop(0) if self._rows else None
        return self._raw.fetchone()

    def fetchall(self):
        if self._rows is not None:
            rows, self._rows = self._rows, []
            return rows
        return self._raw.fetchall()

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """A connection that takes StashGrid's portable SQL on either backend."""

    def __init__(self, raw, dialect):
        self.raw = raw
        self.dialect = dialect

    def execute(self, sql, params=()):
        if self.dialect == "postgres":
            text, wants_id = _to_postgres(sql)
            cur = self.raw.execute(text, tuple(params))
            if wants_id:
                row = cur.fetchone()
                return Cursor(cur, lastrowid=row[0] if row else None, rows=[])
            return Cursor(cur)
        cur = self.raw.execute(sql, params)
        return Cursor(cur, lastrowid=cur.lastrowid)

    @contextmanager
    def savepoint(self):
        """Let a statement fail without aborting the surrounding transaction
        (Postgres aborts the whole transaction on any error)."""
        if self.dialect == "postgres":
            with self.raw.transaction():
                yield
        else:
            yield

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def sqlite_connect(path=None):
    """Raw sqlite3 connection with StashGrid's PRAGMAs (foreign keys on,
    busy timeout). For the migration runner, backups and scanner-side code."""
    conn = sqlite3.connect(path or DB, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def connect():
    """Open a Connection to the configured database."""
    if backend() == "postgres":
        if psycopg is None:
            raise RuntimeError("STASHGRID_DATABASE_URL is Postgres but psycopg isn't installed: "
                               "pip install -r requirements-postgres.txt")
        return Connection(psycopg.connect(config.DATABASE_URL), "postgres")
    return Connection(sqlite_connect(), "sqlite")


@contextmanager
def get_db():
    """Yield a connection; commit on success, roll back on error, always close."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Bring the database schema up to date. Safe to call on every startup."""
    from stashgrid.migrations import run_migrations

    if backend() == "sqlite":
        os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    logger.info("DB: using %s", describe())
    applied = run_migrations()
    if applied:
        logger.info("DB: applied migrations %s", ", ".join(str(v) for v in applied))


def describe():
    """Where the data lives, for log lines (never includes a password)."""
    if backend() == "postgres":
        return "postgres " + re.sub(r"//[^@/]*@", "//", config.DATABASE_URL)
    return "sqlite " + os.path.abspath(DB)
