#!/bin/bash
# Run every plugin test suite: the hook matrix (hooks.sh), the structural and secret-scan checks (scripts.sh), the Python engine
# (py/, once per python3 found) and, when the claude CLI is installed, `claude plugin validate` for the plugin (--strict) and for the
# marketplace at the repository root. Arguments (-v) go to the two shell suites. Exit 1 on any failure.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; PLUGIN="$(cd "$HERE/.." && pwd)"; REPO="$(cd "$PLUGIN/../.." && pwd)"; rc=0
echo "### hooks";   bash "$HERE/hooks.sh"   "$@" || rc=1
echo "### scripts"; bash "$HERE/scripts.sh" "$@" || rc=1
for py in python3 /usr/bin/python3; do
  command -v "$py" >/dev/null 2>&1 || continue
  echo "### engine ($("$py" --version 2>&1))"; ( cd "$HERE/py" && "$py" -m unittest -b 2>&1 | tail -3 ) | tee /dev/stderr | grep -q '^OK' || rc=1
done
if command -v claude >/dev/null 2>&1; then
  echo "### validate plugin";      command claude plugin validate "$PLUGIN" --strict || rc=1
  echo "### validate marketplace"; command claude plugin validate "$REPO" || rc=1
fi
[ $rc -eq 0 ] && echo "### ALL SUITES PASSED" || echo "### FAILURES ABOVE"
exit $rc
