#!/usr/bin/env bash
# Runs scripts/install_linux.sh, which copies the app into ~/.local/share/atak-imagery
# (persistent; safe to delete the extracted folder afterward). Override with ATAK_PIPELINE_HOME.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$ROOT/scripts/install_linux.sh"

if [ ! -f "$SCRIPT" ]; then
    echo "Missing installer script: $SCRIPT"
    exit 1
fi

# With options (e.g. --quiet), run directly and pass them on. The installer rejects unknown options.
if [ "$#" -gt 0 ]; then
    exec "$SCRIPT" "$@"
fi

# If not already running in a real terminal, relaunch in one.
if [ -z "${TERM:-}" ] || [ "${TERM:-dumb}" = "dumb" ]; then
    if command -v x-terminal-emulator >/dev/null 2>&1; then
        exec x-terminal-emulator -e bash -lc "\"$SCRIPT\"; echo; echo 'Press Enter to close...'; read -r"
    elif command -v gnome-terminal >/dev/null 2>&1; then
        exec gnome-terminal -- bash -lc "\"$SCRIPT\"; echo; echo 'Press Enter to close...'; read -r"
    elif command -v konsole >/dev/null 2>&1; then
        exec konsole -e bash -lc "\"$SCRIPT\"; echo; echo 'Press Enter to close...'; read -r"
    elif command -v xfce4-terminal >/dev/null 2>&1; then
        exec xfce4-terminal --hold -e "bash -lc '\"$SCRIPT\"'"
    elif command -v mate-terminal >/dev/null 2>&1; then
        exec mate-terminal -- bash -lc "\"$SCRIPT\"; echo; echo 'Press Enter to close...'; read -r"
    elif command -v xterm >/dev/null 2>&1; then
        exec xterm -hold -e "\"$SCRIPT\""
    fi
fi

exec "$SCRIPT"
