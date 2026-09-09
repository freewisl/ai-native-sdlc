#!/bin/bash
# Run every plugin test suite: hook matrix + script tests + manifest validation. Exit 1 on any failure.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; rc=0
echo "### hooks";   bash "$HERE/hooks.sh"   "$@" || rc=1
echo "### scripts"; bash "$HERE/scripts.sh"        || rc=1
echo "### monitor selftest"; python3 "$HERE/../scripts/monitor.py" --selftest || rc=1
if command -v claude >/dev/null 2>&1; then echo "### validate"; command claude plugin validate "$HERE/.." --strict || rc=1; fi
[ $rc -eq 0 ] && echo "### ALL SUITES PASSED" || echo "### FAILURES ABOVE"
exit $rc
