"""Audit log helper (moved from app.py, SG-06)."""
from flask import g, has_app_context

from stashgrid.db import get_db
from stashgrid.config import logger


def log_audit(user_id, action, target_type, target_id, conn=None, household_id=None):
    """Insert a row into audit_logs. Never raises — a logging failure must not
    crash a route or corrupt a transaction.
    Pass `conn` when the caller holds an open write transaction; opening a
    second connection then fails with 'database is locked'.
    The row is tagged with the current request's household (SG-32) unless one
    is passed explicitly."""
    if household_id is None and has_app_context():
        household_id = g.get("household_id")
    sql = ("INSERT INTO audit_logs (user_id, action, target_type, target_id, household_id) "
           "VALUES (?, ?, ?, ?, ?)")
    params = (user_id, action, target_type, target_id, household_id)
    try:
        if conn is not None:
            conn.execute(sql, params)
            return
        with get_db() as own_conn:
            own_conn.execute(sql, params)
    except Exception as e:
        logger.warning("log_audit failed: %s", e)
