#!/bin/bash
# sdlc plugin — script tests (init/doctor/status/run-evals/monitor/metrics + manifest validation).
# Run from anywhere: bash tests/scripts.sh [-v]. Prints PASS/FAIL per check and exits 1 when any check failed.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; PLUGIN="$(cd "$HERE/.." && pwd)"; SCRIPTS="$PLUGIN/scripts"
export CLAUDE_PLUGIN_ROOT="$PLUGIN"
export SDLC_SKIP_GH_DETECT=1
export SDLC_AUTH=api   # this machine may hold a stored token; tests pin the default auth explicitly
unset CLAUDE_PROJECT_DIR
WORK=$(mktemp -d "${TMPDIR:-/tmp}/sdlc-scripts.XXXXXX"); trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0; VERBOSE=false; [ "${1:-}" = "-v" ] && VERBOSE=true
ok()  { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n      %s\n' "$1" "$(printf '%s' "$2" | head -c 400 | tr '\n' ' ')"; }
check() { if [ "$2" = true ]; then ok "$1"; else bad "$1" "${3:-}"; fi; }
has()   { case "$2" in *"$3"*) ok "$1";; *) bad "$1" "missing '$3' in: $2";; esac; }
lacks() { case "$2" in *"$3"*) bad "$1" "unexpected '$3' in: $2";; *) ok "$1";; esac; }
tree_sum() { ( cd "$1" && find . -type f -not -path './.git/*' -print0 | sort -z | xargs -0 shasum 2>/dev/null | shasum | cut -c1-16 ); }
py_json_ok() { python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" 2>/dev/null; }
git_init() { ( cd "$1" && git init -q . && git config user.email t@t && git config user.name t && git config commit.gpgsign false ) ; }
git_commit() { ( cd "$1" && git add -A >/dev/null 2>&1 && GIT_AUTHOR_DATE="$3" GIT_COMMITTER_DATE="$3" git commit -qm "$2" >/dev/null 2>&1 ); }

echo "== sdlc script tests  (plugin: $PLUGIN, work: $WORK)"

# ---------------------------------------------------------------- manifest / structure
echo "-- structure"
out=$(cd "$PLUGIN" && command claude plugin validate . --strict 2>&1); has "claude plugin validate --strict passes" "$out" "Validation passed"
n=$(ls -d "$PLUGIN"/skills/*/ | wc -l | tr -d ' '); check "19 skills present ($n)" "$([ "$n" -eq 19 ] && echo true || echo false)" "$(ls "$PLUGIN/skills")"
n=$(ls "$PLUGIN"/agents/*.md | wc -l | tr -d ' '); check "5 agents present ($n)" "$([ "$n" -eq 5 ] && echo true || echo false)"
for s in "$PLUGIN"/skills/*/SKILL.md; do
  name=$(basename "$(dirname "$s")"); fm_name=$(sed -n '2,/^---$/p' "$s" | grep -E '^name:' | sed 's/name: *//')
  [ "$fm_name" = "$name" ] || bad "skill frontmatter name matches dir: $name" "got '$fm_name'"
  grep -q '^description:' "$s" || bad "skill has description: $name" ""
  lines=$(wc -l < "$s"); [ "$lines" -le 500 ] || bad "skill body ≤ 500 lines: $name" "$lines lines"
done; ok "skill frontmatter checks ran ($(ls -d "$PLUGIN"/skills/*/ | wc -l | tr -d ' ') skills)"
for h in session-start session-end guard-edit guard-bash post-edit post-bash stop-verify; do [ -f "$SCRIPTS/hooks/$h.sh" ] || bad "hook script exists: $h" ""; done; ok "hook scripts referenced by hooks.json exist"
for f in sdlc-evals.yml sdlc-review.yml sdlc-spec-on-intent.yml sdlc-ci-triage.yml sdlc-monitor.yml sdlc-scan.yml sdlc-autopilot.yml CODEOWNERS; do [ -f "$PLUGIN/templates/github/$f" ] || bad "github template exists: $f" ""; done; ok "github templates present"
for f in intent.md spec.md plan.md REVIEW.md CLAUDE.sections.md LESSONS.md intent-README.md evals-README.md; do [ -f "$PLUGIN/templates/en/$f" ] && [ -f "$PLUGIN/templates/ko/$f" ] || bad "template en+ko: $f" ""; done; ok "en/ko template sets complete"
py_json_ok "$PLUGIN/templates/config/config.json" && ok "templates/config/config.json is valid JSON" || bad "config.json valid" ""
py_json_ok "$PLUGIN/templates/config/bands.json" && ok "bands.json valid JSON" || bad "bands.json valid" ""
py_json_ok "$PLUGIN/templates/config/managed-settings.json" && ok "managed-settings.json valid JSON" || bad "managed valid" ""
check "plugin.json does not re-declare the auto-loaded hooks/hooks.json (would fail to load: duplicate hooks file)" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("false" if d.get("hooks") in ("./hooks/hooks.json","hooks/hooks.json") else "true")' "$PLUGIN/.claude-plugin/plugin.json")" ""
has "managed-settings keeps the playbook keys" "$(cat "$PLUGIN/templates/config/managed-settings.json")" '"disableBypassPermissionsMode": "disable"'

# ---------------------------------------------------------------- init: empty new project (not git)
echo "-- init: empty directory"
E="$WORK/empty"; mkdir -p "$E"
out=$(bash "$SCRIPTS/init.sh" --dir "$E" 2>&1); rc=$?
check "init exits 0 on empty dir" "$([ $rc -eq 0 ] && echo true || echo false)" "$out"
has "init creates config" "$out" "CREATED"; [ -f "$E/.sdlc/config.json" ] && ok "config.json written" || bad "config.json written" "$out"
has "init warns about missing git" "$out" "git"
for f in CLAUDE.md REVIEW.md intent/TEMPLATE.md intent/README.md spec/TEMPLATE.md plan/TEMPLATE.md evals/README.md .sdlc/bands.json .sdlc/frozen-paths.txt .sdlc/LESSONS.md .gitignore; do [ -f "$E/$f" ] || bad "created: $f" "$(ls -R "$E" | head -40)"; done; ok "scaffold files present"
[ -d "$E/evals/cases/no-secret-in-code" ] && ok "example eval cases copied" || bad "example eval cases copied" ""
has "CLAUDE.md has the five sections" "$(cat "$E/CLAUDE.md")" "## Things Claude gets wrong"; has "CLAUDE.md verifying block" "$(cat "$E/CLAUDE.md")" "fix the code, not the test"
sum1=$(tree_sum "$E"); out2=$(bash "$SCRIPTS/init.sh" --dir "$E" 2>&1); sum2=$(tree_sum "$E")
check "second init is idempotent (no file changes)" "$([ "$sum1" = "$sum2" ] && echo true || echo false)" "$out2"
lacks "second init creates nothing" "$out2" "CREATED"; has "second init reports SKIPPED" "$out2" "SKIPPED"

# ---------------------------------------------------------------- init: existing project (non-destructive merge)
echo "-- init: existing project"
X="$WORK/existing"; mkdir -p "$X/src" "$X/tests" "$X/.claude" "$X/.github/workflows" "$X/evals/legacy"; git_init "$X"
cat > "$X/package.json" <<'EOF'
{ "name": "orders-api", "private": true, "scripts": { "build": "echo build", "test": "node tests/run.js", "lint": "echo lint" } }
EOF
printf 'PASS\n' > "$X/tests/run.js"
cat > "$X/CLAUDE.md" <<'EOF'
# Orders API

Team notes that must survive. Ask #orders-dev before touching billing.

## Commands
- Test: npm test

## Style
- ES modules only.
EOF
cat > "$X/.claude/settings.json" <<'EOF'
{ "permissions": { "allow": ["Bash(npm test)", "Bash(git status*)"], "deny": ["Read(.env*)"] },
  "hooks": { "PostToolUse": [ { "matcher": "Edit", "hooks": [ { "type": "command", "command": "echo team-hook" } ] } ] },
  "env": { "TEAM_FLAG": "1" } }
EOF
printf 'name: CI\non: [push]\n' > "$X/.github/workflows/ci.yml"; printf 'legacy harness\n' > "$X/evals/legacy/README.md"; printf 'node_modules/\n' > "$X/.gitignore"
git_commit "$X" baseline "2026-01-01T00:00:00Z"
dry_before=$(tree_sum "$X"); out=$(bash "$SCRIPTS/init.sh" --dir "$X" --dry-run 2>&1); dry_after=$(tree_sum "$X")
check "--dry-run writes nothing" "$([ "$dry_before" = "$dry_after" ] && echo true || echo false)" "$out"; has "--dry-run says WOULD" "$out" "WOULD"
out=$(bash "$SCRIPTS/init.sh" --dir "$X" --github 2>&1); rc=$?
check "init exits 0 on existing project" "$([ $rc -eq 0 ] && echo true || echo false)" "$out"
has "original CLAUDE.md text intact" "$(cat "$X/CLAUDE.md")" "Ask #orders-dev before touching billing."
n=$(grep -c '^## Commands' "$X/CLAUDE.md"); check "## Commands not duplicated" "$([ "$n" -eq 1 ] && echo true || echo false)" "count=$n"
has "missing sections appended" "$(cat "$X/CLAUDE.md")" "## Verifying your work"; has "existing ## Style kept" "$(cat "$X/CLAUDE.md")" "## Style"
py_json_ok "$X/.claude/settings.json" && ok "settings.json still valid JSON" || bad "settings.json valid" "$(cat "$X/.claude/settings.json")"
s=$(cat "$X/.claude/settings.json"); has "original allow kept" "$s" '"Bash(npm test)"'; has "our deny added" "$s" 'git push --force'; has "team hook preserved" "$s" "echo team-hook"; has "env preserved" "$s" '"TEAM_FLAG"'
has "detected npm commands in config" "$(cat "$X/.sdlc/config.json")" '"test": "npm run test"'
[ -f "$X/evals/legacy/README.md" ] && [ "$(cat "$X/evals/legacy/README.md")" = "legacy harness" ] && ok "unrelated evals/ untouched" || bad "unrelated evals/ untouched" ""
has "paths.evals switched to sdlc-evals" "$(cat "$X/.sdlc/config.json")" '"evals": "sdlc-evals"'; [ -f "$X/sdlc-evals/README.md" ] && ok "sdlc-evals/ created" || bad "sdlc-evals/ created" ""
[ -f "$X/.github/workflows/sdlc-review.yml" ] && ok "--github copied workflows" || bad "--github copied workflows" "$(ls "$X/.github/workflows")"
[ "$(cat "$X/.github/workflows/ci.yml")" = "$(printf 'name: CI\non: [push]')" ] && ok "existing ci.yml untouched" || bad "existing ci.yml untouched" ""
[ -f "$X/.github/CODEOWNERS" ] && ok "CODEOWNERS installed" || bad "CODEOWNERS installed" ""
left=$(grep -lE '\{\{[a-z_]+\}\}' "$X"/.github/workflows/sdlc-*.yml "$X/.github/CODEOWNERS" 2>/dev/null || true); check "no unrendered {{placeholders}} in installed .github files (GitHub \${{ }} expressions are fine)" "$([ -z "$left" ] && echo true || echo false)" "$left"
has "CODEOWNERS owner lines rendered" "$(cat "$X/.github/CODEOWNERS")" "CLAUDE.md                 @"
has "ci-triage listens to the detected CI workflow name" "$(cat "$X/.github/workflows/sdlc-ci-triage.yml")" 'workflows: ["CI"]'
has "evals workflow installs from a marketplace source" "$(cat "$X/.github/workflows/sdlc-evals.yml")" "claude plugin marketplace add freewisl/ai-native-sdlc"
out=$(bash "$SCRIPTS/init.sh" --dir "$WORK/gh2" --github --marketplace acme/ai-native-sdlc --ci-workflow "Build and Test" --team @acme/platform 2>&1 || true); mkdir -p "$WORK/gh2" >/dev/null 2>&1
mkdir -p "$WORK/gh3"; out=$(bash "$SCRIPTS/init.sh" --dir "$WORK/gh3" --github --marketplace acme/ai-native-sdlc --ci-workflow "Build and Test" --team @acme/platform 2>&1)
has "--marketplace rendered" "$(cat "$WORK/gh3/.github/workflows/sdlc-evals.yml")" "marketplace add acme/ai-native-sdlc"
has "--ci-workflow rendered" "$(cat "$WORK/gh3/.github/workflows/sdlc-ci-triage.yml")" 'workflows: ["Build and Test"]'
has "--team rendered into CODEOWNERS" "$(cat "$WORK/gh3/.github/CODEOWNERS")" "@acme/platform"
has "default auth renders ANTHROPIC_API_KEY" "$(cat "$WORK/gh3/.github/workflows/sdlc-evals.yml")" 'ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}'
mkdir -p "$WORK/gh4"; out=$(bash "$SCRIPTS/init.sh" --dir "$WORK/gh4" --github --auth oauth 2>&1)
has "--auth oauth renders CLAUDE_CODE_OAUTH_TOKEN for claude -p steps" "$(cat "$WORK/gh4/.github/workflows/sdlc-monitor.yml")" 'CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}'
has "--auth oauth renders claude_code_oauth_token for the action" "$(cat "$WORK/gh4/.github/workflows/sdlc-review.yml")" 'claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}'
lacks "--auth oauth leaves no anthropic_api_key input" "$(cat "$WORK/gh4/.github/workflows/sdlc-review.yml")" "anthropic_api_key:"
has "--auth oauth prints the setup-token note" "$out" "claude setup-token"
mkdir -p "$WORK/solo"; out=$(bash "$SCRIPTS/init.sh" --dir "$WORK/solo" --solo 2>&1); has "--solo sets roles.solo" "$(cat "$WORK/solo/.sdlc/config.json")" '"solo": true'
has "new config records ci.auth" "$(cat "$WORK/solo/.sdlc/config.json")" '"auth": "api"'; has "new config records default_branch" "$(cat "$WORK/solo/.sdlc/config.json")" '"default_branch": "main"'
has "solo → roles.autopilot on by default" "$(cat "$WORK/solo/.sdlc/config.json")" '"autopilot": true'; has "solo → roles.auto_merge on by default" "$(cat "$WORK/solo/.sdlc/config.json")" '"auto_merge": true'
has "team (empty dir) → auto_merge off" "$(cat "$E/.sdlc/config.json")" '"auto_merge": false'; has "team → loop off" "$(cat "$E/.sdlc/config.json")" '"enabled": false'
python3 - "$WORK/solo/.sdlc/config.json" <<'PY'
import json,sys; p=sys.argv[1]; c=json.load(open(p)); c['roles']['autopilot']=False; json.dump(c,open(p,'w'),indent=2)
PY
out=$(bash "$SCRIPTS/init.sh" --dir "$WORK/solo" --solo 2>&1); has "re-run init keeps an explicit roles.autopilot=false (a pause the owner added)" "$(cat "$WORK/solo/.sdlc/config.json")" '"autopilot": false'
# --- zero-flag detection with a stub gh (no network) ---
STUB="$WORK/stub-bin"; mkdir -p "$STUB"
cat > "$STUB/gh" <<'GH'
#!/bin/bash
case "$*" in
  "auth status --hostname github.com"*) exit 0;;
  "auth status"*) echo "X github.kakaocorp.com: token expired" >&2; exit 1;;
  "repo view --json nameWithOwner --jq .nameWithOwner"*) echo "acme/widgets";;
  *collaborators*) [ "${GH_STUB_COLLAB:-}" = "err" ] && { echo '{"message":"Forbidden"}' >&2; exit 1; }; echo "${GH_STUB_COLLAB:-1}";;
  *) exit 1;;
