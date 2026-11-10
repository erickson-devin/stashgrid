"""Kiosk display engine: open the HUD full-screen on an attached display (moved from app.py, SG-06)."""
import os
import shutil
import subprocess
import time

from stashgrid import config
from stashgrid.config import logger


def _detect_display():
    """Find an active X11 display on this machine.
    Checks the environment first (works when launched from a desktop session),
    then probes common display numbers via xrandr (works when running as a
    system service with an X session already active on the console).
    Returns a display string such as ':0', or None if nothing is detected.
    """
    env_display = os.environ.get("DISPLAY")
    if env_display:
        return env_display

    # When running as a system service DISPLAY is not inherited.
    # Probe :0 and :1 directly — one of these will be live if a desktop is running.
    if shutil.which("xrandr"):
        for disp in (":0", ":1"):
            try:
                result = subprocess.run(
                    ["xrandr", "--display", disp],
                    timeout=2,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    return disp
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass

    return None


def _find_kiosk_browser():
    """Return a command list suitable for kiosk mode, or None.
    Tries Chromium first (the default browser on Raspberry Pi OS),
    then Firefox as a fallback.
    """
    for bin_name in ("chromium", "chromium-browser"):
        if shutil.which(bin_name):
            return [
                bin_name,
                "--kiosk",
                "--noerrdialogs",
                "--disable-infobars",
                "--no-first-run",
                "--disable-session-crashed-bubble",
                "--disable-features=TranslateUI",
                "--disable-restore-session-state",
                "--ignore-certificate-errors",  # required for the adhoc self-signed cert
            ]

    if shutil.which("firefox"):
        return ["firefox", "--kiosk"]

    return None


def _launch_kiosk_display(startup_delay=3):
    """Detect an attached display and open StashGrid in a kiosk browser window.
    Intended to be run in a daemon thread so it never blocks the Flask server.
    A short delay is applied first to ensure the server is accepting connections.
    Completely non-fatal: logs a message and returns if no display or browser
    is available.
    """
    if not config.KIOSK:
        logger.info("Kiosk: disabled (STASHGRID_KIOSK=0)")
        return
    time.sleep(startup_delay)

    display = _detect_display()
    if not display:
        logger.info("Kiosk: no display detected — skipping browser launch")
        return

    browser_cmd = _find_kiosk_browser()
    if not browser_cmd:
        logger.warning(
            "Kiosk: display found (%s) but no supported browser detected. "
            "Install Chromium with: sudo apt install chromium-browser",
            display,
        )
        return

    # Choose protocol to match however Flask actually started
    try:
        import OpenSSL  # noqa: F401
        url = "https://localhost:5000"
    except ImportError:
        url = "http://localhost:5000"

    env = os.environ.copy()
    env["DISPLAY"] = display

    try:
        proc = subprocess.Popen(
            browser_cmd + [url],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(
            "Kiosk: launched %s on display %s → %s",
            browser_cmd[0], display, url,
        )
    except Exception as exc:
        logger.error("Kiosk: failed to launch browser — %s", exc)
        return
    # Wait in this (daemon) thread so an exited browser is reaped instead of
    # lingering as a zombie under the app process (SG-24).
    code = proc.wait()
    logger.info("Kiosk: browser exited with code %s", code)
