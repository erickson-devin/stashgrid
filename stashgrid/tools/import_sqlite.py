"""Import a household from a self-hosted SQLite database (SG-34).

Moves one household from a Pi's inventory.db into the database this app is
configured for (normally the hosted Postgres service) as a new household:

    STASHGRID_DATABASE_URL=postgresql://... \\
        python -m stashgrid.tools.import_sqlite /path/to/inventory.db \\
        --photos-from /path/to/static/photos

What comes across: the household's members (with their password hashes, so
they keep their passwords), rooms, shelves, items, shopping lists and items,
share links and scanner keys (their links and keys keep working), and the
audit log. Every row gets a new id. Pending emailed links (verify, reset,
invite) are not copied.

Members are matched to existing accounts by verified email. A username that's
already taken gets a numeric suffix; the report lists every renamed account.

The source file is never modified: it's copied to a temp file and brought up
to the current schema there first. Everything is written in one transaction
and the row counts are checked before it commits, so a failed import leaves
the target unchanged. --dry-run does everything except the commit.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import OrderedDict

from stashgrid import config, db, migrations

TENANT_TABLES = ("rooms", "shelves", "inventory", "shopping_lists", "shopping_list_items",
                 "share_links", "api_keys", "audit_logs")


class SourceError(Exception):
    """A problem with the source data that stops the import."""


def _upgraded_copy(source):
    """Copy the source and run the SQLite migrations on the copy."""
    if not os.path.isfile(source):
        raise SourceError(f"{source}: no such file")
    tmpdir = tempfile.mkdtemp(prefix="stashgrid-import-")
    copy = os.path.join(tmpdir, "source.db")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    saved = migrations.BACKUP_DIR
    migrations.BACKUP_DIR = os.path.join(tmpdir, "backups")     # keep backups of the copy out of the way
    try:
        migrations.run_migrations(copy)
    finally:
        migrations.BACKUP_DIR = saved
    return tmpdir, copy


def _pick_household(src, wanted):
    rows = src.execute("SELECT id, name, created_at FROM households ORDER BY id").fetchall()
    if wanted is not None:
        for row in rows:
            if row["id"] == wanted:
                return row
        raise SourceError(f"household {wanted} not found; the source has: "
                           + ", ".join(f"{r['id']} ({r['name']})" for r in rows))
    if len(rows) != 1:
        raise SourceError("the source has several households; pick one with --household: "
                           + ", ".join(f"{r['id']} ({r['name']})" for r in rows))
    return rows[0]


def _free_username(conn, name):
    candidate, n = name, 1
    while conn.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def _insert(conn, table, row, columns, overrides=None):
    values = dict((c, row[c]) for c in columns)
    values.update(overrides or {})
    cols = list(values)
    return conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        tuple(values[c] for c in cols),
    ).lastrowid


def import_household(source, *, household=None, name=None, photos_from=None, photos_to=None, dry_run=False):
    """Run the import. Returns a report dict; raises SourceError on bad input."""
    photos_to = photos_to or config.PHOTOS_DIR
    tmpdir, copy = _upgraded_copy(source)
    src = sqlite3.connect(copy)
    src.row_factory = sqlite3.Row
    report = OrderedDict(source=os.path.abspath(source), target=db.describe(), dry_run=dry_run,
                         household=None, counts={}, renamed=[], merged=[], warnings=[])
    copied_photos = []
    try:
        hh = _pick_household(src, household)
        sid = hh["id"]
        db.init_db()
        with db.get_db() as conn:
            new_hid = conn.execute("INSERT INTO households (name, created_at) VALUES (?, ?)",
                                   (name or hh["name"], hh["created_at"])).lastrowid
            report["household"] = {"source_id": sid, "id": new_hid, "name": name or hh["name"]}

            # ── people ──
            users = {}
            members = src.execute(
                "SELECT u.*, m.role AS member_role, m.created_at AS member_since FROM household_members m "
                "JOIN users u ON u.id = m.user_id WHERE m.household_id = ? AND u.role != 'system' ORDER BY u.id",
                (sid,)).fetchall()
            for u in members:
                existing = None
                if u["email"] and u["email_verified_at"]:
                    existing = conn.execute("SELECT id, username FROM users WHERE email = ?", (u["email"],)).fetchone()
                if existing:
                    users[u["id"]] = existing[0]
                    report["merged"].append(f"{u['username']} -> existing account {existing[1]} ({u['email']})")
                else:
                    username = _free_username(conn, u["username"])
                    if username != u["username"]:
                        report["renamed"].append(f"{u['username']} -> {username}")
                    email = u["email"]
                    if email and conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                        report["warnings"].append(f"{u['username']}: unverified email {email} already in use; dropped")
                        email = None
                    users[u["id"]] = conn.execute(
                        "INSERT INTO users (username, password_hash, role, must_change_password, session_version, "
                        "is_superuser, email, email_verified_at, display_name) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)",
                        (username, u["password_hash"], "admin" if u["member_role"] in ("owner", "admin") else "user",
                         u["must_change_password"], email, u["email_verified_at"] if email else None,
                         u["display_name"])).lastrowid
                conn.execute("INSERT INTO household_members (household_id, user_id, role, created_at) "
                             "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                             (new_hid, users[u["id"]], u["member_role"], u["member_since"]))
            report["counts"]["members"] = len(members)
            uid = lambda old: users.get(old) if old is not None else None

            # ── places and things ──
            rooms, shelves, lists, items = {}, {}, {}, {}
            for r in src.execute("SELECT * FROM rooms WHERE household_id = ? ORDER BY id", (sid,)):
                rooms[r["id"]] = _insert(conn, "rooms", r, ("name", "is_default"), {"household_id": new_hid})
            for r in src.execute("SELECT * FROM shelves WHERE household_id = ? ORDER BY id", (sid,)):
                shelves[r["id"]] = _insert(conn, "shelves", r, ("shelf_number",),
                                           {"room_id": rooms[r["room_id"]], "household_id": new_hid})
            photo_rows = []
            for r in src.execute("SELECT * FROM inventory WHERE household_id = ? ORDER BY id", (sid,)):
                new_id = _insert(conn, "inventory", r,
                                 ("barcode", "name", "quantity", "notes", "updated_at", "is_active", "removed_at",
                                  "removed_reason", "low_stock_threshold", "preferred_store"),
                                 {"room_id": rooms.get(r["room_id"]), "shelf_id": shelves.get(r["shelf_id"]),
                                  "created_by": uid(r["created_by"]), "household_id": new_hid, "photo_path": None})
                items[r["id"]] = new_id
                if r["photo_path"]:
                    photo_rows.append((new_id, r["photo_path"]))
            for r in src.execute("SELECT * FROM shopping_lists WHERE household_id = ? ORDER BY id", (sid,)):
                lists[r["id"]] = _insert(conn, "shopping_lists", r, ("name", "store_name", "is_default"),
                                         {"household_id": new_hid})
            for r in src.execute("SELECT * FROM shopping_list_items WHERE household_id = ? ORDER BY id", (sid,)):
                _insert(conn, "shopping_list_items", r, ("barcode", "name", "requested_qty", "is_completed"),
                        {"list_id": lists[r["list_id"]], "household_id": new_hid})

            # ── links, keys, history ──
            for table, cols in (("share_links", ("label", "token_hash", "created_at", "expires_at",
                                                 "last_used_at", "revoked_at")),
                                ("api_keys", ("label", "key_prefix", "key_hash", "created_at",
                                              "last_used_at", "revoked_at"))):
                hash_col = "token_hash" if table == "share_links" else "key_hash"
                for r in src.execute(f"SELECT * FROM {table} WHERE household_id = ? ORDER BY id", (sid,)):
                    if conn.execute(f"SELECT 1 FROM {table} WHERE {hash_col} = ?", (r[hash_col],)).fetchone():
                        raise SourceError(f"{table} row {r['id']} ({r['label']}) already exists in the target; "
                                           "was this household imported before?")
                    extra = {"household_id": new_hid, "created_by": uid(r["created_by"])}
                    if table == "share_links":
                        extra["list_id"] = lists.get(r["list_id"]) if r["list_id"] is not None else None
                    _insert(conn, table, r, cols, extra)
            target_maps = {"item": items, "room": rooms, "user": users, "household": {sid: new_hid}}
            for r in src.execute("SELECT * FROM audit_logs WHERE household_id = ? ORDER BY id", (sid,)):
                mapping = target_maps.get(r["target_type"])
                target = mapping.get(r["target_id"]) if mapping is not None else r["target_id"]
                _insert(conn, "audit_logs", r, ("action", "target_type", "timestamp"),
                        {"user_id": uid(r["user_id"]) if r["user_id"] != config.SYSTEM_USER_ID else config.SYSTEM_USER_ID,
                         "target_id": target, "household_id": new_hid})

            # ── photos ──
            for new_id, old_path in photo_rows:
                if not photos_from:
                    continue
                src_file = os.path.join(photos_from, os.path.basename(old_path))
                if not os.path.isfile(src_file):
                    report["warnings"].append(f"photo missing: {src_file}")
                    continue
                filename = f"{new_id}.jpg"
                if not dry_run:
                    os.makedirs(photos_to, exist_ok=True)
                    dest = os.path.join(photos_to, filename)
                    if os.path.exists(dest):
                        raise SourceError(f"{dest} already exists")
                    shutil.copyfile(src_file, dest)
                    copied_photos.append(dest)
                conn.execute("UPDATE inventory SET photo_path = ? WHERE id = ? AND household_id = ?",
                             (f"/static/photos/{filename}", new_id, new_hid))
            if photo_rows and not photos_from:
                report["warnings"].append(f"{len(photo_rows)} item photo(s) not copied: pass --photos-from")
            report["counts"]["photos"] = len(photo_rows) - sum(w.startswith("photo missing") for w in report["warnings"])                 if photos_from else 0

            # ── check ──
            for table in TENANT_TABLES:
                want = src.execute(f"SELECT COUNT(*) FROM {table} WHERE household_id = ?", (sid,)).fetchone()[0]
                got = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE household_id = ?", (new_hid,)).fetchone()[0]
                if want != got:
                    raise SourceError(f"{table}: copied {got} of {want} rows")
                report["counts"][table] = got
            got = conn.execute("SELECT COUNT(*) FROM household_members WHERE household_id = ?", (new_hid,)).fetchone()[0]
            if got != len(members):
                raise SourceError(f"household_members: copied {got} of {len(members)}")
            if dry_run:
                raise _DryRun()
    except _DryRun:
        pass
    except BaseException:
        for path in copied_photos:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    finally:
        src.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
    return report


class _DryRun(Exception):
    """Raised inside the transaction so get_db() rolls it back."""


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m stashgrid.tools.import_sqlite",
                                description="Import a household from a self-hosted StashGrid SQLite database "
                                            "into the configured database (STASHGRID_DATABASE_URL).")
    p.add_argument("source", help="path to the Pi's inventory.db (not modified)")
    p.add_argument("--household", type=int, help="source household id (needed if it has more than one)")
    p.add_argument("--name", help="name for the new household (default: the source's)")
    p.add_argument("--photos-from", help="the Pi's static/photos folder, to copy item photos")
    p.add_argument("--dry-run", action="store_true", help="check everything, write nothing")
    args = p.parse_args(argv)
    try:
        report = import_household(args.source, household=args.household, name=args.name,
                                  photos_from=args.photos_from, dry_run=args.dry_run)
    except SourceError as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 1
    hh = report["household"]
    print(f"{'DRY RUN - nothing written. ' if report['dry_run'] else ''}"
          f"Imported '{hh['name']}' (source household {hh['source_id']}) as household {hh['id']} into {report['target']}")
    for k, v in report["counts"].items():
        print(f"  {k:<20} {v}")
    for line in report["renamed"]:
        print(f"  renamed: {line}")
    for line in report["merged"]:
        print(f"  merged:  {line}")
    for line in report["warnings"]:
        print(f"  warning: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
