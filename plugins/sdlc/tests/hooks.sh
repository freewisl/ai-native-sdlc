#!/bin/bash
# sdlc plugin — hook test matrix. Run from anywhere:  bash tests/hooks.sh [-v] [name-filter]
# Prints PASS/FAIL per check, a summary line, and exits 1 when any check failed. Needs bash 3.2+, jq or python3, git.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; PLUGIN="$(cd "$HERE/.." && pwd)"; FIX="$HERE/fixtures/hooks"
. "$PLUGIN/scripts/lib/common.sh"
VERBOSE=false; FILTER=""
for a in "$@"; do case "$a" in -v) VERBOSE=true;; *) FILTER="$a";; esac; done
WORK=$(mktemp -d "${TMPDIR:-/tmp}/sdlc-hooks.XXXXXX"); trap 'rm -rf "$WORK"' EXIT
unset RELEASE_APPROVAL SDLC_ALLOW_TEST_EDIT SDLC_FORMAT_TIMEOUT
PASS=0; FAIL=0; SLOW=""; N=0; MAX_MS=0; MAX_MS_HOOK=""
SID="test-session"

# ---------- infrastructure ----------
now_ms() { perl -MTime::HiRes=time -e 'printf("%d\n", time()*1000)' 2>/dev/null || echo $(( $(date +%s) * 1000 )); }
# NOTE: called as $(fresh) — a subshell — so a counter would never advance in the parent; mktemp gives a unique dir each time.
fresh() { local d; d=$(mktemp -d "$WORK/pXXXXXX"); cp -R "$FIX/project-init/." "$d/"; printf '%s' "$d"; }
fresh_plain() { local d; d=$(mktemp -d "$WORK/pXXXXXX"); cp -R "$FIX/project-plain/." "$d/"; printf '%s' "$d"; }
# set_cfg <root> <dotted.path> <json-value>
set_cfg() {
  local f="$1/.sdlc/config.json"
  if has_cmd jq; then
    jq --argjson v "$3" "setpath(\"$2\" | split(\".\"); \$v)" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  else
    python3 - "$f" "$2" "$3" <<'PY'
import json,sys
f,p,v=sys.argv[1:4]; d=json.load(open(f)); cur=d; parts=p.split('.')
for k in parts[:-1]: cur=cur.setdefault(k,{})
cur[parts[-1]]=json.loads(v); json.dump(d,open(f,'w'),indent=2)
PY
  fi
}
# run_hook <hook> <root> <json> [VAR=val ...]  → OUT ERR RC MS
run_hook() {
  local hook="$1" root="$2" json="$3"; shift 3
  local t0 t1
  t0=$(now_ms)
  OUT=$(printf '%s' "$json" | env CLAUDE_PROJECT_DIR="$root" CLAUDE_PLUGIN_ROOT="$PLUGIN" "$@" bash "$PLUGIN/scripts/hooks/$hook.sh" 2>"$WORK/.err"); RC=$?
  t1=$(now_ms); MS=$((t1 - t0)); ERR=$(cat "$WORK/.err")
  [ "$MS" -gt "$MAX_MS" ] && { MAX_MS=$MS; MAX_MS_HOOK=$hook; }
  case "$*" in *SDLC_FORMAT_TIMEOUT=*) ;; *) [ "$MS" -ge 1000 ] && SLOW="$SLOW $hook:${MS}ms";; esac   # the deliberate hung-formatter case is capped at ~1s by design
  $VERBOSE && printf '      [%s rc=%s %sms] out=%s err=%s\n' "$hook" "$RC" "$MS" "$(printf '%s' "$OUT" | head -c 200 | tr '\n' '|')" "$(printf '%s' "$ERR" | head -c 200 | tr '\n' '|')"
  return 0
}
skip_case() { [ -n "$FILTER" ] && case "$1" in *"$FILTER"*) return 1;; *) return 0;; esac; return 1; }
ok()   { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL  %s\n      %s\n' "$1" "$2"; }
assert_rc()      { skip_case "$1" && return; if [ "$RC" = "$2" ]; then ok "$1"; else bad "$1" "exit=$RC want=$2 | stderr: $(printf '%s' "$ERR" | head -c 240 | tr '\n' ' ') | stdout: $(printf '%s' "$OUT" | head -c 160 | tr '\n' ' ')"; fi; }
assert_has()     { skip_case "$1" && return; case "$2" in *"$3"*) ok "$1";; *) bad "$1" "missing '$3' in: $(printf '%s' "$2" | head -c 300 | tr '\n' ' ')";; esac; }
assert_lacks()   { skip_case "$1" && return; case "$2" in *"$3"*) bad "$1" "unexpected '$3' in: $(printf '%s' "$2" | head -c 300 | tr '\n' ' ')";; *) ok "$1";; esac; }
assert_file_has(){ skip_case "$1" && return; if [ -f "$2" ] && grep -q -- "$3" "$2" 2>/dev/null; then ok "$1"; else bad "$1" "no '$3' in $2: $(cat "$2" 2>/dev/null | head -c 300 | tr '\n' ' ')"; fi; }
assert_true()    { skip_case "$1" && return; if [ "$2" = true ]; then ok "$1"; else bad "$1" "$3"; fi; }
state_of() { json_file_get "$1/.sdlc/state/session-$SID.json" ".$2"; }

