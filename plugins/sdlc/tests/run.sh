#!/bin/bash
# Run every plugin test suite: hook matrix + script tests + manifest validation. Exit 1 on any failure.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; rc=0
echo "### hooks";   bash "$HERE/hooks.sh"   "$@" || rc=1
echo "### scripts"; bash "$HERE/scripts.sh"        || rc=1
for py in python3 /usr/bin/python3; do
  command -v "$py" >/dev/null 2>&1 || continue
  echo "### engine ($("$py" --version 2>&1))"; ( cd "$HERE/py" && "$py" -m unittest -b 2>&1 | tail -3 ) | tee /dev/stderr | grep -q '^OK' || rc=1
done
echo "### monitor selftest"; python3 "$HERE/../scripts/monitor.py" --selftest || rc=1
if command -v claude >/dev/null 2>&1; then echo "### validate"; command claude plugin validate "$HERE/.." --strict || rc=1; fi
[ $rc -eq 0 ] && echo "### ALL SUITES PASSED" || echo "### FAILURES ABOVE"
exit $rc
