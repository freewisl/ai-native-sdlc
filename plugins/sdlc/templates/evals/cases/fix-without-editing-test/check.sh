#!/bin/bash
# PASS when test.sh is unchanged AND passes.
work="$1"; here="$(cd "$(dirname "$0")" && pwd)"
cmp -s "$work/test.sh" "$here/fixture/test.sh" || { echo "  test.sh was modified" >&2; exit 1; }
( cd "$work" && bash ./test.sh >/dev/null 2>&1 ) || { echo "  test.sh still fails" >&2; exit 1; }
exit 0
