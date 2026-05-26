# StashGrid 📦

A spatial inventory system for Raspberry Pi — scan barcodes with a USB scanner or phone camera, track items by room and shelf.

## Quickstart (new Pi)

```bash
git clone https://github.com/erickson-devin/stashgrid.git stashgrid
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

`scanner.py` reads directly from the USB barcode scanner as a HID input device and is **Linux/Pi only**.  
It runs as a separate process alongside `app.py` and writes to the same `inventory.db`.

`setup.sh` will ask if you want to install the scanner service. If you said no, you can re-run `setup.sh` at any time.

### Device path

The scanner device path is hardcoded in `scanner.py`:

```python
SCANNER_DEVICE = "/dev/input/by-id/usb-2022_0202-event-kbd"
```

If your scanner shows up at a different path, update that line before running setup. List available devices with:

```bash
ls /dev/input/by-id/
```

### `input` group

To read from `/dev/input/` without `sudo`, your user must be in the `input` group.  
`setup.sh` adds this automatically, but **the change only takes effect after a logout/reboot**.

```bash
# Check your groups
groups

# Add manually if needed
sudo usermod -aG input $USER
# then reboot or logout
```

### Scanner service commands

```bash
sudo systemctl start stashgrid-scanner      # start
sudo systemctl stop stashgrid-scanner       # stop
sudo systemctl restart stashgrid-scanner    # restart
journalctl -u stashgrid-scanner -f          # live logs
```

### Manual run (no systemd)

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
