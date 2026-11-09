"""Inventory module: rooms, shelves, items, scanning and shopping lists."""
from flask import Blueprint

bp = Blueprint("inventory", __name__)

from stashgrid.inventory import routes, shopping  # noqa: E402,F401  (registers routes)
