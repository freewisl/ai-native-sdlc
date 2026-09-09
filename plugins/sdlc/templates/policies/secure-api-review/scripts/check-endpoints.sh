#!/bin/bash
# Deterministic companion to the secure-api-review skill.
# Replace the heuristics below with your framework's real checks (route table dump, OpenAPI diff, lint rule).
# Exit 0 always; the skill includes the OUTPUT in its summary. A hook or review pass makes it binding.
root="${1:-.}"
echo "== secure-api-review: endpoint scan ($root)"
echo "-- routes without an auth annotation (heuristic):"
grep -rInE '@(Get|Post|Put|Delete|Patch)Mapping|@app\.(get|post|put|delete|patch)|router\.(get|post|put|delete|patch)\(' "$root" --include='*.java' --include='*.py' --include='*.ts' --include='*.js' 2>/dev/null \
  | grep -viE 'auth|jwt|secured|login_required|health' | head -50 || true
echo "-- PII-tagged fields appearing in log statements (heuristic):"
grep -rInE 'log(ger)?\.(info|debug|warn|error)\(.*(ssn|social|passport|birth|email|phone|address)' "$root" 2>/dev/null | head -50 || true
echo "== end"