esac
GH
chmod +x "$STUB/gh"
G="$WORK/ghdetect"; mkdir -p "$G"; git_init "$G"; ( cd "$G" && git remote add origin https://github.com/acme/widgets.git ); printf 'x\n' > "$G/README.md"; git_commit "$G" init "2026-03-01T00:00:00Z"
out=$(SDLC_SKIP_GH_DETECT= PATH="$STUB:$PATH" GH_STUB_COLLAB=1 bash "$SCRIPTS/init.sh" --dir "$G" 2>&1)
[ -f "$G/.github/workflows/sdlc-review.yml" ] && ok "GitHub remote → workflows installed without --github" || bad "auto github install" "$out"
has "one collaborator → roles.solo auto" "$(cat "$G/.sdlc/config.json")" '"solo": true'
G2="$WORK/ghdetect2"; mkdir -p "$G2"; git_init "$G2"; ( cd "$G2" && git remote add origin https://github.com/acme/widgets.git ); printf 'x\n' > "$G2/README.md"; git_commit "$G2" init "2026-03-01T00:00:00Z"
out=$(SDLC_SKIP_GH_DETECT= PATH="$STUB:$PATH" GH_STUB_COLLAB=err bash "$SCRIPTS/init.sh" --dir "$G2" --no-github 2>&1)
[ ! -d "$G2/.github/workflows" ] && ok "--no-github suppresses the auto install" || bad "--no-github" "$(ls "$G2/.github/workflows")"
lacks "unknown collaborator count keeps team gates (fail closed)" "$(cat "$G2/.sdlc/config.json")" '"solo": true'; has "fail-closed note printed" "$out" "keeping team gates"
G3="$WORK/ghdetect3"; mkdir -p "$G3/.sdlc"; git_init "$G3"; ( cd "$G3" && git remote add origin https://github.com/acme/widgets.git ); printf '{"version":1,"roles":{"solo":false},"paths":{"intent":"intent","spec":"spec","plan":"plan","evals":"evals"}}\n' > "$G3/.sdlc/config.json"
out=$(SDLC_SKIP_GH_DETECT= PATH="$STUB:$PATH" GH_STUB_COLLAB=1 bash "$SCRIPTS/init.sh" --dir "$G3" --no-github 2>&1)
has "auto-detected solo does not override an explicit roles.solo:false" "$(cat "$G3/.sdlc/config.json")" '"solo": false'
has "existing config gains ci.auth" "$(cat "$G3/.sdlc/config.json")" '"auth"'; has "existing config gains default_branch" "$(cat "$G3/.sdlc/config.json")" '"default_branch"'
out=$(SDLC_SKIP_GH_DETECT= PATH="$STUB:$PATH" GH_STUB_COLLAB=5 bash "$SCRIPTS/init.sh" --dir "$G3" --no-github --solo 2>&1)
has "explicit --solo overrides roles.solo:false" "$(cat "$G3/.sdlc/config.json")" '"solo": true'
tf2="$WORK/ci-token2"; printf 'tok-xyz' > "$tf2"; mkdir -p "$WORK/oauth-auto"
out=$(SDLC_AUTH= SDLC_CI_TOKEN_FILE="$tf2" bash "$SCRIPTS/init.sh" --dir "$WORK/oauth-auto" --github 2>&1)
has "stored token → ci.auth oauth" "$(cat "$WORK/oauth-auto/.sdlc/config.json")" '"auth": "oauth"'; has "stored token → workflows use CLAUDE_CODE_OAUTH_TOKEN" "$(cat "$WORK/oauth-auto/.github/workflows/sdlc-evals.yml")" 'CLAUDE_CODE_OAUTH_TOKEN'
printf 'not json' > "$WORK/oauth-auto/.sdlc/config.json"; out=$(bash "$SCRIPTS/init.sh" --dir "$WORK/oauth-auto" --solo 2>&1); has "invalid config JSON is reported, not falsely merged" "$out" "SKIPPED (invalid)"
# --- existing sdlc workflow rendered with the other auth: only the auth line is merged; foreign workflows untouched ---
WA="$WORK/authline"; mkdir -p "$WA/.github/workflows"
printf 'name: sdlc evals\n# custom-marker\njobs:\n  e:\n    steps:\n      - run: x\n        env:\n          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n' > "$WA/.github/workflows/sdlc-evals.yml"
printf 'name: CI\njobs:\n  b:\n    steps:\n      - run: y\n        env:\n          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n' > "$WA/.github/workflows/ci.yml"
out=$(bash "$SCRIPTS/init.sh" --dir "$WA" --auth oauth 2>&1)
has "mismatched sdlc workflow auth line reported as MERGED" "$out" "MERGED"; has "auth line switched to oauth secret" "$(cat "$WA/.github/workflows/sdlc-evals.yml")" 'CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}'
has "rest of the workflow kept (marker)" "$(cat "$WA/.github/workflows/sdlc-evals.yml")" "custom-marker"; has "foreign workflow untouched" "$(cat "$WA/.github/workflows/ci.yml")" 'secrets.ANTHROPIC_API_KEY'
out=$(SDLC_SKIP_GH_DETECT=1 bash "$SCRIPTS/doctor.sh" --dir "$WA" 2>&1); has "doctor: CI auth consistent" "$out" "ci.auth=oauth, workflows use CLAUDE_CODE_OAUTH_TOKEN"
python3 - "$WA/.sdlc/config.json" <<'PY'
import json,sys; p=sys.argv[1]; c=json.load(open(p)); c['ci']['auth']='api'; json.dump(c,open(p,'w'),indent=2)
PY
out=$(SDLC_SKIP_GH_DETECT=1 bash "$SCRIPTS/doctor.sh" --dir "$WA" 2>&1); has "doctor warns when workflows disagree with ci.auth" "$out" "use CLAUDE_CODE_OAUTH_TOKEN but ci.auth=api"
out=$(bash "$SCRIPTS/init.sh" --dir "$WA" 2>&1); has "re-run init follows config ci.auth back to api" "$(cat "$WA/.github/workflows/sdlc-evals.yml")" 'secrets.ANTHROPIC_API_KEY'
# --- github-setup on a Free-plan private repo (stub: rulesets 403, repo PATCH ok): auto-merge is set before the ruleset fails ---
cat > "$STUB/gh" <<'GH'
#!/bin/bash
case "$*" in
  "auth status --hostname github.com"*) exit 0;;
  "auth status"*) echo "X github.kakaocorp.com: token expired" >&2; exit 1;;
  "repo view --json nameWithOwner --jq .nameWithOwner"*) echo "acme/widgets";;
  "repo view acme/widgets --json"*) echo '{"isPrivate":true,"defaultBranchRef":{"name":"main"},"owner":{"login":"acme"},"visibility":"PRIVATE"}';;
  *collaborators*) [ "${GH_STUB_COLLAB:-}" = "err" ] && { echo '{"message":"Forbidden"}' >&2; exit 1; }; echo "${GH_STUB_COLLAB:-1}";;
  "secret list"*) echo "CLAUDE_CODE_OAUTH_TOKEN  2026-09-07";;
  "secret set"*) cat >/dev/null; exit 0;;
  "api -X PATCH repos/acme/widgets"*) echo "PATCHED $*" >> "${GH_STUB_LOG:-/dev/null}"; exit 0;;
  *rulesets*) echo '{"message":"Upgrade to GitHub Pro or make this repository public to enable this feature.","status":"403"}' >&2; exit 1;;
  *) exit 1;;
