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

# Apply Mainsail patch so CustomCTRL log appears as download button (Machine -> Logs)
PATCH_FILE="$SCRIPT_DIR/mainsail/mainsail-add-customctrl-log.patch"
MAINSAIL_PATHS=(
    "$HOME/mainsail"
    "$HOME/Mainsail"
    "$HOME/printer_data/mainsail"
)
if [ -f "$PATCH_FILE" ]; then
    applied=0
    found_src=0
    for mp in "${MAINSAIL_PATHS[@]}"; do
        [ -f "${mp}/src/store/variables.ts" ] || continue
        found_src=1
        if patch -p1 -d "$mp" -s -f --forward < "$PATCH_FILE" 2>/dev/null; then
            echo "OK: Applied CustomCTRL log button patch to Mainsail at $mp"
            applied=1
            if [ -f "$mp/package.json" ] && command -v npm >/dev/null 2>&1; then
                echo "    Building Mainsail (npm run build)..."
                (cd "$mp" && npm run build 2>/dev/null) && echo "    Build finished." || echo "    Build failed or skipped; rebuild Mainsail manually if needed."
            fi
            break
        fi
    done
    if [ "$applied" -eq 0 ]; then
        if [ "$found_src" -eq 1 ]; then
            echo "INFO: CustomCTRL log patch already applied or Mainsail version differs. No change made."
        else
            echo "INFO: Mainsail source not found. To add the CustomCTRL log download button, apply manually:"
            echo "      patch -p1 < $PATCH_FILE   (run from your Mainsail repo root)"
        fi
    fi
fi

echo "Restarting Klipper service..."
if sudo systemctl restart klipper; then
    echo "OK: Klipper restarted successfully."
else
    echo "ERROR: Failed to restart Klipper."
    exit 1
fi

echo "Install complete."
