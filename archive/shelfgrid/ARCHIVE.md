# ShelfGrid branch archive (SG-12)

This folder is a copy of the `ShelfGrid` branch's book-catalogue code
(ISBN scanning, Open Library lookups, the book cards UI, the add-book
JSON API) taken when that branch was deleted. The branch replaced the whole
inventory app, and checking it out on a Pi dropped the inventory tables,
so it couldn't stay around.

It is **reference material for porting the library module (SG-08)**, not
part of the running app: nothing imports it, and it won't run as is.

- `app.py`: the ShelfGrid Flask app (books schema, routes, lookups)
- `scanner.py`: the ShelfGrid USB scanner loop (ISBN mode)
- `templates/`: the ShelfGrid pages; `_book_cards.html` is the book card UI
- `README-shelfgrid.md`, `requirements.txt`: the branch's own docs and deps

Left out: `shopping_token.txt` (a leaked secret, SG-01) and the branch's
setup/update scripts and service files, which main already has newer
versions of. Delete this folder once SG-08 has ported what it needs.
