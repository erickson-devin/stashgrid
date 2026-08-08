#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

git pull

source venv/bin/activate

pip install -r requirements.txt

sudo systemctl restart stashgrid

echo "StashGrid updated successfully."