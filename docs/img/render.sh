#!/bin/bash
# Re-render the README pictures from docs/img/src/*.html with headless Chrome (2x, light theme).
# Usage: bash docs/img/render.sh   (macOS: Google Chrome; set CHROME to another Chromium binary if needed)
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[ -x "$CHROME" ] || { echo "Chrome not found at $CHROME (set CHROME=...)" >&2; exit 1; }
render() { # render <name> <height>
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size=1200,"$2" --force-device-scale-factor=2 \
    --virtual-time-budget=4000 --screenshot="$here/$1.png" "file://$here/src/$1.html" 2>/dev/null
  echo "rendered $1.png"
}
render 01-two-commands 600
render 02-go-flow 640
render 03-run-loop 640
render 04-hooks 640
