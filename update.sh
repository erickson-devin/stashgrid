#!/bin/bash

cd /home/admin/stashgrid

git pull

source venv/bin/activate

pip install -r requirements.txt

sudo systemctl restart stashgrid

echo "StashGrid updated successfully."