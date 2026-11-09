"""Database access and schema setup (SG-07).

Every connection goes through get_db(), which applies the same PRAGMAs
(foreign keys on, busy timeout) and commits/rolls back/closes for you:

    with get_db() as conn:
        conn.execute(...)

The schema itself is managed by numbered migrations in stashgrid/migrations.py.
init_db() runs any that are pending at startup, after taking a backup.
"""
import os
import sqlite3
from contextlib import contextmanager

from stashgrid.config import DB, logger

BUSY_TIMEOUT_MS = 5000


def connect(path=None):
    """Open a raw connection with StashGrid's standard PRAGMAs applied.
    Prefer get_db(); this exists for the migration runner and backups."""
    conn = sqlite3.connect(path or DB, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


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

    db_dir = os.path.dirname(os.path.abspath(DB))
    os.makedirs(db_dir, exist_ok=True)
    applied = run_migrations()
    if applied:
        logger.info("DB: applied migrations %s", ", ".join(str(v) for v in applied))