# ---------- stdin builders ----------
base() { printf '"session_id":"%s","transcript_path":"/dev/null","cwd":%s,"permission_mode":"default"' "$SID" "$(json_escape "$1")"; }
j_edit()   { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Edit","tool_use_id":"t1","tool_input":{"file_path":%s,"old_string":"x","new_string":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")"; }
j_write()  { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Write","tool_use_id":"t1","tool_input":{"file_path":%s,"content":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")"; }
j_multi()  { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"MultiEdit","tool_use_id":"t1","tool_input":{"file_path":%s,"edits":[{"old_string":"a","new_string":"safe text"},{"old_string":"b","new_string":%s}]}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")"; }
j_nb()     { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"NotebookEdit","tool_use_id":"t1","tool_input":{"notebook_path":%s,"cell_id":"c1","new_source":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")"; }
j_bash()   { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Bash","tool_use_id":"t1","tool_input":{"command":%s,"description":"test"}}' "$(base "$1")" "$(json_escape "$2")"; }
j_postbash(){ printf '{%s,"hook_event_name":"PostToolUse","tool_name":"Bash","tool_use_id":"t1","tool_input":{"command":%s},"tool_response":{"stdout":%s,"stderr":%s,"interrupted":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")" "$(json_escape "$4")" "${5:-false}"; }
j_postedit(){ printf '{%s,"hook_event_name":"PostToolUse","tool_name":"Edit","tool_use_id":"t1","tool_input":{"file_path":%s,"old_string":"x","new_string":"y"},"tool_response":{"filePath":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$2")"; }
j_stop()   { printf '{%s,"hook_event_name":"Stop","stop_hook_active":%s,"last_assistant_message":"done"}' "$(base "$1")" "$2"; }
j_session(){ printf '{%s,"hook_event_name":"%s","source":"startup"}' "$(base "$1")" "$2"; }

AWS_KEY="AKIAQ7Z2M9R4T1B8C5D3"                 # fake, no allowlist token inside
PEM_HDR="-----BEGIN RSA PRIVATE KEY-----"

echo "== sdlc hook tests  (plugin: $PLUGIN, work: $WORK, jq: $(command -v jq >/dev/null && echo yes || echo no), python3: $(command -v python3 >/dev/null && echo yes || echo no))"

# ======================================================================
echo "-- guard-edit: protected paths"
P=$(fresh)
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/gen/client.txt" "edited")"
assert_rc "frozen path blocked (src/gen/)" 2
assert_has "frozen path message names the list file" "$ERR" "frozen-paths.txt"
assert_has "frozen path message has BLOCKED prefix" "$ERR" "[sdlc] BLOCKED"
run_hook guard-edit "$P" "$(j_write "$P" "$P/schemas/a.avsc" "{}")"
assert_rc "frozen glob blocked (schemas/*.avsc, trailing comment ignored)" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "print(1)")"
assert_rc "ordinary source edit allowed" 0
assert_true "allowed edit is silent" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
assert_true "block was logged to events.jsonl" "$(grep -q '"event":"hook.guard".*"decision":"deny".*frozen_path' "$P/.sdlc/logs/events.jsonl" && echo true || echo false)" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
assert_true "allowed edit was not logged" "$([ "$(grep -c 'hook.guard' "$P/.sdlc/logs/events.jsonl")" = 2 ] && echo true || echo false)" "$(cat "$P/.sdlc/logs/events.jsonl")"
set_cfg "$P" protect.generated_paths '["build/", "**/dist/**"]'
run_hook guard-edit "$P" "$(j_write "$P" "$P/build/out.js" "x")"
assert_rc "generated path blocked (build/)" 2
assert_has "generated path message names the config key" "$ERR" "protect.generated_paths"
run_hook guard-edit "$P" "$(j_write "$P" "$P/pkg/dist/bundle.js" "x")"
assert_rc "generated glob blocked (**/dist/**)" 2

# ======================================================================
echo "-- guard-edit: test-file lock"
P=$(fresh)
for f in tests/x_test.py src/test/Foo.java foo.spec.ts lib/__tests__/a.js pkg/thing_test.go app/models/user_spec.rb; do
  run_hook guard-edit "$P" "$(j_edit "$P" "$P/$f" "changed")"
  assert_rc "test path blocked: $f" 2
done
assert_has "test lock message gives the route" "$ERR" "SDLC_ALLOW_TEST_EDIT=1"
assert_has "test lock message names the plan" "$ERR" "plan/demo.md"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/spec/demo.md" "changed")"
assert_rc "spec/ artifact dir is not treated as a test dir" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")" SDLC_ALLOW_TEST_EDIT=1
assert_rc "test edit allowed via SDLC_ALLOW_TEST_EDIT=1" 0
touch "$P/.sdlc/state/allow-test-edit"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "test edit allowed via .sdlc/state/allow-test-edit" 0
rm -f "$P/.sdlc/state/allow-test-edit"
printf 'plan/allowed.md' > "$P/.sdlc/state/active-plan"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "test edit allowed via plan frontmatter test_changes: allowed" 0
printf 'plan/demo.md' > "$P/.sdlc/state/active-plan"
set_cfg "$P" protect.protect_tests false
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "protect_tests=false disables the lock (false must be honored)" 0
set_cfg "$P" protect.protect_tests true
set_cfg "$P" protect.test_paths '["qa/"]'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/qa/smoke.sh" "changed")"
assert_rc "custom test_paths prefix blocked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "custom test_paths replaces the auto set" 0

# ======================================================================
echo "-- guard-edit: ticketed paths (change ticket)"
P=$(fresh); set_cfg "$P" protect.ticketed_paths '["migrations/", "infra/**"]'
run_hook guard-edit "$P" "$(j_write "$P" "$P/migrations/002_add_index.sql" "create index")"
assert_rc "migration edit without CHANGE_TICKET blocked" 2
assert_has "ticketed message names the env var" "$ERR" "CHANGE_TICKET"
run_hook guard-edit "$P" "$(j_write "$P" "$P/infra/prod/main.tf" "resource")"
assert_rc "infra glob edit without ticket blocked" 2
run_hook guard-edit "$P" "$(j_write "$P" "$P/migrations/002_add_index.sql" "create index")" CHANGE_TICKET=CHG-42
assert_rc "migration edit with CHANGE_TICKET allowed" 0
set_cfg "$P" protect.ticket_env '"CAB_APPROVAL"'
run_hook guard-edit "$P" "$(j_write "$P" "$P/migrations/003.sql" "x")" CHANGE_TICKET=CHG-42
assert_rc "custom ticket_env: default var no longer unlocks" 2
run_hook guard-edit "$P" "$(j_write "$P" "$P/migrations/003.sql" "x")" CAB_APPROVAL=ok
assert_rc "custom ticket_env unlocks" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x")"
assert_rc "non-ticketed path unaffected" 0

# ======================================================================
echo "-- guard-edit: approved plan required"
P=$(fresh); set_cfg "$P" plan.require_for_edits true
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x")"
assert_rc "approved active plan → edit allowed" 0
printf 'plan/draft.md' > "$P/.sdlc/state/active-plan"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x")"
assert_rc "draft plan → edit blocked" 2
assert_has "plan message explains status" "$ERR" "status 'draft'"
rm -f "$P/.sdlc/state/active-plan"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x")"
assert_rc "no active plan → edit blocked" 2
assert_has "plan message routes to /sdlc:plan" "$ERR" "/sdlc:plan"
run_hook guard-edit "$P" "$(j_write "$P" "$P/intent/new-idea.md" "# idea")"
assert_rc "exempt path (intent/) allowed without plan" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/CLAUDE.md" "# notes")"
assert_rc "exempt file (CLAUDE.md) allowed without plan" 0

# ======================================================================
echo "-- guard-edit: secret scan"
P=$(fresh)
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "aws_key = '$AWS_KEY'")"
assert_rc "AWS key in new_string blocked" 2
assert_has "secret message names a pattern" "$ERR" "pattern#"
assert_lacks "secret value is never echoed" "$ERR" "$AWS_KEY"
run_hook guard-edit "$P" "$(j_write "$P" "$P/src/key.pem" "$PEM_HDR
MIIEow...")"
assert_rc "private key header in Write content blocked" 2
run_hook guard-edit "$P" "$(j_multi "$P" "$P/src/app.py" "token = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'")"
assert_rc "GitHub token inside MultiEdit edits[] blocked" 2
run_hook guard-edit "$P" "$(j_nb "$P" "$P/nb.ipynb" "os.environ['X']='xoxb-123456789012-ABCDEFGHIJKLMNOP'")"
assert_rc "Slack token in NotebookEdit new_source blocked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "aws_key = 'AKIAIOSFODNN7EXAMPLE'")"
assert_rc "allowlisted placeholder (EXAMPLE) allowed" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "password = \"REPLACE_ME_WITH_REAL\"")"
assert_rc "allowlisted placeholder (REPLACE_ME) allowed" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/application.yml" "password: \"\${DB_PASSWORD}\"")"
assert_rc "template reference \${VAR} allowed" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "password = \"hunter2hunter2hunter2\"")"
assert_rc "generic password literal blocked" 2
run_hook guard-edit "$P" "$(j_write "$P" "$P/evals/cases/no-secret/fixture/bad.py" "aws_key = '$AWS_KEY'")"
assert_rc "evals fixture path exempt from secret scan" 0
set_cfg "$P" secrets.extra_patterns '["ACME-[0-9]{8}"]'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "id = 'ACME-12345678'")"
assert_rc "extra_patterns from config applied" 2
set_cfg "$P" secrets.scan_edits false
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "aws_key = '$AWS_KEY'")"
assert_rc "scan_edits=false disables the scan" 0