esac
GH
chmod +x "$STUB/gh"; : > "$WORK/gh.log"
out=$(PATH="$STUB:$PATH" GH_STUB_LOG="$WORK/gh.log" SDLC_CI_TOKEN_FILE="$tf2" bash "$SCRIPTS/github-setup.sh" --repo acme/widgets --dir "$WORK/solo" --auth oauth 2>&1); rc=$?
check "github-setup exits non-zero when the ruleset is unavailable" "$([ $rc -ne 0 ] && echo true || echo false)" "$out"
has "auto-merge setting applied despite ruleset 403" "$(cat "$WORK/gh.log")" "allow_auto_merge=true"; has "ruleset failure explains the plan limit" "$out" "GitHub Pro"
[ "$(printf '%s\n' "$out" | grep -n 'REPO    allow_auto_merge' | cut -d: -f1)" -lt "$(printf '%s\n' "$out" | grep -n 'RULESET failed' | cut -d: -f1)" ] && ok "REPO settings run before the ruleset" || bad "order" "$out"
has "stored token is used for oauth" "$out" "using stored token ("
out=$(bash "$SCRIPTS/init.sh" --dir "$X" --solo 2>&1); has "--solo on an existing config merges roles.solo" "$(cat "$X/.sdlc/config.json")" '"solo": true'; has "existing config merge reported" "$out" "roles.solo=true"
[ -f "$PLUGIN/skills/go/SKILL.md" ] && ok "go skill present" || bad "go skill present" ""
if command -v gh >/dev/null 2>&1; then
  tf="$WORK/ci-token"; printf 'tok-abc123' > "$tf"
  out=$(SDLC_CI_TOKEN_FILE="$tf" bash "$SCRIPTS/github-setup.sh" --repo octo/demo --dry-run --dir "$WORK/solo" 2>&1)
  has "github-setup takes auth from config ci.auth" "$out" "auth=api"
  has "stored subscription token is not registered as an API key" "$out" "not used as ANTHROPIC_API_KEY"; lacks "api mode does not pick the stored token" "$out" "using stored token ("
  has "github-setup dry-run announces auto-merge setting" "$out" "allow_auto_merge=true"
  lacks "no admin bypass without --bypass" "$out" '"actor_id":5'
  out=$(SDLC_CI_TOKEN_FILE="$tf" bash "$SCRIPTS/github-setup.sh" --repo octo/demo --dry-run --dir "$WORK/solo" --bypass --auth oauth 2>&1)
  has "--bypass adds the admin bypass" "$out" '"actor_id":5'
  has "github-setup resolves a stored token (keychain or file)" "$out" "using stored token ("
  has "github-setup dry-run shows the ruleset payload" "$out" '"type": "required_status_checks"'
  lacks "github-setup never prints the token" "$out" "tok-abc123"
