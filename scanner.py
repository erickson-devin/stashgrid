import sqlite3
import requests
import time
import sys
import os
import json
import tempfile
from datetime import datetime

# evdev is a Linux-only package that reads raw hardware input events.
# scanner.py is intended to run on the Raspberry Pi. It cannot run on Windows.
if sys.platform != "linux":
    print("[!] scanner.py is designed for Linux/Raspberry Pi only.")
    print(f"    Detected platform: {sys.platform}")
    print("    Run this script on the Pi, not your development machine.")
    sys.exit(1)

try:
    from evdev import InputDevice, categorize, ecodes  # type: ignore[import]
except ImportError:
    print("[!] 'evdev' package not found. Install it on the Pi with:")
    print("    pip install evdev")
    sys.exit(1)

SCANNER_DEVICE = "/dev/input/by-id/usb-2022_0202-event-kbd"
DB = "inventory.db"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_state.json")

# Operational State
scan_mode = "IN"  # Default mode
mode_out_started = None
MODE_OUT_TIMEOUT_SECONDS = 300
barcode = ""

# Numeric and functional keyboard map
key_map = {
    "KEY_0": "0", "KEY_1": "1", "KEY_2": "2", "KEY_3": "3", "KEY_4": "4",
    "KEY_5": "5", "KEY_6": "6", "KEY_7": "7", "KEY_8": "8", "KEY_9": "9",
    "KEY_A": "A", "KEY_B": "B", "KEY_C": "C", "KEY_D": "D", "KEY_E": "E",
    "KEY_F": "F", "KEY_G": "G", "KEY_H": "H", "KEY_I": "I", "KEY_J": "J",
    "KEY_K": "K", "KEY_L": "L", "KEY_M": "M", "KEY_N": "N", "KEY_O": "O",
    "KEY_P": "P", "KEY_Q": "Q", "KEY_R": "R", "KEY_S": "S", "KEY_T": "T",
    "KEY_U": "U", "KEY_V": "V", "KEY_W": "W", "KEY_X": "X", "KEY_Y": "Y",
    "KEY_Z": "Z", "KEY_MINUS": "-"
}

def _write_state():
    """Atomically write current scanner state to a JSON file so Flask can serve it."""
    state = {
        "mode": scan_mode,
        "mode_out_started": mode_out_started,
        "timeout_seconds": MODE_OUT_TIMEOUT_SECONDS,
        "updated_at": datetime.now().isoformat()
    }

    try:
        dir_ = os.path.dirname(STATE_FILE)
        with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
            json.dump(state, f)
            tmp_path = f.name
        os.replace(tmp_path, STATE_FILE)  # atomic on POSIX
    except Exception as e:
        print(f"[!] Failed to write scanner state: {e}")


def add_scan(item_barcode):
    print(f"[*] Dispatching ISBN {item_barcode} to backend API...")
    try:
        response = requests.post(
            "http://localhost:5000/api/books/add",
            json={
                "isbn": item_barcode
            },
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("is_new"):
                print(f"[+] Added new book: {data.get('title')} (ISBN: {item_barcode})")
            else:
                print(f"[=] Re-scanned existing book: {data.get('title')}")
        else:
            print(f"[!] Server returned error: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"[!] Connection to backend API failed: {e}")

def remove_scan(item_barcode):
    print("[!] Warning: Remove Mode is disabled for physical library scanning.")
    print("    Please use the web interface to delete records.")

def handle_scan(scanned):
    global scan_mode, mode_out_started

    enforce_mode_timeout()
    scanned = scanned.strip().upper()

    # ── 1. SHEET ACTION COMMAND HANDLING ──
    if scanned in ["SG-CANCEL", "SG-DONE"]:
        scan_mode = "IN"
        mode_out_started = None
        _write_state()
        print("======== SESSION RESET / DISENGAGED ========")
        print("Awaiting scan...")
        return

    if scanned in ["SG-ADD", "SG-IN"]:
        scan_mode = "IN"
        mode_out_started = None
        _write_state()
        print("--> Mode toggled: [ INTAKE / ADD ]")
        return

    if scanned in ["SG-REMOVE", "SG-OUT"]:
        scan_mode = "OUT"
        mode_out_started = time.time()
        _write_state()
        print("--> Mode toggled: [ OUTTAKE / REMOVE ] (5 Minute Safety Timer Active)")
        return

    # ── 2. ITEM PROCESSING ──
    if scan_mode == "OUT":
        remove_scan(scanned)
    else:
        add_scan(scanned)

def enforce_mode_timeout():
    global scan_mode, mode_out_started

    if scan_mode == "OUT" and mode_out_started:
        elapsed = time.time() - mode_out_started

        if elapsed >= MODE_OUT_TIMEOUT_SECONDS:
            scan_mode = "IN"
            mode_out_started = None
            _write_state()
            print("MODE-OUT timed out after 5 minutes. Mode reset to IN.")

# ── OLD API LOOKUPS REMOVED (Now handled centrally by app.py) ──

device = InputDevice(SCANNER_DEVICE)

print("==================================================")
print("ShelfGrid Hardware Scanner Engine Active")
print("Awaiting scan sequence...")
print("==================================================")

for event in device.read_loop():
    if event.type == ecodes.EV_KEY:
        key_event = categorize(event)

        if key_event.keystate == 1:
            keycode = key_event.keycode

            if keycode == "KEY_ENTER":
                if barcode:
                    handle_scan(barcode)
                    barcode = ""
            elif keycode in key_map:
                barcode += key_map[keycode]