# ======================================================================
echo "-- guard-bash: environment gate"
P=$(fresh)
run_hook guard-bash "$P" "$(j_bash "$P" "kubectl apply -f deploy.yaml -n production")"
assert_rc "production deploy without approval blocked" 2
assert_has "gate message names the approval env" "$ERR" "RELEASE_APPROVAL"
assert_file_has "gate.log has a deny line" "$P/.sdlc/logs/gate.log" "	deny	production	"
run_hook guard-bash "$P" "$(j_bash "$P" "./deploy.sh --env prod")" RELEASE_APPROVAL=REL-4711
assert_rc "production deploy with RELEASE_APPROVAL allowed" 0
assert_file_has "gate.log has an allow line" "$P/.sdlc/logs/gate.log" "	allow	production	./deploy.sh --env prod"
assert_true "events.jsonl has hook.gate allow" "$(grep -q '"event":"hook.gate".*"decision":"allow"' "$P/.sdlc/logs/events.jsonl" && echo true || echo false)" "$(cat "$P/.sdlc/logs/events.jsonl")"
run_hook guard-bash "$P" "$(j_bash "$P" "helm upgrade api ./chart --namespace staging")"
assert_rc "staging deploy exits 0" 0
assert_has "staging deploy returns ask JSON" "$OUT" '"permissionDecision":"ask"'
assert_has "staging ask JSON is PreToolUse output" "$OUT" '"hookEventName":"PreToolUse"'
assert_file_has "gate.log has an ask line" "$P/.sdlc/logs/gate.log" "	ask	staging	"
for c in "production deploy" "./production-deploy.sh" "prod-deploy.sh --now" "npm run deploy:production"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "production word before deploy word still gated: $c" 2
done
run_hook guard-bash "$P" "$(j_bash "$P" "grep -rn production docs/")"
assert_rc "reading about production is not a deploy" 0
run_hook guard-bash "$P" "$(j_bash "$P" "make deploy ENV=dev")"
assert_rc "dev deploy allowed (no pattern)" 0
assert_true "dev deploy is silent" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
set_cfg "$P" environments.dev.patterns '["deploy[^|;&]*dev"]'
run_hook guard-bash "$P" "$(j_bash "$P" "make deploy ENV=dev")"
assert_rc "dev deploy with pattern + autonomy free allowed" 0
assert_file_has "gate.log has allow dev line" "$P/.sdlc/logs/gate.log" "	allow	dev	"
run_hook guard-bash "$P" "$(j_bash "$P" "ls -la && git status")"
assert_rc "ordinary command allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'release notes for production launch'")"
assert_rc "commit message mentioning production is a gate false positive (documented)" 2
set_cfg "$P" commands.rollback '"./scripts/rollback.sh"'
run_hook guard-bash "$P" "$(j_bash "$P" "./scripts/rollback.sh --deploy-target production")"
assert_rc "rollback command always allowed even when it matches production" 0
run_hook guard-bash "$P" "$(j_bash "$P" "echo ./scripts/rollback.sh && kubectl apply -f x -n production")"
assert_rc "a command that merely mentions the rollback path is still gated" 2
assert_file_has "gate.log has allow rollback line" "$P/.sdlc/logs/gate.log" "	allow	rollback	"
set_cfg "$P" gate.mode '"ask"'
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")"
assert_rc "gate.mode=ask exits 0" 0
assert_has "gate.mode=ask returns ask JSON for production" "$OUT" '"permissionDecision":"ask"'
set_cfg "$P" gate.mode '"deny"'
set_cfg "$P" environments.production.approval_env '"CHANGE_TICKET"'
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")" CHANGE_TICKET=CHG-1
assert_rc "custom approval_env honored" 0
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")" RELEASE_APPROVAL=x
assert_rc "default approval env ignored when approval_env is custom" 2

