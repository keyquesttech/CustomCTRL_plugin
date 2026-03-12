#!/bin/bash
set -e

TARGET="$HOME/klipper/klippy/extras/customctrl.py"

if [ -L "$TARGET" ]; then
    rm "$TARGET"
    echo "OK: Removed symlink $TARGET"
elif [ -e "$TARGET" ]; then
    echo "WARNING: $TARGET exists but is not a symlink. Skipping removal."
    echo "         Remove it manually if you are sure it belongs to this plugin."
else
    echo "INFO: $TARGET does not exist. Nothing to remove."
fi

echo "Restarting Klipper service..."
if sudo systemctl restart klipper; then
    echo "OK: Klipper restarted successfully."
else
    echo "ERROR: Failed to restart Klipper."
    exit 1
fi

echo "Uninstall complete."
