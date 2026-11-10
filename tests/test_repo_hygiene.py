"""Runtime secrets and state must never be committed (SG-01)."""
import fnmatch
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files the app or the Pi creates at runtime. Any of these in git is a leak:
# shopping_token.txt was once committed on the ShelfGrid branch.
FORBIDDEN = [
    "shopping_token.txt", "scanner_state.json", ".env",
    "*.db", "*.db-wal", "*.db-shm", "*.db-journal",
    "*.pem", ".cloudflared/*",
    "static/photos/*", "static/uploads/*", "backups/*", "logs/*",
]


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.splitlines()


def test_no_runtime_files_are_tracked():
    leaks = [f for f in _tracked_files()
             if any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch(os.path.basename(f), p) for p in FORBIDDEN)]
    assert not leaks, f"runtime files committed: {leaks}"


@pytest.mark.parametrize("path", ["shopping_token.txt", "scanner_state.json", "inventory.db",
                                  "inventory.db-wal", "backups/x.db", "static/photos/1.jpg", ".env"])
def test_runtime_files_are_ignored(path):
    try:
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT)
    except OSError:
        pytest.skip("git not available")
    assert r.returncode == 0, f"{path} is not in .gitignore"