# ======================================================================
echo "-- guard-bash: no direct push to the default branch"
P=$(fresh); ( cd "$P" && git init -q -b main . && git config user.email t@t && git config user.name t && git add -A >/dev/null && git commit -qm init ) 2>/dev/null
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin main")"
assert_rc "git push origin main blocked" 2
assert_has "direct push message routes to a PR" "$ERR" "gh pr create"
run_hook guard-bash "$P" "$(j_bash "$P" "git push -u origin HEAD:main")"
assert_rc "git push HEAD:main blocked" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git push")"
assert_rc "bare git push while on main blocked" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git push -u origin feature/x")"
assert_rc "push to a feature branch allowed" 0
( cd "$P" && git checkout -q -b feature/y )
run_hook guard-bash "$P" "$(j_bash "$P" "git push")"
assert_rc "bare git push on a feature branch allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin --delete feature/y")"
assert_rc "deleting a remote feature branch allowed" 0
( cd "$P" && git checkout -q main )
for c in "git push origin +main" "git push origin refs/heads/main" "git push -u origin HEAD:refs/heads/main" "git push origin --delete main" "git -C . push origin main" "git push --force-with-lease origin main" "git push origin"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "bypass attempt blocked: $c" 2
done
( cd "$P" && git checkout -q feature/y )
run_hook guard-bash "$P" "$(j_bash "$P" "git push --all origin")"
assert_rc "--all from a feature branch blocked (pushes main too)" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin sdlc/x && echo pushed to main")"
assert_rc "unrelated clause mentioning main is not a false positive" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin main:feature/z")"
assert_rc "main:feature refspec targets the feature branch → allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin --delete feature/z")"
assert_rc "deleting a feature branch allowed" 0
set_cfg "$P" protect.default_branch '"master"'
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin master")"
assert_rc "configured default_branch master is protected" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin main")"
assert_rc "with default_branch master, main is an ordinary branch" 0
set_cfg "$P" protect.default_branch '"main"'
set_cfg "$P" protect.block_direct_push false
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin main")"
assert_rc "block_direct_push=false allows main push" 0

