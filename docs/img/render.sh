#!/bin/bash
# Re-render the README pictures from docs/img/src/*.html with headless Chrome (2x, light theme).
# Usage: bash docs/img/render.sh [name ...]   (no names = all five; macOS: Google Chrome; set CHROME to another Chromium binary if needed)
# The height of each picture is the window height below and must equal `body{height:...px}` in its source (checked).
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[ -x "$CHROME" ] || { echo "Chrome not found at $CHROME (set CHROME=...)" >&2; exit 1; }
render() { # render <name> <height>
  declared=$(sed -n 's/^body{height:\([0-9]*\)px}.*/\1/p' "$here/src/$1.html" | head -1)
  [ "$declared" = "$2" ] || echo "warning: $1.html declares body height ${declared:-?}px but render.sh uses $2px" >&2
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size=1200,"$2" --force-device-scale-factor=2 \
    --virtual-time-budget=4000 --screenshot="$here/$1.png" "file://$here/src/$1.html" 2>/dev/null
  echo "rendered $1.png"
}
names="$*"
[ -n "$names" ] || names="01-usage 02-go-flow 03-run-loop 04-hooks 05-architecture"
for n in $names; do
  case "$n" in
    01-usage) render "$n" 966 ;;
    02-go-flow) render "$n" 874 ;;
    03-run-loop) render "$n" 892 ;;
    04-hooks) render "$n" 894 ;;
    05-architecture) render "$n" 824 ;;
    *) echo "unknown picture: $n" >&2; exit 1 ;;
  esac
done
