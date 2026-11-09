"""Core module: auth, sessions, users, audit log, security headers, kiosk."""
from flask import Blueprint

bp = Blueprint("core", __name__)

from stashgrid.core import security, routes  # noqa: E402,F401  (registers hooks and routes)