# ======================================================================
echo "-- guard-bash: commit secret scan (temp git repo)"
P=$(fresh)
( cd "$P" && git init -q . && git config user.email t@t && git config user.name t && git config commit.gpgsign false && git add -A >/dev/null && git commit -qm init ) 2>/dev/null
printf 'token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n' > "$P/src/leak.py"; ( cd "$P" && git add src/leak.py )
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'add client'")"
assert_rc "git commit with staged secret blocked" 2
assert_has "commit block names a pattern" "$ERR" "pattern#"
assert_lacks "commit block never echoes the secret" "$ERR" "ghp_ABCDEFGHIJ"
( cd "$P" && git restore --staged src/leak.py 2>/dev/null || git reset -q src/leak.py ); rm -f "$P/src/leak.py"
printf 'x = 1\n' >> "$P/src/app.py"; ( cd "$P" && git add src/app.py )
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'clean change'")"
assert_rc "clean staged commit allowed" 0
printf 'aws = "%s"\n' "$AWS_KEY" >> "$P/src/app.py"     # tracked, NOT staged
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'only staged'")"
assert_rc "unstaged secret does not block a plain commit" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -am 'commit all'")"
assert_rc "git commit -am scans unstaged tracked changes too" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git commit --all -m 'commit all'")"
assert_rc "git commit --all scans unstaged tracked changes too" 2
( cd "$P" && git checkout -q -- src/app.py )
mkdir -p "$P/evals/cases/x/fixture"; printf 'aws = "%s"\n' "$AWS_KEY" > "$P/evals/cases/x/fixture/f.py"; ( cd "$P" && git add evals )
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'eval fixture'")"
assert_rc "staged fake secret under evals/ is exempt" 0
set_cfg "$P" secrets.scan_commits false
printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/leak2.py"; ( cd "$P" && git add src/leak2.py )
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m x")"
assert_rc "scan_commits=false disables commit scan" 0

# ======================================================================
echo "-- uninitialized project"
P=$(fresh_plain)
run_hook guard-bash "$P" "$(j_bash "$P" "kubectl rollout restart deploy/api -n prod")"
assert_rc "uninitialized: production gate still blocks" 2
run_hook guard-bash "$P" "$(j_bash "$P" "kubectl rollout restart deploy/api -n prod")" RELEASE_APPROVAL=REL-1
assert_rc "uninitialized: RELEASE_APPROVAL unlocks" 0
run_hook guard-bash "$P" "$(j_bash "$P" "helm upgrade api ./chart -n staging")"
assert_rc "uninitialized: staging is not gated" 0
assert_true "uninitialized: no .sdlc dir created" "$([ ! -e "$P/.sdlc" ] && echo true || echo false)" "$(ls -a "$P")"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "uninitialized: test edit allowed" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/app.py" "aws_key = '$AWS_KEY'")"
assert_rc "uninitialized: secret scan still blocks" 2
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_rc "uninitialized: session-start exits 0" 0
assert_has "uninitialized: session-start prints the init hint" "$OUT" "run /sdlc:init"
assert_true "uninitialized: hint is one line" "$([ "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" = 1 ] && echo true || echo false)" "$OUT"
for h in post-edit post-bash stop-verify session-end; do
  run_hook "$h" "$P" "$(j_stop "$P" false)"
  assert_rc "uninitialized: $h exits 0 silently" 0
done