fi
[ -f "$X/.sdlc/APPROVALS.md" ] && ok "APPROVALS.md register created" || bad "APPROVALS.md created" ""
[ -f "$X/.sdlc/examples/intent.md" ] && [ -f "$X/.sdlc/examples/production-gate.sh" ] && ok "playbook examples copied to .sdlc/examples" || bad "examples copied" "$(ls "$X/.sdlc" 2>/dev/null)"
has "example intent.md is the article's" "$(cat "$X/.sdlc/examples/intent.md")" "Author: J. Ortiz (claims operations). Status: draft."
has ".gitignore appended" "$(cat "$X/.gitignore")" ".sdlc/logs/"; has ".gitignore original kept" "$(cat "$X/.gitignore")" "node_modules/"
mkdir -p "$WORK/ko"; out=$(bash "$SCRIPTS/init.sh" --dir "$WORK/ko" --lang ko 2>&1); has "--lang ko uses Korean template" "$(cat "$WORK/ko/intent/TEMPLATE.md" 2>/dev/null)" "문제"
has "--lang ko sets config language" "$(cat "$WORK/ko/.sdlc/config.json" 2>/dev/null)" '"language": "ko"'

# ---------------------------------------------------------------- doctor
echo "-- doctor"
out=$(bash "$SCRIPTS/doctor.sh" --dir "$X" 2>&1); rc=$?
check "doctor exits 0" "$([ $rc -eq 0 ] && echo true || echo false)" "$out"; has "doctor shows git ✓" "$out" "git repository"; has "doctor lists next steps" "$out" "Next steps"
bash "$SCRIPTS/doctor.sh" --dir "$X" --json > "$WORK/doctor.json" 2>/dev/null; py_json_ok "$WORK/doctor.json" && ok "doctor --json parses" || bad "doctor --json parses" "$(head -c 300 "$WORK/doctor.json")"
out=$(bash "$SCRIPTS/doctor.sh" --dir "$WORK/nothing-here" 2>&1); has "doctor on uninitialized dir points to init" "$out" "init"

