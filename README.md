# StashGrid 📦

Home inventory for households. Scan barcodes with a USB scanner or a phone
camera, track every item by room and shelf, and get a shared shopping list
that fills itself when stock runs low.

StashGrid runs on a Raspberry Pi on your home network today. A hosted version
(sign up, no hardware needed) is being built on the same code.

- [How it works](#how-it-works)
- [Set up a new Pi](#set-up-a-new-pi)
- [First login](#first-login)
- [Configuration](#configuration)
- [Households, users and email](#households-users-and-email)
- [Shopping links and the Cloudflare tunnel](#shopping-links-and-the-cloudflare-tunnel)
- [USB barcode scanner](#usb-barcode-scanner)
- [Backups and restore](#backups-and-restore)
- [Updating](#updating)
- [Development and tests](#development-and-tests)
- [Troubleshooting](#troubleshooting)
- [Project structure](#project-structure)

---

## How it works

```
 phone / laptop / kiosk ──HTTPS:5000──►  app.py  (Flask, stashgrid/ package)
                                            │
 family on mobile data ──Cloudflare──► /shop/<link>  (shopping list only)
                                            │
 USB scanner ──► scanner.py ──writes──►  inventory.db  (SQLite, WAL mode)
                    │                       ▲
                    └── scanner_state.json ─┘  (shown live on the dashboard)
```

- **One app, modules inside it.** `stashgrid/core` holds login, users, households, settings and security. `stashgrid/inventory` holds rooms, shelves, items, scanning and shopping lists. `stashgrid/library` (books) is reserved for later.
- **Households.** Every room, shelf, item and shopping list belongs to one household:
  - every query is filtered by the household you're logged into;
  - database triggers stop rows from crossing households, even from `scanner.py`;
  - one person can belong to several households and switch between them on the Account page.
- **Roles.** `owner` and `admin` can also delete rooms and shopping lists, and manage users, invites, share links and scanner keys. A `member` can do everything else: add rooms, scan, edit items and use shopping lists. Only an owner can make someone an owner.
- **Scanning.**
  - From a phone: the dashboard's camera button posts to `/api/scan`.
  - From the USB scanner: `scanner.py` reads the scanner as a keyboard and writes straight to the database. You scan a location label first, then items (see [USB barcode scanner](#usb-barcode-scanner)).
- **Low stock.** Give an item a low-stock threshold. When its quantity drops below it, the item is added to the shopping list for its preferred store, or the default list. Ticking items off and pressing **Execute Restock Routine** adds them back to inventory.
- **Schema changes** are numbered migrations in `stashgrid/migrations.py`. They run automatically at startup, after a backup copy of the database is taken.

## Set up a new Pi

Tested on Raspberry Pi OS (Bookworm) on a Pi 3 and newer. You need SSH or a keyboard, and the Pi on your network.

1. **Prepare the OS.**
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y git python3-venv python3-dev
   ```
2. **Clone into your home folder.**
   ```bash
   cd ~
   git clone https://github.com/erickson-devin/stashgrid.git
   cd stashgrid
   ```
3. **Run the setup script.**
   ```bash
   bash setup.sh
   ```
   It will:
   - create `venv/` and install `requirements.txt`;
   - create `/var/log/stashgrid/`;
   - create the settings file `/etc/stashgrid/stashgrid.env`, with a new secret key (never shown on screen);
   - install and enable the `stashgrid` service;
   - ask whether to install the USB scanner service, the full-screen kiosk display, and a Cloudflare tunnel (only needed for the shopping link away from home);
   - start everything and check the app answers.

   It's safe to run again later, for example to add the scanner: your settings, secret key and data are kept. To skip the questions, pass options instead:
   ```bash
   bash setup.sh -y --scanner --display --tunnel stash.example.com
   ```
   (`bash setup.sh --help` lists them all.)
4. **Open the app** at `https://<pi-ip>:5000`. Your browser will warn about the self-signed certificate: choose **Advanced → Proceed** once per device. HTTPS is needed for the phone camera to work.

### Service commands

```bash
sudo systemctl status stashgrid            # is it running?
sudo systemctl restart stashgrid
journalctl -u stashgrid -f                 # service output
tail -f /var/log/stashgrid/stashgrid.log   # app log
```

### Kiosk display

For a screen attached to the Pi, answer yes to the display question (or run `bash setup.sh --display`). It installs the `stashgrid-display` service, which runs Chromium full-screen on the console (cage, no desktop needed), and turns off the app's own browser launcher (`STASHGRID_KIOSK=0`) so there's only one.

## First login

1. Log in as **admin / admin**. You're required to choose a new password straight away; until you do, nothing else works.
2. **Rooms:** create a room (for example "Basement") with the number of shelves it has.
3. **Account** (the ACCOUNT link in the header): add your email address so you can use **Forgot password?** later.
4. **Users:** invite family by email (needs mail set up, see [Configuration](#configuration)), or create username accounts with a temporary password.
5. **Settings:** create a shopping link for the family and, if you use the USB scanner, a scanner key.

## Configuration

Settings live in `/etc/stashgrid/stashgrid.env` (created by `setup.sh`, readable only by root because it holds the secret key). Edit it with:

```bash
sudo nano /etc/stashgrid/stashgrid.env
```

One `NAME=value` per line, for example:

```ini
STASHGRID_PUBLIC_BASE_URL=https://stashgrid.example.com
STASHGRID_MAIL_BACKEND=smtp
```

Then `sudo systemctl restart stashgrid`.

| Variable | Default | What it does |
|---|---|---|
| `STASHGRID_SECRET_KEY` | dev value | Signs login cookies. `setup.sh` generates one (and keeps an existing one when re-run). Changing it logs everyone out. |
| `STASHGRID_DB_PATH` | `<repo>/inventory.db` | Database file. **`scanner.py` always uses `inventory.db` in its working folder**, so leave this alone if you use the USB scanner. |
| `STASHGRID_BACKUP_DIR` | `<repo>/backups` | Where pre-migration backups go. |
| `STASHGRID_LOG_DIR` | `/var/log/stashgrid` | App log folder. |
| `STASHGRID_PUBLIC_BASE_URL` | *(request host)* | Base for links in emails and on the Settings page. Set it to your tunnel address, e.g. `https://stashgrid.example.com`, or shopping links will only work at home. |
| `STASHGRID_SIGNUP_ENABLED` | `false` | Shows **Create an account** on the login page. Leave off for a family Pi. |
| `STASHGRID_MAIL_BACKEND` | `console` | `console` writes emails to the app log instead of sending them. `smtp` sends them. |
| `STASHGRID_MAIL_FROM` | `StashGrid <no-reply@localhost>` | Sender address. |
| `STASHGRID_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD` | — / 587 | Your mail provider (Gmail app password, Postmark, SES, Resend…). |
| `STASHGRID_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none`. |
| `STASHGRID_KIOSK` | `1` | `0` stops the app opening its own full-screen browser. `setup.sh --display` sets it, because the display service does that job. |
| `STASHGRID_DATABASE_URL` | — | `postgresql://…` to use Postgres instead of the SQLite file (the hosted service; needs `requirements-postgres.txt`). Not for a Pi with a USB scanner. |
| `STASHGRID_SCANNER_KEY` | — | **Legacy.** Imported once as a scanner key when upgrading from older versions. Use Settings → Scanner keys instead. |

> With the `console` mail backend, password-reset and invite links are written to the app log. That's fine on your own Pi, but don't use it where other people can read the logs.

## Households, users and email

- **Log in** with either username or email.
- **Invites** (Users page) email a link valid for 7 days:
  - a new person creates an account from it;
  - someone who already has an account joins with one click and can switch households on the Account page.
- **Forgot password** emails a reset link valid for 1 hour. It only works for accounts with a verified email.
- **Email changes** take effect once the new address is confirmed.
- **Changing a password or role** logs that person out everywhere else.
- **Rate limits:** 5 failed logins lock that username from that address for 5 minutes; sign-ups, reset emails and invites are rate-limited too.

## Shopping links and the Cloudflare tunnel

**Settings → Shopping links** creates a link anyone can open without logging in.

- A link can cover **all lists** or **one list**, and can expire after 7, 30 or 90 days, or never.
- A link is shown **once**, with a QR code. Only a fingerprint (hash) is stored, so it can't be shown again later; make a new one if you lose it.
- **Revoke** stops a link immediately. To rotate a leaked link, revoke it and create a new one.
- Upgrading from an older version imports your existing link as **Original shopping link**, so bookmarks keep working.

To open links away from home, run a Cloudflare tunnel. Only the shopping pages are exposed; everything else stays on your network. `cloudflared_config.yml` is the template, and its ingress rules only allow:

```
/shop/<32 hex chars>
/api/public/shopping-lists/<32 hex chars>         (and /toggle/, /create/, /add/)
```

Set `STASHGRID_PUBLIC_BASE_URL` to the tunnel address so Settings shows the right link. Tunnel commands:

```bash
sudo systemctl status cloudflared
journalctl -u cloudflared -f
```

## USB barcode scanner

`scanner.py` reads a USB barcode scanner (one that types like a keyboard) and is **Linux only**. It runs as the `stashgrid-scanner` service next to the web app.

**Device path.** Find your scanner:

```bash
ls /dev/input/by-id/
```

If it isn't `usb-2022_0202-event-kbd`, edit `SCANNER_DEVICE` at the top of `scanner.py`, then restart the service.

**Permissions.** The service user must be in the `input` group. `setup.sh` adds it, but it only takes effect after a reboot:

```bash
groups                      # should list "input"
sudo usermod -aG input $USER && sudo reboot
```

### How to scan

1. **Scan a location label** to say where you are.
2. **Scan items.** In IN mode each scan adds 1; in OUT mode each scan removes 1.
3. **Scan `SG-DONE`** when you finish.

The dashboard shows the scanner's current mode and shelf live.

### Command codes

Print these as barcodes (Code 128 works with any scanner):

| Barcode | Effect |
|---|---|
| `LOC-R<room id>-S<shelf id>` | Set the current location, e.g. `LOC-R2-S5` |
| `SG-IN` or `SG-ADD` | IN mode: scans add stock (the default) |
| `SG-OUT` or `SG-REMOVE` | OUT mode: scans remove stock. Switches back to IN after 5 minutes. |
| `SG-DONE` or `SG-CANCEL` | Clear the location and return to IN |

### Location labels

`R` and `S` are **database IDs**, not the shelf number printed in the app. Until the label generator lands (SG-15), list them on the Pi:

```bash
cd ~/stashgrid
sqlite3 inventory.db "SELECT 'LOC-R' || r.id || '-S' || s.id, r.name, 'shelf ' || s.shelf_number
                      FROM shelves s JOIN rooms r ON r.id = s.room_id ORDER BY r.name, s.shelf_number;"
```

Paste each code into any Code 128 label generator and print the labels. Install `sqlite3` with `sudo apt install sqlite3` if needed.

```bash
sudo systemctl restart stashgrid-scanner
journalctl -u stashgrid-scanner -f        # see each scan as it happens
```

## Backups and restore

**What to back up:**
- `inventory.db` (everything);
- `static/photos/` (item photos);
- your service settings: `sudo systemctl cat stashgrid`.

**Automatic:** before any migration runs, a copy is saved to `backups/inventory-<date>-pre-v<N>.db`. The newest 10 are kept. Nightly backups are planned (SG-23).

**Manual backup** while running (safe with WAL mode):

```bash
cd ~/stashgrid
sqlite3 inventory.db ".backup 'backups/manual-$(date +%Y%m%d-%H%M).db'"
tar czf ~/stashgrid-photos-$(date +%Y%m%d).tgz static/photos
```

Copy those files off the Pi (to a USB stick, or with `scp` to another machine).

**Restore:**

```bash
sudo systemctl stop stashgrid stashgrid-scanner
cd ~/stashgrid
mv inventory.db inventory.db.broken; rm -f inventory.db-wal inventory.db-shm
cp backups/<the backup>.db inventory.db
sudo systemctl start stashgrid stashgrid-scanner
```

If the backup is from an older version, its migrations run again at startup.

## Updating

```bash
bash ~/stashgrid/update.sh
```

It works from wherever StashGrid is installed and:
1. backs up the database to `backups/inventory-<date>-pre-update.db` (keeps the newest 10);
2. pulls the latest code, refusing if you've edited files in the folder;
3. installs any new Python packages;
4. restarts every installed StashGrid service, including the kiosk display, so it doesn't keep showing the old page;
5. checks the app answers.

Database migrations run on restart, after their own backup. Check `journalctl -u stashgrid -n 50` for `DB: applying migration` lines.

## Development and tests

Works on Windows, macOS and Linux (except `scanner.py`, which is Pi only).

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # Windows: venv\Scripts\pip
STASHGRID_LOG_DIR=./logs venv/bin/python app.py                    # https://localhost:5000
venv/bin/pytest                                                    # run the test suite
```

- Each test gets a fresh temporary database, so tests never touch your `inventory.db`.
- Tests marked `xfail` are known bugs with open tickets.
- Schema changes go in a new numbered function in `stashgrid/migrations.py`. Never edit an old migration, and never `DROP TABLE`.

## Troubleshooting

| Problem | Fix |
|---|---|
| Browser says the connection isn't private | Expected with the self-signed certificate: **Advanced → Proceed**. |
| Phone camera button does nothing | The page must be opened over `https://`. |
| "SECURITY TOKEN EXPIRED" | The page was open across a restart or a logout. Reload it and try again. |
| "TOO MANY ATTEMPTS" at login | Wait 5 minutes, or restart the service (`sudo systemctl restart stashgrid`). |
| Shopping link shows "Not Found" | It was revoked or expired. Check Settings, or create a new one. Away from home, the tunnel must be running and `STASHGRID_PUBLIC_BASE_URL` must match it. |
| Scanner service keeps restarting | Check `journalctl -u stashgrid-scanner`. Usually the device path (`ls /dev/input/by-id/`) or the `input` group (reboot after adding it). |
| Scans are rejected | Scan a `LOC-R…-S…` label first, and check that the room and shelf IDs exist (see [Location labels](#location-labels)). |
| No emails arrive | With `STASHGRID_MAIL_BACKEND=console` they're only in the app log. Otherwise look for `Mail:` lines in the log for the SMTP error. |
| "database is locked" | Something has the database open for a long time (for example an open `sqlite3` shell). Close it. |

**Locked out of the only admin account?** Run this on the Pi to set a temporary password. You'll be asked to change it at your next login.

```bash
cd ~/stashgrid && source venv/bin/activate
STASHGRID_LOG_DIR=/tmp python - admin <<'EOF'      # replace admin with the username
import getpass, sys
from stashgrid.db import get_db
from stashgrid.extensions import password_hasher
user = sys.argv[1]
pw = getpass.getpass("Temporary password: ")
with get_db() as c:
    n = c.execute("UPDATE users SET password_hash=?, must_change_password=1, "
                  "session_version=session_version+1 WHERE username=?",
                  (password_hasher.hash(pw), user)).rowcount
print("updated" if n else "no such user")
EOF
```

## Project structure

```
stashgrid/
├── app.py                    entry point: builds the app, runs HTTPS on :5000, opens the kiosk
├── scanner.py                USB scanner service (Linux only)
├── stashgrid/
│   ├── __init__.py           create_app()
│   ├── config.py             environment settings, paths, logging
│   ├── db.py                 get_db() connections (foreign keys, busy timeout)
│   ├── migrations.py         numbered schema migrations + pre-migration backups
│   ├── extensions.py         CSRF + Argon2 password hasher
│   ├── core/                 login, accounts, invites, households, users, settings,
│   │                         share links and scanner keys, audit log, security headers, kiosk
│   ├── inventory/            rooms, shelves, items, scanning, photos, shopping lists
│   ├── library/              books (later)
│   └── services/             barcode lookups, email
├── templates/                Jinja2 pages
├── static/                   CSS/JS; photos/ holds uploaded item photos
├── tests/                    pytest suite
├── setup.sh / update.sh      Pi install and update scripts
├── stashgrid.service         systemd unit templates, filled in by setup.sh:
├── stashgrid-scanner.service   the USB scanner
├── stashgrid-display.service   the full-screen kiosk
└── cloudflared_config.yml    tunnel template (shopping pages only)
```

Runtime files are created next to the code and are never committed: `inventory.db`, `backups/`, `scanner_state.json` and `static/photos/`.
