#!/usr/bin/env bash
# install.sh - Install mssh and the relay on macOS / Linux.
#
# Copies `mssh` + `oc-relay.py` into a bin directory (they must stay together,
# because mssh locates oc-relay.py in its own directory) and makes them
# executable. Safe to run again (idempotent).
#
# Usage:
#   bash install.sh                    # auto-detect dest: XDG_BIN_DIR, ~/bin, or ~/.local/bin
#   bash install.sh /custom/dir        # explicit destination
#   MSSH_INSTALL_DIR=/custom/dir bash install.sh

set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found on PATH." >&2
    echo "  macOS: install Command Line Tools (xcode-select --install) or 'brew install python'." >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

DEST=""
case "${1:-}" in
    "" | "-h" | "--help")
        if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
            usage
            exit 0
        fi
        ;;
    *)
        DEST="$1"
        ;;
esac

if [ -z "$DEST" ]; then
    DEST="${MSSH_INSTALL_DIR:-}"
fi
if [ -z "$DEST" ]; then
    if [ -n "${XDG_BIN_DIR:-}" ] && [ -d "$XDG_BIN_DIR" ]; then
        DEST="$XDG_BIN_DIR"
    elif [ -d "$HOME/bin" ]; then
        DEST="$HOME/bin"
    else
        DEST="$HOME/.local/bin"
    fi
fi

mkdir -p "$DEST"
install -m 755 "$HERE/mssh" "$DEST/mssh"
install -m 755 "$HERE/oc-relay.py" "$DEST/oc-relay.py"

echo "Installed mssh + oc-relay.py -> $DEST"

case ":$PATH:" in
    *":$DEST:"*) ;;
    *) echo "Add to your shell profile: export PATH=\"$DEST:\$PATH\"" ;;
esac

echo "Verify:  mssh   (running it with no target prints usage)"
echo "Try:     mssh mybox"
echo "Optional tests from the repo: python3 tests/test_relay.py"