# ---------------------------------------------------------------- status + metrics on a chain repo
echo "-- status / metrics"
C="$WORK/chain"; mkdir -p "$C"; git_init "$C"; bash "$SCRIPTS/init.sh" --dir "$C" >/dev/null 2>&1; mkdir -p "$C/.sdlc/state"; git_commit "$C" "init" "2026-02-01T09:00:00Z"
mk() { # <kind> <slug> <status> <created> [extra]
  printf -- '---\ntype: %s\nslug: %s\ntitle: %s\nstatus: %s\nauthor: t\ncreated: %s\n%s\n---\n# %s %s\n\nBody of the %s for %s: %s %s %s\n' "$1" "$2" "$2" "$3" "$4" "${5:-links: {}}" "$1" "$2" "$1" "$2" "$1$1$1" "$2$2" "$3$3$3" > "$C/$1/$2.md"
}
mk intent claims-status draft 2026-02-01; git_commit "$C" "intent(claims-status)" "2026-02-01T10:00:00Z"
mk intent claims-status approved 2026-02-01; git_commit "$C" "intent(claims-status): approve" "2026-02-02T10:00:00Z"
mk spec claims-status approved 2026-02-03; git_commit "$C" "spec(claims-status)" "2026-02-03T10:00:00Z"
mk plan claims-status approved 2026-02-04 "test_changes: forbidden"; printf 'plan/claims-status.md' > "$C/.sdlc/state/active-plan"; git_commit "$C" "plan(claims-status)" "2026-02-04T10:00:00Z"
mk spec claims-status approved 2026-02-03; printf '\nR9. late requirement\n' >> "$C/spec/claims-status.md"; git_commit "$C" "spec(claims-status): rework" "2026-02-05T10:00:00Z"
mk intent retry-policy rejected 2026-02-06 'reason: "duplicate"'; git_commit "$C" "intent(retry-policy): reject" "2026-02-06T10:00:00Z"
out=$(bash "$SCRIPTS/status.sh" --dir "$C" 2>&1); rc=$?
check "status exits 0" "$([ $rc -eq 0 ] && echo true || echo false)" "$out"; has "status lists the slug" "$out" "claims-status"; has "status shows rework 1" "$(printf '%s' "$out" | grep claims-status)" "1"
out=$(bash "$SCRIPTS/status.sh" claims-status --dir "$C" 2>&1); has "status <slug> shows the spec rework commit" "$out" "rework"
has "status shows commit dates (BSD/GNU date)" "$(bash "$SCRIPTS/status.sh" --dir "$C" 2>&1 | grep claims-status)" "2026-02-0"
out=$(python3 "$SCRIPTS/metrics.py" --dir "$C" --since 5y 2>&1); rc=$?
check "metrics exits 0" "$([ $rc -eq 0 ] && echo true || echo false)" "$out"; has "metrics has Stage 1 table" "$out" "Stage 1"; has "metrics survival rate computed" "$out" "Survival rate"
has "metrics counts spec rework after plan" "$(printf '%s' "$out" | grep 'Requirements rework')" "| 1 |"
has "metrics intent→spec elapsed" "$(printf '%s' "$out" | grep 'intent.md commit → spec.md commit')" "| 48"
python3 "$SCRIPTS/metrics.py" --dir "$C" --format json > "$WORK/metrics.json" 2>/dev/null; py_json_ok "$WORK/metrics.json" && ok "metrics --format json parses" || bad "metrics json" "$(head -c 200 "$WORK/metrics.json")"
mkdir -p "$C/.sdlc/logs"; now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '{"ts":"%s","event":"verify.run","session_id":"a","decision":"pass","reason":"hook"}\n{"ts":"%s","event":"verify.run","session_id":"a","decision":"allow","reason":"skill"}\n{"ts":"%s","event":"verify.run","session_id":"a","decision":"fail","reason":"hook"}\n' "$now" "$now" "$now" >> "$C/.sdlc/logs/events.jsonl"
has "metrics counts hook pass and skill allow as success (2/3)" "$(python3 "$SCRIPTS/metrics.py" --dir "$C" 2>&1 | grep 'Local verify runs pass rate')" "| 0.67 |"
printf '{"ts":"%s","event":"go.run","session_id":"a","decision":"info","reason":"skill","detail":{"slug":"x","autopilot":true,"duration_min":42}}\n{"ts":"%s","event":"go.run","session_id":"a","decision":"info","reason":"skill","detail":{"slug":"y","duration_min":18}}\n' "$now" "$now" >> "$C/.sdlc/logs/events.jsonl"
has "metrics shows request→PR median from go.run" "$(python3 "$SCRIPTS/metrics.py" --dir "$C" 2>&1 | grep 'Request → PR')" "| 30 (2) |"
# --- evals secret check: copied config is excluded exactly, agent-written files are judged ---
CK="$WORK/ck-work"; mkdir -p "$CK/.claude" "$CK.meta"; printf 'password: Hunter2Hunter2\n' > "$CK/CLAUDE.md"; printf 'CLAUDE.md\n' > "$CK.meta/copied.txt"
bash "$PLUGIN/templates/evals/cases/no-secret-in-code/check.sh" "$CK" >/dev/null 2>&1 && ok "check.sh ignores the copied CLAUDE.md" || bad "check.sh copied exclusion" "flagged copied file"
printf 'url = "postgres://app:Hunter2Hunter2@db/x"\n' > "$CK/db.py"
bash "$PLUGIN/templates/evals/cases/no-secret-in-code/check.sh" "$CK" >/dev/null 2>&1 && bad "check.sh must flag agent code" "passed" || ok "check.sh flags a credential in agent-written code"
rm "$CK/db.py"; printf 'x\n' > "$CK.meta/copied.txt"
bash "$PLUGIN/templates/evals/cases/no-secret-in-code/check.sh" "$CK" >/dev/null 2>&1 && bad "check.sh must flag an agent-written CLAUDE.md" "passed" || ok "check.sh flags CLAUDE.md when the agent (not the harness) wrote it"
out=$(bash "$SCRIPTS/doctor.sh" --dir "$C" 2>&1); has "doctor reports auto mode readiness" "$out" "auto mode readiness"; has "doctor next steps carry layer tags" "$out" "[1 start]"

