"""Library module (books). Deferred: ported from the ShelfGrid branch in SG-08 (#8).

Registered now so the URL space and module layout exist; it has no routes yet.
"""
from flask import Blueprint

bp = Blueprint("library", __name__, url_prefix="/library")
