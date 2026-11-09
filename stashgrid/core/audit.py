"""Audit log helper (moved from app.py, SG-06)."""
import sqlite3

from stashgrid.config import DB, logger


def log_audit(user_id, action, target_type, target_id, conn=None):
    """Insert a row into audit_logs. Never raises — a logging failure must not
    crash a route or corrupt a transaction.
    Pass `conn` when the caller holds an open write transaction; opening a
    second connection then fails with 'database is locked'."""
    sql = ("INSERT INTO audit_logs (user_id, action, target_type, target_id) "
           "VALUES (?, ?, ?, ?)")
    params = (user_id, action, target_type, target_id)
    try:
        if conn is not None:
            conn.execute(sql, params)
            return
        with sqlite3.connect(DB) as own_conn:
            own_conn.execute(sql, params)
    except Exception as e:
        logger.warning("log_audit failed: %s", e)
