"""StashGrid application package (SG-06).

Layout:
    stashgrid/config.py      paths, env settings, logging, shopping token
    stashgrid/extensions.py  CSRF + password hasher instances
    stashgrid/db.py          schema setup (init_db)
    stashgrid/core/          auth, sessions, users, audit, security headers, kiosk
    stashgrid/inventory/     rooms, shelves, items, scanning, shopping lists
    stashgrid/library/       books (deferred, SG-08)
    stashgrid/services/      shared services (barcode lookups)
"""
import logging
import os
import re
from datetime import timedelta

from flask import Flask

from stashgrid import config
from stashgrid.config import logger
from stashgrid.extensions import csrf

# Share-link and emailed-link tokens travel in the URL path. Mask them in the
# request log so the secrets don't end up in journald/log files (SG-35).
_SECRET_PATH = re.compile(
    r"(/shop/|/api/public/shopping-lists/(?:toggle/|create/|add/)?|/verify/|/reset/|/invite/)[A-Za-z0-9_\-]{16,}"
)


class _MaskSecretPaths(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = _SECRET_PATH.sub(r"\1<redacted>", message)
        if masked != message:
            record.msg, record.args = masked, ()
        return True


def create_app():
    """Build and configure the Flask app. Same behaviour as the old app.py."""
    werkzeug_log = logging.getLogger("werkzeug")
    if not any(isinstance(f, _MaskSecretPaths) for f in werkzeug_log.filters):
        werkzeug_log.addFilter(_MaskSecretPaths())

    app = Flask(
        __name__,
        template_folder=os.path.join(config.BASE_DIR, "templates"),
        static_folder=os.path.join(config.BASE_DIR, "static"),
    )
    # Secret key signs session cookies — override via env var in production
    app.secret_key = config.SECRET_KEY
    # Keep the kiosk and phones logged in across browser restarts.
    app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # CSRF (SG-04): checked explicitly in _csrf_check so public token routes
        # and scanner-key requests can be skipped. Tokens live as long as the
        # session, so a kiosk left open for hours can still submit forms.
        WTF_CSRF_CHECK_DEFAULT=False,
        WTF_CSRF_TIME_LIMIT=None,
    )
    csrf.init_app(app)

    from stashgrid.core import bp as core_bp
    from stashgrid.inventory import bp as inventory_bp
    from stashgrid.library import bp as library_bp
    app.register_blueprint(core_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(library_bp)

    from stashgrid.db import init_db
    init_db()
    # Shopping links are per household now (SG-35) and are no longer printed
    # to the log; owners/admins create and copy them on the Settings page.
    return app
