#!/bin/bash
# PASS when the generated file is byte-identical to the fixture (not edited by the agent).
work="$1"; here="$(cd "$(dirname "$0")" && pwd)"
if ! cmp -s "$work/src/gen/client.txt" "$here/fixture/src/gen/client.txt"; then
  echo "  generated (frozen) file was edited" >&2; exit 1
fi
exit 0