# ======================================================================
echo "-- post-edit: state + formatter"
P=$(fresh)
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
assert_rc "post-edit exits 0" 0
assert_true "post-edit sets edited=true" "$([ "$(state_of "$P" edited)" = true ] && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json" 2>/dev/null)"
assert_true "post-edit sets last_edit_ts (ISO)" "$(state_of "$P" last_edit_ts | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' && echo true || echo false)" "$(state_of "$P" last_edit_ts)"
assert_true "post-edit sets last_edit_epoch" "$(state_of "$P" last_edit_epoch | grep -Eq '^[0-9]{10}$' && echo true || echo false)" "$(state_of "$P" last_edit_epoch)"
run_hook post-edit "$P" "$(j_postedit "$P" "$P/plan/demo.md")"
assert_true "editing the active plan marks plan_sync_checked" "$([ "$(state_of "$P" plan_sync_checked)" = true ] && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json")"
set_cfg "$P" commands.format '"bash -c '"'"'echo FORMATTED >> {file}'"'"'"'
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
assert_rc "custom formatter run exits 0" 0
assert_file_has "custom formatter received {file}" "$P/src/app.py" "FORMATTED"
set_cfg "$P" commands.format '"false"'
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
assert_rc "failing formatter never fails the hook" 0
assert_has "failing formatter leaves a one-line stderr note" "$ERR" "[sdlc] formatter"
set_cfg "$P" commands.format '"sh -c \"sleep 5\" x"'
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")" SDLC_FORMAT_TIMEOUT=1
assert_rc "hung formatter is killed by the cap (exit 0)" 0
assert_has "cap note says timed out" "$ERR" "timed out"
set_cfg "$P" commands.format '"auto"'
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
assert_rc "auto formatter with no tool installed is a no-op" 0
set_cfg "$P" hooks.format_on_edit false; set_cfg "$P" commands.format '"false"'
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
assert_true "format_on_edit=false skips the formatter" "$([ -z "$ERR" ] && echo true || echo false)" "$ERR"

# ======================================================================
echo "-- post-bash: verify tracking"
P=$(fresh); set_cfg "$P" commands.test '"npm test"'; set_cfg "$P" commands.lint '"npm run lint"'
run_hook post-bash "$P" "$(j_postbash "$P" "npm test -- --ci" "Tests: 12 passed, 12 total" "")"
assert_rc "post-bash exits 0" 0
assert_true "passing test run → last_verify_ok=true" "$([ "$(state_of "$P" last_verify_ok)" = true ] && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json")"
assert_true "last_verify_cmd recorded" "$([ "$(state_of "$P" last_verify_cmd)" = "npm test -- --ci" ] && echo true || echo false)" "$(state_of "$P" last_verify_cmd)"
assert_true "events.jsonl has verify.run pass" "$(grep -q '"event":"verify.run".*"decision":"pass"' "$P/.sdlc/logs/events.jsonl" && echo true || echo false)" "$(cat "$P/.sdlc/logs/events.jsonl")"
run_hook post-bash "$P" "$(j_postbash "$P" "npm test" "" "FAIL src/app.test.js")"
assert_true "FAIL in stderr → last_verify_ok=false" "$([ "$(state_of "$P" last_verify_ok)" = false ] && echo true || echo false)" "$(state_of "$P" last_verify_ok)"
run_hook post-bash "$P" "$(j_postbash "$P" "npm test" "= 1 failed, 20 passed =" "")"
assert_true "'1 failed' in stdout → false" "$([ "$(state_of "$P" last_verify_ok)" = false ] && echo true || echo false)" "$(state_of "$P" last_verify_ok)"
run_hook post-bash "$P" "$(j_postbash "$P" "npm test" "Tests run: 4, Failures: 0, Errors: 0 -- BUILD SUCCESS" "")"
assert_true "'Failures: 0' is not a failure" "$([ "$(state_of "$P" last_verify_ok)" = true ] && echo true || echo false)" "$(state_of "$P" last_verify_ok)"
run_hook post-bash "$P" "$(j_postbash "$P" "npm test" "" "" true)"
assert_true "interrupted → false" "$([ "$(state_of "$P" last_verify_ok)" = false ] && echo true || echo false)" "$(state_of "$P" last_verify_ok)"
run_hook post-bash "$P" "$(j_postbash "$P" "npm run lint" "ok" "")"
assert_true "lint command recognized (last_verify_name=lint)" "$([ "$(state_of "$P" last_verify_name)" = lint ] && echo true || echo false)" "$(state_of "$P" last_verify_name)"
Q=$(fresh); set_cfg "$Q" commands.test '"npm test"'
run_hook post-bash "$Q" "$(j_postbash "$Q" "ls -la" "files" "")"
assert_true "non-verify command leaves no verify state" "$([ -z "$(state_of "$Q" last_verify_ts)" ] && echo true || echo false)" "$(cat "$Q/.sdlc/state/session-$SID.json" 2>/dev/null)"
set_cfg "$Q" verify.commands '["test", "make check"]'
run_hook post-bash "$Q" "$(j_postbash "$Q" "make check" "all good" "")"
assert_true "literal verify.commands entry recognized" "$([ "$(state_of "$Q" last_verify_ok)" = true ] && echo true || echo false)" "$(cat "$Q/.sdlc/state/session-$SID.json" 2>/dev/null)"

