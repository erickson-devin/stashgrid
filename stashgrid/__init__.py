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
import os
from datetime import timedelta

from flask import Flask

from stashgrid import config
from stashgrid.config import logger
from stashgrid.extensions import csrf


def create_app():
    """Build and configure the Flask app. Same behaviour as the old app.py."""
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

    # Log the public shopping URL so you always know where to send the link
    logger.info(
        "Public shopping list URL: https://stashgrid.devinerickson.com/shop/%s",
        config.SHOPPING_TOKEN
    )
    print(f"[StashGrid] Public shopping list URL: https://stashgrid.devinerickson.com/shop/{config.SHOPPING_TOKEN}")
    return app
