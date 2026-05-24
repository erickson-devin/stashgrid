import sqlite3
import requests
import time
import sys
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

# Spatial Tracking States
current_room_id = None
current_shelf_id = None
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

def add_scan(item_barcode, room_id, shelf_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB) as conn:
        # Match items living at this exact relational coordinate
        existing = conn.execute("""
            SELECT id FROM inventory
            WHERE barcode = ? AND room_id = ? AND shelf_id = ? AND is_active = 1
        """, (item_barcode, room_id, shelf_id)).fetchone()

        if existing:
            conn.execute("""
                UPDATE inventory
                SET quantity = quantity + 1,
                    updated_at = ?
                WHERE id = ?
            """, (now, existing[0]))
            print(f"[+] Incrementing quantity for barcode: {item_barcode}")
        else:
            product_name = lookup_product_name(item_barcode) or "[ New Asset ]"
            conn.execute("""
                INSERT INTO inventory
                (barcode, name, room_id, shelf_id, quantity, notes, updated_at, is_active)
                VALUES (?, ?, ?, ?, 1, '', ?, 1)
            """, (item_barcode, product_name, room_id, shelf_id, now))
            print(f"[*] Registered new spatial item: {product_name} ({item_barcode})")

def remove_scan(item_barcode, room_id, shelf_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB) as conn:
        existing = conn.execute("""
            SELECT id, quantity FROM inventory
            WHERE barcode = ? AND room_id = ? AND shelf_id = ? AND is_active = 1
        """, (item_barcode, room_id, shelf_id)).fetchone()

        if not existing:
            print(f"[!] Warning: Barcode {item_barcode} not found on this shelf. Cannot remove.")
            return

        item_id, quantity = existing

        if quantity > 1:
            conn.execute("""
                UPDATE inventory
                SET quantity = quantity - 1,
                    updated_at = ?
                WHERE id = ?
            """, (now, item_id))
            print(f"[-] Decremented quantity for barcode: {item_barcode}")
        else:
            # Soft delete matching your web application's rules
            conn.execute("""
                UPDATE inventory
                SET quantity = 0,
                    is_active = 0,
                    removed_at = ?,
                    removed_reason = 'Removed via Physical Scanner HUD'
                WHERE id = ?
            """, (now, item_id))
            print(f"[✕] Final unit removed. Deactivating entry ID #{item_id}")

def handle_scan(scanned):
    global current_room_id, current_shelf_id, scan_mode, mode_out_started

    enforce_mode_timeout()
    scanned = scanned.strip().upper()

    # ── 1. SHEET ACTION COMMAND HANDLING ──
    if scanned in ["SG-CANCEL", "SG-DONE"]:
        current_room_id = None
        current_shelf_id = None
        scan_mode = "IN"
        mode_out_started = None
        print("======== SESSION RESET / DISENGAGED ========")
        print("Awaiting physical location target scanning...")
        return

    if scanned in ["SG-ADD", "SG-IN"]:
        scan_mode = "IN"
        mode_out_started = None
        print("--> Mode toggled: [ INTAKE / ADD ]")
        return

    if scanned in ["SG-REMOVE", "SG-OUT"]:
        scan_mode = "OUT"
        mode_out_started = time.time()
        print("--> Mode toggled: [ OUTTAKE / REMOVE ] (5 Minute Safety Timer Active)")
        return

    # ── 2. PHYSICAL LOCATION SPATIAL TAGGING ──
    # Expects format: LOC-R<room_id>-S<shelf_id>  Example: LOC-R2-S5
    if scanned.startswith("LOC-R"):
        try:
            parts = scanned.split("-")
            room_part = parts[1]  # "R2"
            shelf_part = parts[2] # "S5"

            current_room_id = int(room_part.replace("R", ""))
            current_shelf_id = int(shelf_part.replace("S", ""))

            print(f"\n=========================================")
            print(f"TARGET ACQUIRED: Room ID {current_room_id} // Shelf ID {current_shelf_id}")
            print(f"Current Mode: [{scan_mode}]")
            print(f"=========================================")
            return
        except Exception:
            print(f"[!] Invalid location barcode syntax: '{scanned}'. Use format: LOC-R[ID]-S[ID]")
            return

    # ── 3. ITEM PROCESSING ──
    if current_room_id is None or current_shelf_id is None:
        print(f"[!] Scan Rejected: '{scanned}'. You must scan a location barcode first (e.g., LOC-R1-S3)")
        return

    if scan_mode == "OUT":
        remove_scan(scanned, current_room_id, current_shelf_id)
    else:
        add_scan(scanned, current_room_id, current_shelf_id)

def enforce_mode_timeout():
    global scan_mode, mode_out_started

    if scan_mode == "OUT" and mode_out_started:
        elapsed = time.time() - mode_out_started

        if elapsed >= MODE_OUT_TIMEOUT_SECONDS:
            scan_mode = "IN"
            mode_out_started = None
            print("MODE-OUT timed out after 5 minutes. Mode reset to IN.")

def lookup_product_name(barcode):
    if not barcode.isdigit():
        return ""

    return (
        lookup_open_food_facts(barcode)
        or lookup_upcitemdb(barcode)
        or ""
    )


def lookup_open_food_facts(barcode):
    url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"

    try:
        response = requests.get(
            url,
            timeout=5,
            headers={"User-Agent": "StorageInventoryPi/1.0"}
        )

        if response.status_code != 200:
            return ""

        data = response.json()

        if data.get("status") == 1:
            product = data.get("product", {})
            name = (
                product.get("product_name")
                or product.get("generic_name")
                or product.get("brands")
            )

            if name:
                return name.strip()

    except Exception as e:
        print(f"OpenFoodFacts lookup failed for {barcode}: {e}")

    return ""


def lookup_upcitemdb(barcode):
    url = f"https://api.upcitemdb.com/prod/trial/lookup?upc={barcode}"

    try:
        response = requests.get(
            url,
            timeout=5,
            headers={"User-Agent": "StorageInventoryPi/1.0"}
        )

        if response.status_code != 200:
            return ""

        data = response.json()

        items = data.get("items", [])
        if items:
            title = items[0].get("title")
            if title:
                return title.strip()

    except Exception as e:
        print(f"UPCitemDB lookup failed for {barcode}: {e}")

    return ""

device = InputDevice(SCANNER_DEVICE)

print("==================================================")
print("StashGrid Spatial Hardware Service Engine Active")
print("Awaiting location assignment scan sequence...")
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