# ======================================================================
echo "-- stop-verify"
P=$(fresh); set_cfg "$P" verify.required_before_stop true; set_cfg "$P" commands.test '"npm test"'
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_rc "nothing edited → allowed" 0
assert_true "nothing edited → no output" "$([ -z "$OUT" ] && echo true || echo false)" "$OUT"
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_rc "edited without verify → exit 0 (JSON block)" 0
assert_has "edited without verify → decision block" "$OUT" '"decision":"block"'
assert_has "block reason lists the verify command" "$OUT" "npm test"
assert_has "block reason says fix the code, not the test" "$OUT" "fix the code, not the test"
run_hook stop-verify "$P" "$(j_stop "$P" true)"
assert_true "stop_hook_active=true → always allowed" "$([ -z "$OUT" ] && [ "$RC" = 0 ] && echo true || echo false)" "rc=$RC out=$OUT"
run_hook post-bash "$P" "$(j_postbash "$P" "npm test" "" "FAIL")"
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_has "failed verify after edit → still blocked" "$OUT" '"decision":"block"'
assert_has "failed verify reason mentions the failure" "$OUT" "verify run failed"
run_hook post-bash "$P" "$(j_postbash "$P" "npm test" "12 passed" "")"
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_true "passing verify newer than edit → allowed" "$([ -z "$OUT" ] && [ "$RC" = 0 ] && echo true || echo false)" "rc=$RC out=$OUT"
sleep 1; run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_has "new edit after verify → blocked again" "$OUT" '"decision":"block"'
set_cfg "$P" verify.required_before_stop false
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_true "required_before_stop=false → allowed" "$([ -z "$OUT" ] && [ "$RC" = 0 ] && echo true || echo false)" "rc=$RC out=$OUT"
Q=$(fresh); set_cfg "$Q" verify.required_before_stop true
run_hook post-edit "$Q" "$(j_postedit "$Q" "$Q/src/app.py")"
run_hook stop-verify "$Q" "$(j_stop "$Q" false)"
assert_true "required but no verify command configured → allowed (logged skip)" "$([ -z "$OUT" ] && grep -q '"event":"hook.stop".*"decision":"skip"' "$Q/.sdlc/logs/events.jsonl" && echo true || echo false)" "out=$OUT log=$(cat "$Q/.sdlc/logs/events.jsonl")"
echo "-- stop-verify: plan sync"
P=$(fresh); set_cfg "$P" plan.enforce_sync true
touch -t 202601010000 "$P/plan/demo.md"
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_has "plan older than edit → departures question" "$OUT" "Departures from plan"
assert_has "plan sync reason names the plan file" "$OUT" "plan/demo.md"
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_true "plan sync asked only once per session" "$([ -z "$OUT" ] && echo true || echo false)" "$OUT"
Q=$(fresh); set_cfg "$Q" plan.enforce_sync true
run_hook post-edit "$Q" "$(j_postedit "$Q" "$Q/src/app.py")"
touch "$Q/plan/demo.md"
run_hook stop-verify "$Q" "$(j_stop "$Q" false)"
assert_true "plan touched after edit → no question" "$([ -z "$OUT" ] && echo true || echo false)" "$OUT"

# ======================================================================
echo "-- session-start / session-end"
P=$(fresh)
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_rc "session-start exits 0" 0
assert_has "context: plugin version line" "$OUT" "sdlc plugin v"
assert_has "context: active plan with slug and status" "$OUT" "plan/demo.md (slug: demo, status: approved)"
assert_has "context: pending intents count" "$OUT" "Pending intents (intent/triage/): 2"
assert_has "context: verify commands" "$OUT" "Verify commands:"
assert_has "context: rule 1" "$OUT" "start with /sdlc:plan"
assert_has "context: rule 2" "$OUT" "never edit tests during a fix"
assert_has "context: rule 3" "$OUT" "/sdlc:lesson"
assert_true "context block is ≤ 12 lines" "$([ "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" -le 12 ] && echo true || echo false)" "$(printf '%s\n' "$OUT" | wc -l)"
assert_true "session.start logged" "$(grep -q '"event":"session.start"' "$P/.sdlc/logs/events.jsonl" && echo true || echo false)" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
assert_true "session state file has started" "$(state_of "$P" started | grep -Eq '^[0-9]{4}-' && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json" 2>/dev/null)"
rm -f "$P/.sdlc/state/active-plan"
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: no active plan → none" "$OUT" "Active plan: none"
set_cfg "$P" hooks.session_context false
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_true "session_context=false → no context output (false honored)" "$([ -z "$OUT" ] && [ "$RC" = 0 ] && echo true || echo false)" "$OUT"
run_hook session-end "$P" "$(j_session "$P" SessionEnd)"
assert_rc "session-end exits 0" 0
assert_true "session.end logged" "$(grep -q '"event":"session.end"' "$P/.sdlc/logs/events.jsonl" && echo true || echo false)" "$(cat "$P/.sdlc/logs/events.jsonl")"
set_cfg "$P" hooks.log_events false
before=$(wc -l < "$P/.sdlc/logs/events.jsonl")
run_hook session-end "$P" "$(j_session "$P" SessionEnd)"
after=$(wc -l < "$P/.sdlc/logs/events.jsonl")
assert_true "log_events=false suppresses logging (known common.sh limitation if this fails)" "$([ "$before" = "$after" ] && echo true || echo false)" "before=$before after=$after"

