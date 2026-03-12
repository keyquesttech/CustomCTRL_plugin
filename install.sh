#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/customctrl.py"
TARGET="$HOME/klipper/klippy/extras/customctrl.py"

if [ ! -f "$SOURCE" ]; then
    echo "ERROR: Source file not found: $SOURCE"
    exit 1
fi

if [ ! -d "$HOME/klipper/klippy/extras" ]; then
    echo "ERROR: Klipper extras directory not found: $HOME/klipper/klippy/extras"
    echo "       Is Klipper installed at ~/klipper?"
    exit 1
fi

ln -sf "$SOURCE" "$TARGET"
echo "OK: Symlinked $SOURCE -> $TARGET"

echo "Restarting Klipper service..."
if sudo systemctl restart klipper; then
    echo "OK: Klipper restarted successfully."
else
    echo "ERROR: Failed to restart Klipper."
    exit 1
fi

echo "Install complete."