# ---------------------------------------------------------------- run-evals (dry) and monitor
echo "-- run-evals / monitor"
out=$(bash "$SCRIPTS/run-evals.sh" --dry-run --dir "$E" 2>&1); rc=$?
check "run-evals --dry-run exits 0" "$([ $rc -eq 0 ] && echo true || echo false)" "$out"; has "dry-run lists 3 cases" "$out" "3 case(s) would run"; has "dry-run shows the claude command" "$out" "--output-format json"
out=$(bash "$SCRIPTS/run-evals.sh" --dry-run --dir "$E" --case 'no-*' 2>&1); has "--case filters" "$out" "1 case(s) would run"
out=$(bash "$SCRIPTS/run-evals.sh" --dry-run --dir "$WORK/nothing-here" 2>&1); check "run-evals without cases exits 2" "$([ $? -ne 0 ] && echo true || echo false)" "$out"
out=$(python3 "$SCRIPTS/monitor.py" --selftest 2>&1); has "monitor selftest PASS" "$out" "== selftest PASS"
mkdir -p "$E/.sdlc/history"; python3 - "$E/.sdlc/history/eval_pass_rate.jsonl" <<'PY'
import json,sys
vals=[1.0,0.98,1.0,0.99,1.0,0.98,1.0,0.99,1.0,0.98,0.6]
open(sys.argv[1],'w').write("".join(json.dumps({"ts":f"2026-09-{i+1:02d}T02:00:00Z","value":v})+"\n" for i,v in enumerate(vals)))
PY
before=$(tree_sum "$E"); out=$(python3 "$SCRIPTS/monitor.py" --dry-run --dir "$E" --metric eval_pass_rate 2>&1); after=$(tree_sum "$E")
has "monitor detects 3sigma on synthetic breach" "$out" "3sigma"; has "monitor tier 3 → propose" "$out" "propose"; has "dry-run does not invoke claude" "$out" "DRY would"
check "monitor --dry-run writes nothing" "$([ "$before" = "$after" ] && echo true || echo false)" "$out"
lacks "monitor dry-run creates no triage intent" "$(ls "$E/intent/triage" 2>/dev/null)" ".md"

