#!/usr/bin/env sh
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$SCRIPT_DIR/scripts/start/start.sh"
echo
echo "You can close this window after the app opens."
printf "Press Enter to close..."
read -r _
