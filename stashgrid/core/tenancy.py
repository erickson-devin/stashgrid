"""Household (tenant) scoping helpers (SG-32).

Every room, shelf, item, shopping list and list item belongs to one household.
Routes must:
  1. read the current household with current_household_id(), and
  2. include `household_id = ?` in every query on a tenant table, or check
     ownership first with one of the require_* helpers (404 if not ours).

The database enforces the same rules with triggers (see migration 3), so a
missed check fails loudly instead of leaking across households.
"""
from flask import abort, g

# The household that existing data (and the legacy shopping link / scanner key)
# was moved into by migrations 3 and 5.
DEFAULT_HOUSEHOLD_ID = 1

ROLES = ("member", "admin", "owner")
MANAGER_ROLES = ("owner", "admin")


def current_household_id():
    """The household the current request acts on. 403 if there is none."""
    hid = g.get("household_id")
    if not hid:
        abort(403)
    return hid


def is_manager():
    return g.get("household_role") in MANAGER_ROLES


def load_membership(conn, user_id, preferred_household_id=None):
    """Return (household_id, role, household_name) for the user, preferring the
    household already in their session, else their oldest membership.
    None if the user belongs to no household."""
    rows = conn.execute(
        "SELECT m.household_id, m.role, h.name FROM household_members m "
        "JOIN households h ON h.id = m.household_id "
        "WHERE m.user_id = ? ORDER BY m.created_at, m.household_id",
        (user_id,),
    ).fetchall()
    if not rows:
        return None
    for row in rows:
        if row[0] == preferred_household_id:
            return row
    return rows[0]


def _require(conn, sql, params):
    row = conn.execute(sql, params).fetchone()
    if not row:
        abort(404)
    return row


def require_room(conn, room_id):
    return _require(conn, "SELECT id, name, is_default FROM rooms WHERE id = ? AND household_id = ?",
                    (room_id, current_household_id()))


def require_shelf(conn, shelf_id, room_id=None):
    """Shelf must be ours and, if room_id is given, belong to that room."""
    row = _require(conn, "SELECT id, room_id, shelf_number FROM shelves WHERE id = ? AND household_id = ?",
                   (shelf_id, current_household_id()))
    if room_id is not None and row[1] != room_id:
        abort(404)
    return row


def require_item(conn, item_id):
    return _require(conn, "SELECT id FROM inventory WHERE id = ? AND household_id = ?",
                    (item_id, current_household_id()))


def require_list(conn, list_id):
    return _require(conn, "SELECT id, is_default FROM shopping_lists WHERE id = ? AND household_id = ?",
                    (list_id, current_household_id()))


def require_list_item(conn, item_id):
    return _require(conn, "SELECT id, is_completed FROM shopping_list_items WHERE id = ? AND household_id = ?",
                    (item_id, current_household_id()))