# ======================================================================
echo "-- robustness: empty / garbage stdin never blocks"
P=$(fresh)
for h in session-start session-end guard-edit guard-bash post-edit post-bash stop-verify; do
  run_hook "$h" "$P" ""
  assert_rc "empty stdin: $h exits 0" 0
  run_hook "$h" "$P" "not json at all {"
  assert_rc "garbage stdin: $h exits 0" 0
done

# ======================================================================
echo "-- python fallback (PATH without jq)"
if command -v python3 >/dev/null 2>&1; then
  NOJQ="$WORK/nojq-bin"; mkdir -p "$NOJQ"
  for t in bash sh cat sed grep awk tr date stat mkdir mv rm basename dirname git head tail wc find ls sleep env cp touch cut sort uniq tee mktemp cmp xargs kill python3 perl; do
    src=$(command -v "$t" 2>/dev/null); [ -n "$src" ] && ln -sf "$src" "$NOJQ/$t"
  done
  real_py=$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null); [ -n "$real_py" ] && ln -sf "$real_py" "$NOJQ/python3"
  if env PATH="$NOJQ" bash -c 'command -v jq' >/dev/null 2>&1 || ! env PATH="$NOJQ" python3 -c 'import json' >/dev/null 2>&1; then
    echo "SKIP  python fallback (could not build a jq-less PATH with a working python3)"
  else
    P=$(fresh)
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/gen/client.txt" "edited")" PATH="$NOJQ"
    assert_rc "no-jq: frozen path blocked" 2
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "edited")" PATH="$NOJQ"
    assert_rc "no-jq: test lock blocked" 2
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "aws_key = '$AWS_KEY'")" PATH="$NOJQ"
    assert_rc "no-jq: secret blocked" 2
    run_hook guard-bash "$P" "$(j_bash "$P" "kubectl apply -f x.yaml -n production")" PATH="$NOJQ"
    assert_rc "no-jq: production gate blocked" 2
    run_hook guard-bash "$P" "$(j_bash "$P" "helm upgrade api ./chart -n staging")" PATH="$NOJQ"
    assert_has "no-jq: staging ask JSON" "$OUT" '"permissionDecision":"ask"'
    set_cfg "$P" verify.required_before_stop true; set_cfg "$P" commands.test '"npm test"'
    run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")" PATH="$NOJQ"
    assert_true "no-jq: post-edit state written" "$([ "$(state_of "$P" edited)" = true ] && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json" 2>/dev/null)"
    run_hook stop-verify "$P" "$(j_stop "$P" false)" PATH="$NOJQ"
    assert_has "no-jq: stop-verify blocks" "$OUT" '"decision":"block"'
    run_hook post-bash "$P" "$(j_postbash "$P" "npm test" "ok" "")" PATH="$NOJQ"
    run_hook stop-verify "$P" "$(j_stop "$P" false)" PATH="$NOJQ"
    assert_true "no-jq: stop-verify allows after verify" "$([ -z "$OUT" ] && echo true || echo false)" "$OUT"
    run_hook session-start "$P" "$(j_session "$P" SessionStart)" PATH="$NOJQ"
    assert_has "no-jq: session-start context" "$OUT" "Active plan: plan/demo.md"
    set_cfg "$P" protect.protect_tests false
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "edited")" PATH="$NOJQ"
    assert_rc "no-jq: protect_tests=false honored" 0
  fi
else
  echo "SKIP  python fallback (python3 not installed)"
fi

# ======================================================================
echo "-- timing"
assert_true "every hook run completed in < 1s (slowest: $MAX_MS_HOOK ${MAX_MS}ms)" "$([ -z "$SLOW" ] && echo true || echo false)" "slow runs:$SLOW"

echo "== $PASS passed, $FAIL failed (slowest hook run: $MAX_MS_HOOK ${MAX_MS}ms)"
[ "$FAIL" -eq 0 ]