# ---------------------------------------------------------------- run-loop (/sdlc:run)
echo "-- run-loop"
L="$WORK/loop"; mkdir -p "$L"; git_init "$L"; printf 'x\n' > "$L/README.md"; git_commit "$L" init "2026-03-01T00:00:00Z"
bash "$SCRIPTS/init.sh" --dir "$L" --solo --no-github >/dev/null 2>&1
has "init enables the loop on a solo repository" "$(cat "$L/.sdlc/config.json")" '"enabled": true'
mk_intent() { printf -- '---\ntype: intent\nslug: %s\ntitle: %s\nstatus: %s\ncreated: %s\n---\n# %s\n' "$1" "$2" "$3" "$4" "$2" > "$L/intent/$1.md"; }
mk_intent a-first "First thing" approved 2026-03-01; mk_intent b-second "Second thing" approved 2026-03-02; mk_intent c-draft "Not yet" draft 2026-03-03; mk_intent d-done "Already done" approved 2026-03-04
printf -- '---\ntype: plan\nslug: d-done\nstatus: implemented\n---\n' > "$L/plan/d-done.md"
git_commit "$L" intents "2026-03-05T00:00:00Z"
setcfg() { python3 - "$L/.sdlc/config.json" "$2" "$3" <<'PY'
import json,sys; p,path,val=sys.argv[1:4]; c=json.load(open(p)); d=c
for k in path.split('.')[:-1]: d=d.setdefault(k,{})
d[path.split('.')[-1]]=json.loads(val); json.dump(c,open(p,'w'),indent=2)
PY
}
setcfg "$L" loop.enabled false; git_commit "$L" off "2026-03-05T00:30:00Z"
out=$(bash "$SCRIPTS/run-loop.sh" --dir "$L" --dry-run 2>&1); rc=$?
check "run-loop refuses while loop.enabled is false" "$([ $rc -eq 2 ] && echo true || echo false)" "$out"; has "refusal names the switch" "$out" "loop.enabled=false"
setcfg "$L" loop.enabled true; git_commit "$L" enable "2026-03-05T01:00:00Z"
out=$(bash "$SCRIPTS/run-loop.sh" --dir "$L" --dry-run 2>&1); rc=$?
check "dry-run exits 0" "$([ $rc -eq 0 ] && echo true || echo false)" "$out"
has "queue: first approved intent in created order" "$out" 'WOULD RUN  claude -p "/sdlc:go --slug a-first --autopilot --merge First thing"'
has "queue: second approved intent" "$out" '--slug b-second'
lacks "draft intent not queued" "$out" "c-draft"; lacks "implemented plan not queued" "$out" "d-done"
has "dry-run announces the self-check" "$out" "WOULD SELF-CHECK"
out=$(bash "$SCRIPTS/run-loop.sh" --dir "$L" --dry-run --max-items 1 2>&1); lacks "--max-items 1 stops after one" "$out" "b-second"; has "stop reason max_items" "$out" "stop=max_items"
touch "$L/.sdlc/state/pause"; out=$(bash "$SCRIPTS/run-loop.sh" --dir "$L" --dry-run 2>&1); has "pause file stops the loop" "$out" "PAUSED"; rm "$L/.sdlc/state/pause"
setcfg "$L" roles.solo false
out=$(bash "$SCRIPTS/run-loop.sh" --dir "$L" --dry-run 2>&1); rc=$?; check "team repository refused" "$([ $rc -eq 2 ] && echo true || echo false)" "$out"; has "refusal names solo" "$out" "solo-only"
setcfg "$L" roles.solo true; git_commit "$L" solo "2026-03-05T02:00:00Z"
printf 'y\n' > "$L/dirty.txt"; out=$(bash "$SCRIPTS/run-loop.sh" --dir "$L" --dry-run 2>&1); has "dirty tree refused" "$out" "not clean"; rm "$L/dirty.txt"
# real run with stubs: claude is a no-op that records its prompt; gh reports PR state per GH_STUB_MERGED
cat > "$STUB/claude" <<'CL'
#!/bin/bash
printf '%s\n' "$*" >> "${CL_STUB_LOG:-/dev/null}"; case "$*" in *self-check*) echo 'SDLC_SELFCHECK clean';; esac; exit 0
CL
chmod +x "$STUB/claude"
cat > "$STUB/gh" <<'GH'
#!/bin/bash
case "$*" in
  "auth status --hostname github.com"*) exit 0;;
  "auth status"*) echo "X github.kakaocorp.com: token expired" >&2; exit 1;;
  "pr list --head sdlc/"*"--state merged"*) h=$(printf '%s' "$*" | sed -E 's/.*--head sdlc\/([^ ]+).*/\1/'); [ "${GH_STUB_MODE:-}" = merge ] && grep -q -- "--slug $h " "${CL_STUB_LOG:-/dev/null}" 2>/dev/null && echo 7;;
  "pr list"*) ;;
  *) exit 1;;
