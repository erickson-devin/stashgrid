"""Pi install pieces (SG-24): kiosk launcher switch, service templates, scripts."""
import os
import re
import shutil
import subprocess

import pytest

from conftest import ROOT

from stashgrid import config
from stashgrid.core import kiosk

PLACEHOLDERS = {"__INSTALL_DIR__", "__VENV_DIR__", "__USER__", "__UID__", "__ENV_FILE__"}
UNITS = ["stashgrid.service", "stashgrid-scanner.service", "stashgrid-display.service"]


def test_kiosk_switch_off_skips_everything(monkeypatch):
    monkeypatch.setattr(config, "KIOSK", False)
    monkeypatch.setattr(kiosk, "_detect_display", lambda: pytest.fail("should not probe for a display"))
    kiosk._launch_kiosk_display(startup_delay=0)


def test_kiosk_browser_is_reaped(monkeypatch):
    waited = []

    class FakeProc:
        def wait(self):
            waited.append(True)
            return 0

    monkeypatch.setattr(config, "KIOSK", True)
    monkeypatch.setattr(kiosk, "_detect_display", lambda: ":0")
    monkeypatch.setattr(kiosk, "_find_kiosk_browser", lambda: ["chromium", "--kiosk"])
    monkeypatch.setattr(kiosk.subprocess, "Popen", lambda *a, **k: FakeProc())
    kiosk._launch_kiosk_display(startup_delay=0)
    assert waited == [True]


@pytest.mark.parametrize("unit", UNITS)
def test_unit_templates(unit):
    text = open(os.path.join(ROOT, unit), encoding="utf-8").read()
    used = set(re.findall(r"__[A-Z_]+__", text))
    assert used <= PLACEHOLDERS, used - PLACEHOLDERS
    assert "Environment=STASHGRID_SECRET_KEY" not in text, "secrets belong in the settings file, not the unit"
    assert "/home/" not in text, "paths come from setup.sh"
    if unit != "stashgrid-display.service":
        assert "EnvironmentFile=__ENV_FILE__" in text


@pytest.mark.parametrize("script", ["setup.sh", "update.sh"])
def test_scripts(script):
    text = open(os.path.join(ROOT, script), encoding="utf-8").read()
    assert "\r" not in text, "CRLF line endings break bash on the Pi"
    assert "/home/admin" not in text and "/home/pi/stashgrid" not in text, "install path must not be hard-coded"
    assert "shopping_token" not in text, "share links are created in Settings now (SG-35)"
    bash = shutil.which("bash")
    if bash:
        r = subprocess.run([bash, "-n", os.path.join(ROOT, script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_update_script_restarts_every_service():
    text = open(os.path.join(ROOT, "update.sh"), encoding="utf-8").read()
    for svc in ("stashgrid", "stashgrid-scanner", "stashgrid-display"):
        assert svc in text
    assert "pre-update.db" in text and "/login" in text
