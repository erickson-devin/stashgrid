# StashGrid 📦

A spatial inventory system for Raspberry Pi — scan barcodes with a USB scanner or phone camera, track items by room and shelf.

## Quickstart (new Pi)

```bash
git clone <your-repo-url> stashgrid
cd stashgrid
bash setup.sh
```

`setup.sh` will:
1. Check/install Python prerequisites (`python3-venv`, `python3-dev`)
2. Create a `venv/` and install all pip dependencies
3. Create the `/var/log/stashgrid/` log directory
4. Install and enable a **systemd service** so StashGrid starts on every boot
5. Optionally start the service immediately

After setup, the web UI is available at `https://<pi-ip>:5000`.  
> On first visit your browser will warn about the self-signed cert — click **Advanced → Proceed**.

---

## Service commands

```bash
sudo systemctl start stashgrid      # start
sudo systemctl stop stashgrid       # stop
sudo systemctl restart stashgrid    # restart
sudo systemctl status stashgrid     # status

journalctl -u stashgrid -f          # live service logs
tail -f /var/log/stashgrid/stashgrid.log  # app logs
```

---

## Updating from GitHub

```bash
cd stashgrid
git pull
source venv/bin/activate
pip install -r requirements.txt     # pick up any new/changed deps
sudo systemctl restart stashgrid
```

---

## Manual run (no systemd)

```bash
cd stashgrid
source venv/bin/activate
python app.py
```

---

## Hardware scanner (scanner.py)

`scanner.py` reads from the USB barcode scanner HID device directly and is **Linux/Pi only**.  
Update `SCANNER_DEVICE` at the top of `scanner.py` to match your scanner's `/dev/input/by-id/` path, then run:

```bash
source venv/bin/activate
python scanner.py
```

---

## Project structure

```
stashgrid/
├── app.py              # Flask web app
├── scanner.py          # USB HID barcode scanner service (Pi only)
├── requirements.txt    # Python dependencies
├── setup.sh            # One-shot automated setup script
├── stashgrid.service   # Systemd unit template (filled in by setup.sh)
├── inventory.db        # SQLite database (auto-created on first run)
├── templates/          # Jinja2 HTML templates
└── static/             # CSS, JS, uploaded photos
```