esac
GH
chmod +x "$STUB/gh"; : > "$WORK/cl.log"
out=$(PATH="$STUB:$PATH" CL_STUB_LOG="$WORK/cl.log" GH_STUB_MODE=merge bash "$SCRIPTS/run-loop.sh" --dir "$L" --claude-bin "$STUB/claude" 2>&1); rc=$?
check "loop run exits 0 when everything merged" "$([ $rc -eq 0 ] && echo true || echo false)" "$out"
has "both items merged" "$out" "merged=2"; has "self-check ran and was clean" "$out" "self_check=clean"; has "stopped because the queue emptied" "$out" "stop=queue empty"
has "go invoked with --autopilot --merge and the intent title" "$(cat "$WORK/cl.log")" "/sdlc:go --slug a-first --autopilot --merge First thing"
check "each item ran in its own -p session (2 go calls)" "$([ "$(grep -c -- '-p /sdlc:go' "$WORK/cl.log")" -eq 2 ] && echo true || echo false)" "$(cat "$WORK/cl.log")"
has "loop.run event logged" "$(cat "$L/.sdlc/logs/events.jsonl")" '"event":"loop.run"'; has "loop.item events logged" "$(cat "$L/.sdlc/logs/events.jsonl")" '"event":"loop.item"'
# failure path: nothing merges → failures counted, slug blocked after max_failures_per_slug, skipped next run
setcfg "$L" loop.max_failures_per_slug 1; git_commit "$L" cfg "2026-03-05T03:00:00Z"
out=$(PATH="$STUB:$PATH" CL_STUB_LOG="$WORK/cl2.log" GH_STUB_MODE=fail bash "$SCRIPTS/run-loop.sh" --dir "$L" --claude-bin "$STUB/claude" 2>&1); rc=$?
check "loop run exits 1 when items failed" "$([ $rc -eq 1 ] && echo true || echo false)" "$out"
has "failed items counted" "$out" "failed=2"; has "slug blocked after max_failures_per_slug" "$out" "BLOCKED a-first"
has "block recorded in state file" "$(cat "$L/.sdlc/state/loop-failures.txt")" "a-first 1"
out=$(bash "$SCRIPTS/run-loop.sh" --dir "$L" --dry-run 2>&1); has "blocked slugs are skipped next run" "$out" "blocked=[a-first b-second]"
out=$(SDLC_SKIP_GH_DETECT=1 bash "$SCRIPTS/doctor.sh" --dir "$L" 2>&1); has "doctor reports the loop row" "$out" "autopilot loop (/sdlc:run)"

echo "== $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
