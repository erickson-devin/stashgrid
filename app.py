"""StashGrid entry point. The application lives in the stashgrid package (SG-06);
this file stays so `python app.py` and the systemd unit keep working."""
import threading

from stashgrid import create_app
from stashgrid.core.kiosk import _launch_kiosk_display

app = create_app()


if __name__ == "__main__":
    # Spin up the kiosk display in a background thread.
    # The thread is daemonised so it dies automatically when Flask exits.
    # It will silently do nothing if no display or browser is found.
    threading.Thread(target=_launch_kiosk_display, daemon=True).start()

    # HTTPS is required for camera access on mobile browsers (iOS Safari, Android Chrome).
    # ssl_context='adhoc' generates a self-signed cert automatically via pyOpenSSL.
    # Install on Pi with: pip install pyOpenSSL
    # First visit: browser will show a security warning — tap "Advanced" → "Proceed" once.
    try:
        app.run(host="0.0.0.0", port=5000, ssl_context="adhoc")
    except Exception:
        # pyOpenSSL not installed — fall back to plain HTTP (camera will not work on mobile)
        print("[!] pyOpenSSL not found. Running on HTTP — camera scanning will not work on mobile.")
        print("    Install with: pip install pyOpenSSL")
        app.run(host="0.0.0.0", port=5000)
