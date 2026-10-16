#!/bin/bash
# sdlc plugin — hook test matrix. Run from anywhere:  bash tests/hooks.sh [-v] [name-filter]
# Prints PASS/FAIL per check, a summary line, and exits 1 when any check failed. Needs bash 3.2+, jq or python3, git.
# Fixtures: project-init (version-2 config, work docs), project-v1 (a 0.x repository), project-plain (not initialized).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; PLUGIN="$(cd "$HERE/.." && pwd)"; FIX="$HERE/fixtures/hooks"
. "$PLUGIN/scripts/lib/common.sh"
VERBOSE=false; FILTER=""
for a in "$@"; do case "$a" in -v) VERBOSE=true;; *) FILTER="$a";; esac; done
WORK=$(mktemp -d "${TMPDIR:-/tmp}/sdlc-hooks.XXXXXX"); trap 'rm -rf "$WORK"' EXIT
unset RELEASE_APPROVAL CHANGE_TICKET CAB_APPROVAL SDLC_ALLOW_TEST_EDIT SDLC_FORMAT_TIMEOUT CLAUDE_PROJECT_DIR
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null      # the temp repositories see no user or system git config
PASS=0; FAIL=0; SLOW=""; MAX_MS=0; MAX_MS_HOOK=""
SID="test-session"

# ---------- infrastructure ----------
now_ms() { perl -MTime::HiRes=time -e 'printf("%d\n", time()*1000)' 2>/dev/null || echo $(( $(date +%s) * 1000 )); }
# NOTE: called as $(fresh) — a subshell — so a counter would never advance in the parent; mktemp gives a unique dir each time.
fresh()       { local d; d=$(mktemp -d "$WORK/pXXXXXX"); cp -R "$FIX/project-init/." "$d/"; printf '%s' "$d"; }
fresh_v1()    { local d; d=$(mktemp -d "$WORK/pXXXXXX"); cp -R "$FIX/project-v1/." "$d/"; printf '%s' "$d"; }
fresh_plain() { local d; d=$(mktemp -d "$WORK/pXXXXXX"); cp -R "$FIX/project-plain/." "$d/"; printf '%s' "$d"; }
# every git command of the tests runs inside $WORK — never in the repository the tests live in
in_work() { case "$1" in "$WORK"/?*) return 0;; esac; echo "tests/hooks.sh: refusing to touch '$1' (outside $WORK)" >&2; return 1; }
gitp() { local d="$1"; in_work "$d" || return 1; shift; git -C "$d" "$@"; }
# git_init <dir> — a repository on branch main with everything committed
git_init() {
  in_work "$1" || return 1
  ( cd "$1" && git init -q . && git symbolic-ref HEAD refs/heads/main && git config user.email t@t && git config user.name t \
      && git config commit.gpgsign false && git add -A >/dev/null 2>&1 && git commit -qm init ) >/dev/null 2>&1
}
fresh_git() { local d; d=$(fresh); git_init "$d"; printf '%s' "$d"; }
# activate <dir> <slug> — check out sdlc/<slug> and make <slug> the active work doc (what the engine does for a session)
activate() {
  in_work "$1" || return 1
  { gitp "$1" checkout -q "sdlc/$2" || gitp "$1" checkout -q -b "sdlc/$2"; } >/dev/null 2>&1
  mkdir -p "$1/.sdlc/state"; printf '%s\n' "$2" > "$1/.sdlc/state/active-work"
}
# fresh_active [slug] — a repository on sdlc/<slug> with that work doc active (default: planned), i.e. sdlc work: existing tests are
# locked. What the fixture ships is in the default branch (existing); a file created afterwards is not (this change's own).
fresh_active() { local d; d=$(fresh_git); activate "$d" "${1:-planned}"; printf '%s' "$d"; }
# mk <dir> <rel> [content] — create a file (and its directories)
mk() { mkdir -p "$(dirname "$1/$2")" && printf '%s\n' "${3:-x}" > "$1/$2"; }
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
  local limit=1000; case "$*" in *PATH=*) limit=2500;; esac                 # the python fallback starts one interpreter per config read
  case "$*" in *SDLC_FORMAT_TIMEOUT=*) ;; *) [ "$MS" -ge "$limit" ] && SLOW="$SLOW $hook:${MS}ms";; esac   # the deliberate hung-formatter case is capped at ~1s by design
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
assert_eq()      { skip_case "$1" && return; if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2' want '$3'"; fi; }
state_of() { json_file_get "$1/.sdlc/state/session-$SID.json" ".$2"; }
log_has() { grep -q -- "$2" "$1/.sdlc/logs/events.jsonl" 2>/dev/null && echo true || echo false; }   # log_has <root> <regex> → true|false
short() { printf '%s' "$1" | tr '\n\r\t' '   ' | head -c 64; }

# ---------- stdin builders (the first argument of j_* is the working directory of the call) ----------
base() { printf '"session_id":"%s","transcript_path":"/dev/null","cwd":%s,"permission_mode":"default"' "$SID" "$(json_escape "$1")"; }
j_edit()   { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Edit","tool_use_id":"t1","tool_input":{"file_path":%s,"old_string":"x","new_string":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")"; }
j_write()  { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Write","tool_use_id":"t1","tool_input":{"file_path":%s,"content":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")"; }
j_multi()  { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"MultiEdit","tool_use_id":"t1","tool_input":{"file_path":%s,"edits":[{"old_string":"a","new_string":"safe text"},{"old_string":"b","new_string":%s}]}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")"; }
j_nb()     { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"NotebookEdit","tool_use_id":"t1","tool_input":{"notebook_path":%s,"cell_id":"c1","new_source":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")"; }
j_bash()   { printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Bash","tool_use_id":"t1","tool_input":{"command":%s,"description":"test"}}' "$(base "$1")" "$(json_escape "$2")"; }
j_postbash(){ printf '{%s,"hook_event_name":"PostToolUse","tool_name":"Bash","tool_use_id":"t1","tool_input":{"command":%s},"tool_response":{"stdout":%s,"stderr":%s,"interrupted":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$3")" "$(json_escape "$4")" "${5:-false}"; }
j_postbashfail(){ printf '{%s,"hook_event_name":"PostToolUseFailure","tool_name":"Bash","tool_use_id":"t1","tool_input":{"command":%s},"error":%s,"is_interrupt":false}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "Exit code 1
$3")"; }
j_postedit(){ printf '{%s,"hook_event_name":"PostToolUse","tool_name":"Edit","tool_use_id":"t1","tool_input":{"file_path":%s,"old_string":"x","new_string":"y"},"tool_response":{"filePath":%s}}' "$(base "$1")" "$(json_escape "$2")" "$(json_escape "$2")"; }
j_stop()   { printf '{%s,"hook_event_name":"Stop","stop_hook_active":%s,"last_assistant_message":"done"}' "$(base "$1")" "$2"; }
j_session(){ printf '{%s,"hook_event_name":"%s","source":"startup"}' "$(base "$1")" "$2"; }

# the fake secrets are assembled from pieces so that this file itself carries no secret-shaped literal
AWS_KEY="AKIAQ7Z2M9R4""T1B8C5D3"
PEM_HDR="-----BEGIN RSA ""PRIVATE KEY-----"
GH_TOKEN="ghp_""ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
SLACK_TOKEN="xoxb-""123456789012-ABCDEFGHIJKLMNOP"
PW_LITERAL="hunter2hunter2""hunter2"

echo "== sdlc hook tests  (plugin: $PLUGIN, work: $WORK, jq: $(command -v jq >/dev/null && echo yes || echo no), python3: $(command -v python3 >/dev/null && echo yes || echo no))"

# ======================================================================
echo "-- lib: frontmatter and the active work doc"
F="$WORK/fm"; mkdir -p "$F"
printf -- '---\nslug: demo\nstatus: ready          # proposed | ready | planned\ntest_changes: "allowed"\nowner: '"'"'me'"'"'\nstatus_x: no\n---\nstatus: done\n' > "$F/a.md"
printf -- '---\r\nstatus: planned\r\n---\r\n' > "$F/crlf.md"
printf 'status: ready\n' > "$F/nofront.md"
assert_eq "frontmatter: value with a trailing comment" "$(frontmatter_get "$F/a.md" status)" "ready"
assert_eq "frontmatter: double-quoted value" "$(frontmatter_get "$F/a.md" test_changes)" "allowed"
assert_eq "frontmatter: single-quoted value" "$(frontmatter_get "$F/a.md" owner)" "me"
assert_eq "frontmatter: a key is not a prefix match" "$(frontmatter_get "$F/a.md" stat)" ""
assert_eq "frontmatter: a missing key is empty" "$(frontmatter_get "$F/a.md" nope)" ""
assert_eq "frontmatter: CRLF line endings" "$(frontmatter_get "$F/crlf.md" status)" "planned"
assert_eq "frontmatter: no leading --- block → empty" "$(frontmatter_get "$F/nofront.md" status)" ""
assert_eq "frontmatter: missing file → empty" "$(frontmatter_get "$F/none.md" status)" ""
P=$(fresh_git)
assert_eq "active work: no active-work file → none" "$(active_work_slug "$P")" ""
activate "$P" planned
assert_eq "active work: the slug applies on its own branch" "$(active_work_slug "$P")" "planned"
assert_eq "active work: the doc is <work>/<slug>.md" "$(active_work_file "$P")" "work/planned.md"
gitp "$P" checkout -q main
assert_eq "active work: ignored on any other branch (stale file)" "$(active_work_slug "$P")" ""
assert_eq "active work: the doc is ignored with it" "$(active_work_file "$P")" ""
gitp "$P" checkout -q sdlc/planned
for bad_slug in "Bad Slug" "../x" ".hidden" "a/b" ""; do
  printf '%s\n' "$bad_slug" > "$P/.sdlc/state/active-work"
  assert_eq "active work: unusable slug '$bad_slug' → none" "$(active_work_slug "$P")" ""
done
printf 'planned\nsecond line is ignored\n' > "$P/.sdlc/state/active-work"
assert_eq "active work: only the first line counts" "$(active_work_slug "$P")" "planned"
activate "$P" ghost
assert_eq "active work: a slug without a doc has no file" "$(active_work_file "$P")" ""
set_cfg "$P" paths.work '"tasks/"'
assert_eq "work dir: paths.work honored, trailing slash dropped" "$(work_dir "$P")" "tasks"
assert_eq "work dir: default is work" "$(work_dir "$(fresh)")" "work"

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
assert_true "block was logged to events.jsonl" "$(log_has "$P" '"event":"hook.guard".*"decision":"deny".*frozen_path')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
assert_true "allowed edit was not logged" "$([ "$(grep -c 'hook.guard' "$P/.sdlc/logs/events.jsonl")" = 2 ] && echo true || echo false)" "$(cat "$P/.sdlc/logs/events.jsonl")"
set_cfg "$P" protect.generated_paths '["build/", "**/dist/**"]'
run_hook guard-edit "$P" "$(j_write "$P" "$P/build/out.js" "x")"
assert_rc "generated path blocked (build/)" 2
assert_has "generated path message names the config key" "$ERR" "protect.generated_paths"
run_hook guard-edit "$P" "$(j_write "$P" "$P/pkg/dist/bundle.js" "x")"
assert_rc "generated glob blocked (**/dist/**)" 2
run_hook guard-edit "$P" "$(j_write "$P" "$P/dist/bundle.js" "x")"
assert_rc "generated glob: **/ also stands for no directory at all (dist/bundle.js)" 2
run_hook guard-edit "$P" "$(j_write "$P" "$P/distant/a.js" "x")"
assert_rc "generated glob: dist is a whole directory name (distant/a.js is not under **/dist/**)" 0
run_hook guard-edit "$P" "$(j_write "$P" "$P/schemas/sub/b.avsc" "{}")"
assert_rc "frozen glob: * stops at / (schemas/*.avsc leaves schemas/sub/b.avsc), as in the merge gate" 0
run_hook guard-edit "$P" "$(j_write "$P" "$P/x/schemas/b.avsc" "{}")"
assert_rc "frozen glob: a pattern is anchored at the repository root (x/schemas/b.avsc is not schemas/*.avsc)" 0
set_cfg "$P" protect.generated_paths '["dist/**"]'
run_hook guard-edit "$P" "$(j_write "$P" "$P/dist/a.js" "x")"
assert_rc "generated glob: dist/** blocks dist/a.js" 2
run_hook guard-edit "$P" "$(j_write "$P" "$P/pkg/dist/a.js" "x")"
assert_rc "generated glob: …at the root only, pkg/dist/a.js takes **/dist/**" 0

# ======================================================================
echo "-- guard-edit: test-file lock during sdlc work (existing files locked, new ones free)"
P=$(fresh)
mk "$P" lib/__tests__/a.js; mk "$P" pkg/thing_test.go; mk "$P" app/models/user_spec.rb; mk "$P" spec/helper.rb
mk "$P" test_util.py; mk "$P" src/FooTest.java; mk "$P" web/button.test.tsx; mk "$P" src/contest/main.py; mk "$P" src/latest.py
mk "$P" app/src/androidTest/java/ui/ScreenTest.kt; mk "$P" lib/testFixtures/data.json; mk "$P" Sources/AppTests.swift; mk "$P" src/UserSpec.kt
mk "$P" web/button.test.d.ts; mk "$P" work/tests/plan_test.py; mk "$P" qa/smoke.sh; mk "$P" qa/sub/deep.sh
git_init "$P"; activate "$P" planned                          # everything above is in the default branch: existing files; the work doc applies
for f in tests/x_test.py src/test/Foo.java foo.spec.ts lib/__tests__/a.js pkg/thing_test.go app/models/user_spec.rb spec/helper.rb test_util.py src/FooTest.java web/button.test.tsx \
         app/src/androidTest/java/ui/ScreenTest.kt lib/testFixtures/data.json Sources/AppTests.swift src/UserSpec.kt; do
  run_hook guard-edit "$P" "$(j_edit "$P" "$P/$f" "changed")"
  assert_rc "existing test path blocked: $f" 2
done
for f in src/app.py src/contest/main.py src/latest.py web/button.test.d.ts work/tests/plan_test.py; do
  run_hook guard-edit "$P" "$(j_edit "$P" "$P/$f" "changed")"
  assert_rc "existing non-test path allowed (not a test for the merge gate either): $f" 0
done
for f in tests/new_test.py src/test/NewFoo.java new.spec.ts tests/sub/dir/deep_test.py; do
  run_hook guard-edit "$P" "$(j_write "$P" "$P/$f" "def test_new(): pass")"
  assert_rc "NEW test file allowed: $f" 0
done
run_hook guard-edit "$P" "$(j_write "$P" "/tmp/outside_test.py" "x")"
assert_rc "a path outside the project is not a project test" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_has "test lock message gives the unlock route" "$ERR" "SDLC_ALLOW_TEST_EDIT=1"
assert_has "test lock message names test_changes" "$ERR" "test_changes: allowed"
assert_has "test lock message points at the active work doc" "$ERR" "work/planned.md"
assert_has "test lock message says tests this change adds are free" "$ERR" "test files this change adds stay free to edit"
assert_lacks "test lock message does not mention the verify skill" "$ERR" "/sdlc:verify"
assert_lacks "test lock message does not mention plan files" "$ERR" "plan/"
assert_true "test lock was logged" "$(log_has "$P" '"decision":"deny".*test_lock')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")" SDLC_ALLOW_TEST_EDIT=1
assert_rc "existing test edit allowed via SDLC_ALLOW_TEST_EDIT=1" 0
mkdir -p "$P/.sdlc/state"; touch "$P/.sdlc/state/allow-test-edit"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "the 0.x unlock file .sdlc/state/allow-test-edit unlocks nothing (an agent could create it itself)" 2
rm -f "$P/.sdlc/state/allow-test-edit"
set_cfg "$P" protect.protect_tests false
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "protect_tests=false disables the lock (false must be honored)" 0
set_cfg "$P" protect.protect_tests true
set_cfg "$P" protect.test_paths '["qa/"]'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/qa/smoke.sh" "changed")"
assert_rc "custom test_paths prefix blocked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "custom test_paths replaces the auto set" 0
set_cfg "$P" protect.test_paths '["qa/*.sh"]'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/qa/smoke.sh" "changed")"
assert_rc "custom test_paths glob: qa/*.sh locks qa/smoke.sh" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/qa/sub/deep.sh" "changed")"
assert_rc "…and * stops at /: qa/sub/deep.sh is no test of it (the merge gate reads the glob the same way)" 0
set_cfg "$P" protect.test_paths '[]'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/qa/smoke.sh" "changed")"
assert_rc "an explicit empty test_paths locks nothing" 0
Q=$(fresh_active)
assert_true "the fixture's version-2 config is sparse (no protect.test_paths)" "$([ -z "$(json_file_get "$Q/.sdlc/config.json" .protect.test_paths)" ] && echo true || echo false)" "$(cat "$Q/.sdlc/config.json")"
run_hook guard-edit "$Q" "$(j_edit "$Q" "$Q/tests/x_test.py" "changed")"
assert_rc "sparse config: an unset test_paths means auto" 2
set_cfg "$Q" paths.work '"tasks"'; mkdir -p "$Q/tasks"; cp "$Q/work/planned.md" "$Q/tasks/planned.md"
mk "$Q" tasks/tests/a_test.py; mk "$Q" work/tests/b_test.py; gitp "$Q" add -A >/dev/null 2>&1; gitp "$Q" commit -qm more >/dev/null 2>&1; gitp "$Q" branch -f main HEAD >/dev/null 2>&1
run_hook guard-edit "$Q" "$(j_edit "$Q" "$Q/tasks/tests/a_test.py" "changed")"
assert_rc "paths.work is the work dir that is never a test dir (tasks/)" 0
run_hook guard-edit "$Q" "$(j_edit "$Q" "$Q/work/tests/b_test.py" "changed")"
assert_rc "…and work/ is an ordinary dir then: its tests are locked" 2

echo "-- guard-edit: the test lock applies only during sdlc work (an active work doc applies)"
P=$(fresh_git)
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "no sdlc work (main, no active-work file): editing an existing test is allowed" 0
assert_true "…and silently" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
run_hook guard-edit "$P" "$(j_write "$P" "$P/tests/x_test.py" "def test_x(): pass")"
assert_rc "…also through Write" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/gen/client.txt" "x")"
assert_rc "…while the frozen-path guard stays on outside sdlc work" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "aws_key = '$AWS_KEY'")"
assert_rc "…and so does the secret scan" 2
gitp "$P" checkout -q -b sdlc/planned >/dev/null 2>&1
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "on a sdlc/<slug> branch without an active-work file: no sdlc work, allowed" 0
mkdir -p "$P/.sdlc/state"; printf 'planned\n' > "$P/.sdlc/state/active-work"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "on sdlc/<slug> with active-work naming it: sdlc work, the existing test is locked" 2
assert_has "lock message names the active work doc" "$ERR" "work/planned.md"
gitp "$P" checkout -q main
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "stale active-work (planned) on main: no sdlc work, allowed" 0
gitp "$P" checkout -q sdlc/planned
printf 'allowed\n' > "$P/.sdlc/state/active-work"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "active-work naming another slug than the branch: no sdlc work, allowed" 0
activate "$P" ghost
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "active slug without a doc file (branch sdlc/ghost): still locked, no crash — deleting the doc is no way out" 2
assert_has "…and the message names the doc it expects" "$ERR" "work/ghost.md"
printf '../../etc/passwd\n' > "$P/.sdlc/state/active-work"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "garbage active-work names no slug: no sdlc work, allowed, no crash" 0
activate "$P" planned
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "existing test edit blocked: active doc says test_changes: none" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")" SDLC_ALLOW_TEST_EDIT=1
assert_rc "env unlock works next to an active doc" 0
activate "$P" allowed
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "existing test edit allowed: active doc says test_changes: allowed (comment after the value)" 0
activate "$P" built
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "any status of the active doc keeps the lock: built" 2

# ======================================================================
echo "-- guard-edit: a test this change adds is not an existing test (measured against the default branch)"
P=$(fresh_git); activate "$P" planned
mk "$P" tests/session_test.py "def test_s(): pass"                  # written by the session, not committed
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/session_test.py" "def test_s(): assert True")"
assert_rc "a test the session just wrote (uncommitted) is free to edit" 0
run_hook guard-edit "$P" "$(j_write "$P" "$P/tests/session_test.py" "def test_s(): assert 1")"
assert_rc "…also through Write" 0
gitp "$P" add tests/session_test.py >/dev/null 2>&1; gitp "$P" commit -qm "wip" >/dev/null 2>&1
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/session_test.py" "def test_s(): assert 2")"
assert_rc "…and after a WIP commit on the session branch" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "an existing test (already on the default branch) stays locked next to it" 2
assert_has "…the message says what counts as existing" "$ERR" "those already on the default branch"
gitp "$P" checkout -q main; gitp "$P" merge -q --ff-only sdlc/planned >/dev/null 2>&1
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/session_test.py" "changed")"
assert_rc "…on the default branch itself no work doc applies: nothing is locked" 0
gitp "$P" checkout -q -B sdlc/planned main >/dev/null 2>&1          # the next piece of sdlc work starts from the merged default branch
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/session_test.py" "changed")"
assert_rc "once the change is on the default branch its tests are existing tests: locked (the next sdlc branch)" 2
P=$(fresh_git)
gitp "$P" update-ref refs/remotes/origin/main main                  # origin/main = the default branch as published
activate "$P" planned
mk "$P" tests/unpushed_test.py; gitp "$P" add tests/unpushed_test.py >/dev/null 2>&1; gitp "$P" commit -qm "t" >/dev/null 2>&1
gitp "$P" branch -f main sdlc/planned >/dev/null 2>&1               # the local default branch ran ahead of origin/main
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/unpushed_test.py" "changed")"
assert_rc "origin/<default> is the base when it exists (like the merge gate), not a local branch that ran ahead" 0
gitp "$P" update-ref -d refs/remotes/origin/main >/dev/null 2>&1
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/unpushed_test.py" "changed")"
assert_rc "without origin/<default> the local default branch is the base" 2
P=$(fresh_git); activate "$P" planned; mk "$P" tests/s_test.py
set_cfg "$P" protect.default_branch '"trunk"'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/s_test.py" "changed")"
assert_rc "no such default branch: nothing to compare with, an existing file counts as existing" 2
gitp "$P" branch trunk main >/dev/null 2>&1
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/s_test.py" "changed")"
assert_rc "protect.default_branch names the base branch" 0
P=$(fresh); gitp "$P" init -q . >/dev/null 2>&1; gitp "$P" symbolic-ref HEAD refs/heads/sdlc/planned
mkdir -p "$P/.sdlc/state"; printf 'planned\n' > "$P/.sdlc/state/active-work"                   # sdlc work on a branch that has no commit yet
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "a repository without any commit: no base, existing tests are locked" 2
R=$(mktemp -d "$WORK/rXXXXXX"); mkdir "$R/app"; cp -R "$FIX/project-init/." "$R/app/"; git_init "$R"
P="$R/app"; activate "$P" planned; mk "$P" tests/fresh_test.py
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/fresh_test.py" "changed")"
assert_rc "project in a subdirectory of the repository: a new test is free" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "…and an existing one is locked" 2

# ======================================================================
echo "-- guard-edit: design first (work doc ready or proposed)"
P=$(fresh_git); activate "$P" ready
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "ready doc: source edit blocked" 2
assert_has "design-first message" "$ERR" "design first — fill Plan and Proof, then set status planned"
assert_has "design-first message names the doc and its status" "$ERR" "work/ready.md has status 'ready'"
assert_lacks "design-first message does not mention removed skills" "$ERR" "/sdlc:plan"
assert_true "design-first block was logged" "$(log_has "$P" '"decision":"deny".*design_first')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
run_hook guard-edit "$P" "$(j_write "$P" "$P/src/new_module.py" "x = 1")"
assert_rc "ready doc: creating a source file blocked" 2
run_hook guard-edit "$P" "$(j_write "$P" "$P/tests/new_test.py" "def test_x(): pass")"
assert_rc "ready doc: even a new test file waits for the design" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/work/ready.md" "## Plan")"
assert_rc "ready doc: the work doc itself is editable" 0
run_hook guard-edit "$P" "$(j_write "$P" "$P/work/another-item.md" "# idea")"
assert_rc "ready doc: other docs in the work dir are editable" 0
run_hook guard-edit "$P" "$(j_write "$P" "$P/.sdlc/state/note.txt" "x")"
assert_rc "ready doc: .sdlc/ is editable" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/CLAUDE.md" "# notes")"
assert_rc "ready doc: CLAUDE.md waits for the design too" 2
run_hook guard-edit "$P" "$(j_write "$P" "/tmp/outside.txt" "x")"
assert_rc "ready doc: a path outside the project is not guarded" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "ready doc: an existing test is refused by the test lock first" 2
assert_has "…and the message is the test lock's" "$ERR" "is locked"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/work/ready.md" "key = '$AWS_KEY'")"
assert_rc "ready doc: the secret scan applies to the work doc too" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/gen/client.txt" "x")"
assert_rc "ready doc: frozen paths are still frozen" 2
assert_has "…with the frozen message" "$ERR" "frozen-paths.txt"
activate "$P" proposed
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "proposed doc: source edit blocked" 2
assert_has "proposed doc: message carries the status" "$ERR" "status 'proposed'"
activate "$P" planned
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "planned doc: source edit allowed" 0
assert_true "planned doc: allowed edit is silent" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
run_hook guard-edit "$P" "$(j_write "$P" "$P/tests/new_test.py" "def test_x(): pass")"
assert_rc "planned doc: a new test file is allowed" 0
activate "$P" built
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "built doc: source edit allowed" 0
activate "$P" ready; gitp "$P" checkout -q main
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "ready doc named by a stale active-work on main: ignored" 0
activate "$P" ghost
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "active slug without a doc: nothing to wait for" 0
Q=$(fresh)
mkdir -p "$Q/.sdlc/state"; printf 'ready\n' > "$Q/.sdlc/state/active-work"
run_hook guard-edit "$Q" "$(j_edit "$Q" "$Q/src/app.py" "x = 1")"
assert_rc "no git repository: no branch, so no active doc" 0
P=$(fresh_git)                          # no active-work file at all: /sdlc:init and /sdlc:run write these without a work doc
for f in CLAUDE.md REVIEW.md work/new-item.md .sdlc/config.json .claude/settings.json src/app.py; do
  run_hook guard-edit "$P" "$(j_write "$P" "$P/$f" "# written without a work doc")"
  assert_rc "no active doc: $f is not denied for lacking a work doc" 0
done
mkdir -p "$P/.sdlc/state"; printf 'ready\n' > "$P/.sdlc/state/active-work"      # stale: names a ready doc, but the branch is main
for f in CLAUDE.md REVIEW.md .claude/settings.json; do
  run_hook guard-edit "$P" "$(j_write "$P" "$P/$f" "# written without a work doc")"
  assert_rc "stale active-work on the default branch: $f is not denied" 0
done
activate "$P" built
for f in CLAUDE.md REVIEW.md .claude/settings.json; do
  run_hook guard-edit "$P" "$(j_write "$P" "$P/$f" "# written on a built change")"
  assert_rc "active doc past design (built): $f is not denied" 0
done
P=$(fresh_git)
set_cfg "$P" paths.work '"tasks"'; mkdir -p "$P/tasks"; cp "$P/work/ready.md" "$P/tasks/ready.md"; activate "$P" ready
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "custom paths.work: the doc is read from there" 2
assert_has "custom paths.work: message names tasks/ready.md" "$ERR" "tasks/ready.md"
run_hook guard-edit "$P" "$(j_write "$P" "$P/tasks/next.md" "# idea")"
assert_rc "custom paths.work: that dir is editable" 0
run_hook guard-edit "$P" "$(j_write "$P" "$P/work/next.md" "# idea")"
assert_rc "custom paths.work: the default dir is no longer special" 2

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
run_hook guard-edit "$P" "$(j_multi "$P" "$P/src/app.py" "token = '$GH_TOKEN'")"
assert_rc "GitHub token inside MultiEdit edits[] blocked" 2
run_hook guard-edit "$P" "$(j_nb "$P" "$P/nb.ipynb" "os.environ['X']='$SLACK_TOKEN'")"
assert_rc "Slack token in NotebookEdit new_source blocked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "aws_key = 'AKIAIOSFODNN7EXAMPLE'")"
assert_rc "allowlisted placeholder (EXAMPLE) allowed" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "password = \"REPLACE_ME_WITH_REAL\"")"
assert_rc "allowlisted placeholder (REPLACE_ME) allowed" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/application.yml" "password: \"\${DB_PASSWORD}\"")"
assert_rc "template reference \${VAR} allowed" 0
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "password = \"$PW_LITERAL\"")"
assert_rc "generic password literal blocked" 2
set_cfg "$P" secrets.extra_patterns '["ACME-[0-9]{8}"]'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "id = 'ACME-12345678'")"
assert_rc "extra_patterns from config applied" 2
set_cfg "$P" secrets.scan_edits false
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "aws_key = '$AWS_KEY'")"
assert_rc "scan_edits=false disables the scan" 0

# ======================================================================
echo "-- guard-bash: production deploy gate (one tier)"
P=$(fresh)
run_hook guard-bash "$P" "$(j_bash "$P" "kubectl apply -f deploy.yaml -n production")"
assert_rc "production deploy without approval blocked" 2
assert_has "gate message names the approval env" "$ERR" "RELEASE_APPROVAL"
assert_has "gate message names the config key" "$ERR" "protect.deploy_patterns"
assert_true "gate deny is in events.jsonl" "$(log_has "$P" '"event":"hook.gate".*"decision":"deny"')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
assert_true "there is no gate.log any more" "$([ ! -e "$P/.sdlc/logs/gate.log" ] && echo true || echo false)" "$(ls "$P/.sdlc/logs")"
run_hook guard-bash "$P" "$(j_bash "$P" "./deploy.sh --env prod")" RELEASE_APPROVAL=REL-4711
assert_rc "production deploy with RELEASE_APPROVAL allowed" 0
assert_true "events.jsonl has hook.gate allow" "$(log_has "$P" '"event":"hook.gate".*"decision":"allow"')" "$(cat "$P/.sdlc/logs/events.jsonl")"
assert_lacks "the approval value itself is never logged" "$(cat "$P/.sdlc/logs/events.jsonl")" "REL-4711"
Q=$(fresh)
run_hook guard-bash "$Q" "$(j_bash "$Q" "helm upgrade api ./chart --namespace staging")"
assert_rc "staging deploy is not gated" 0
assert_true "staging deploy is silent" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
assert_true "staging deploy leaves no gate event" "$([ "$(log_has "$Q" hook.gate)" = false ] && echo true || echo false)" "$(cat "$Q/.sdlc/logs/events.jsonl" 2>/dev/null)"
for c in "production deploy" "./production-deploy.sh" "prod-deploy.sh --now" "npm run deploy:production"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "production word before deploy word still gated: $c" 2
done
run_hook guard-bash "$P" "$(j_bash "$P" "grep -rn production docs/")"
assert_rc "reading about production is not a deploy" 0
run_hook guard-bash "$P" "$(j_bash "$P" "make deploy ENV=dev")"
assert_rc "dev deploy allowed (no pattern)" 0
assert_true "dev deploy is silent" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
run_hook guard-bash "$P" "$(j_bash "$P" "ls -la && git status")"
assert_rc "ordinary command allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'release notes for production launch'")"
assert_rc "a commit message mentioning production is text, not a deploy (git without push is not checked)" 0
set_cfg "$P" commands.rollback '"./scripts/rollback.sh"'
run_hook guard-bash "$P" "$(j_bash "$P" "./scripts/rollback.sh --deploy-target production")"
assert_rc "no rollback exemption any more, even with commands.rollback set" 2
set_cfg "$P" gate.mode '"ask"'
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")"
assert_rc "gate.mode is gone: production is still a hard block" 2
assert_lacks "gate.mode=ask produces no ask JSON" "$OUT" "permissionDecision"
set_cfg "$P" protect.deploy_approval_env '"CHANGE_TICKET"'
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")" CHANGE_TICKET=CHG-1
assert_rc "custom deploy_approval_env honored" 0
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")" RELEASE_APPROVAL=x
assert_rc "default approval env ignored when deploy_approval_env is custom" 2
assert_has "…and the message names the custom variable" "$ERR" "CHANGE_TICKET"
set_cfg "$P" protect.deploy_approval_env '"bad name"'
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")" RELEASE_APPROVAL=x
assert_rc "an unusable deploy_approval_env name falls back to RELEASE_APPROVAL" 0
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")"
assert_lacks "…and never crashes on it" "$ERR" "bad substitution"
R=$(fresh); set_cfg "$R" protect.deploy_patterns '["ship-it"]'
run_hook guard-bash "$R" "$(j_bash "$R" "ship-it now")"
assert_rc "custom deploy_patterns: a match is gated" 2
run_hook guard-bash "$R" "$(j_bash "$R" "kubectl apply -f x -n production")"
assert_rc "custom deploy_patterns replace the default list" 0
set_cfg "$R" protect.deploy_patterns '[]'
run_hook guard-bash "$R" "$(j_bash "$R" "kubectl apply -f x -n production")"
assert_rc "an explicit empty deploy_patterns switches the gate off" 0
set_cfg "$R" protect.deploy_patterns '["([", "ship-it"]'
run_hook guard-bash "$R" "$(j_bash "$R" "ship-it now")"
assert_rc "an invalid pattern does not disable the valid ones" 2
run_hook guard-bash "$R" "$(j_bash "$R" "echo hello")"
assert_rc "an invalid pattern never blocks unrelated commands" 0

# ======================================================================
echo "-- guard-bash: the deploy gate reads simple commands, not the whole text"
GP=$(fresh)
gate_pass()  { local c="$1"; shift; run_hook guard-bash "$GP" "$(j_bash "$GP" "$c")" "$@"; assert_rc "deploy gate passes: $(short "$c")" 0; }
gate_block() { local c="$1"; shift; run_hook guard-bash "$GP" "$(j_bash "$GP" "$c")" "$@"; assert_rc "deploy gate blocks: $(short "$c")" 2; }
# the word lists are the contract: a change to one of them shows up here (the behavior tests below pin both sides of each list)
TEXT_TOOLS="echo printf cat grep egrep fgrep rg ag ack head tail wc sort uniq cut tr diff jq ls stat file du df tree pwd readlink realpath basename dirname which whereis type test [ [[ cd pushd popd true false : date uname hostname id whoami yq xq column nl od xxd hexdump strings md5sum sha1sum sha256sum shasum cksum cmp comm join paste fold fmt rev tac seq sleep"      # not less and more: they run what a ! line or LESSOPEN says; not sed, awk, find: they run what -i, e, system() and -exec say
SHELL_CMDS="sh bash zsh dash ksh ssh"
GIT_LOCAL="commit log show diff grep checkout switch branch tag status add rm mv merge fetch pull stash remote cherry-pick restore reset revert blame clean describe ls-files rev-parse rev-list shortlog worktree apply am archive bundle clone init notes reflog format-patch cat-file show-ref name-rev for-each-ref diff-tree range-diff cherry show-branch gc fsck prune maintenance sparse-checkout"
GIT_RUNS="rebase bisect submodule difftool mergetool config"       # they run what they are given (--exec, run, foreach, a tool, an alias): not on the list
GIT_C_INERT="user.name=* user.email=*"                              # the -c settings that run nothing: they name the committer
ENV_INERT="LANG=* LC_*=* TZ=* TERM=* NO_COLOR=* COLUMNS=* GIT_PAGER=cat GIT_PAGER= PAGER=cat PAGER= GIT_EDITOR=true GIT_EDITOR=: GIT_SEQUENCE_EDITOR=true GIT_SEQUENCE_EDITOR=: GIT_AUTHOR_NAME=* GIT_AUTHOR_EMAIL=* GIT_AUTHOR_DATE=* GIT_COMMITTER_NAME=* GIT_COMMITTER_EMAIL=* GIT_COMMITTER_DATE=*"      # language, time zone, terminal; a pager that is cat or empty; author and committer
assert_eq "the text tools the gate skips are exactly the specified ones" "$(sed -n 's/^SDLC_TEXT_TOOLS="\(.*\)"$/\1/p' "$PLUGIN/scripts/hooks/guard-bash.sh")" "$TEXT_TOOLS"
assert_eq "the shells whose heredoc bodies the gate reads are exactly the specified ones" "$(sed -n 's/^SDLC_SHELLS="\(.*\)"$/\1/p' "$PLUGIN/scripts/hooks/guard-bash.sh")" "$SHELL_CMDS"
assert_eq "the git commands the gate skips are exactly the listed ones (push is not among them)" "$(sed -n 's/^SDLC_GIT_LOCAL="\(.*\)"$/\1/p' "$PLUGIN/scripts/hooks/guard-bash.sh")" "$GIT_LOCAL"
assert_eq "the -c settings a git command may carry and still be skipped are exactly the listed ones" "$(sed -n 's/^SDLC_GIT_C_INERT="\(.*\)"$/\1/p' "$PLUGIN/scripts/hooks/guard-bash.sh")" "$GIT_C_INERT"
assert_eq "the variables that may stand in front of a skipped git command are exactly the listed ones" "$(sed -n 's/^SDLC_ENV_INERT="\(.*\)"$/\1/p' "$PLUGIN/scripts/hooks/guard-bash.sh")" "$ENV_INERT"

# what the whole-text match refused although nothing was deployed
FALSE_POSITIVES=(
  'git commit -m "fix deploy to production docs"'
  'grep -r deploy config/production.yml'
  'echo "release to prod"'
  $'cat > notes.md <<EOF\nrelease notes: the deploy goes to production\nand the rollout to live follows\nEOF'
  $'python3 - <<\'EOF\'\nimport json\ndeploy_patterns = ["production"]\nprint(json.dumps(deploy_patterns))\nEOF'
  'git log --grep=deploy -- prod/'
  'git checkout -b release-prod'
  'rg "promote.*live"'
  "printf 'deploy prod\\n' > x.txt"
  'ls # deploy to prod'
  $'ls\n# deploy to production\nls'
  './gen.sh > prod-deploy.log'
  'cat deploy.log | grep prod | head'
  'git status && git diff release..prod -- deploy/'
  $'git commit -m "$(cat <<\'EOF\'\nFix the deploy script for production (see #12)\n\nCo-Authored-By: someone\nEOF\n)"'
  $'git commit -m "$(cat <<\'EOF\'\nthe user\'s "release" to prod :)\nEOF\n)" && git status'
  'git commit -m "document \`./deploy.sh prod\` usage"'
  'echo "release $(date) to prod"'
  "echo '\$(./deploy prod)'"
  'FOO=release-prod echo hi'
  'wc -l < prod/deploy.log'
  'sort < deploy-prod.txt | uniq'
  'kubectl apply -f - < k8s/staging/app.yaml > prod.log'
)
for c in "${FALSE_POSITIVES[@]}"; do gate_pass "$c"; done

# commands, tools and the ways to wrap them: still gated
for c in './scripts/deploy.sh production' 'make deploy ENV=prod' 'npm run deploy:prod' 'kubectl apply -f k8s/prod/' \
         'helm upgrade app ./chart -f values-production.yaml' 'terraform apply -var env=prod' 'make deploy-prod' 'npm run release:production' \
         'gh workflow run deploy.yml -f env=production' 'docker compose -f prod.yml up deploy' './DEPLOY.sh PROD' 'bash scripts/deploy-prod.sh' \
         'source ./deploy-prod.sh' '. ./deploy-prod.sh' 'eval "./deploy prod"' 'xargs -a prod-hosts.txt ./deploy.sh' \
         'find . -name x -exec ./deploy.sh prod {} \;' 'python3 -c "import os; os.system(\"./deploy prod\")"' \
         'bash -c "deploy prod"' "ssh host 'release production'" 'sudo ./deploy.sh prod' 'cd x && ./deploy prod' '$(./deploy prod)' \
         '`./deploy prod`' 'echo hi; ./deploy prod' 'true && ./deploy prod' 'false || ./deploy prod' 'echo x | ./deploy prod' \
         './deploy prod &' 'nohup ./deploy prod > out.log 2>&1 &' '(./deploy prod)' '{ ./deploy prod; }' 'if ./deploy prod; then echo ok; fi' \
         '! ./deploy prod' 'diff <(./deploy prod) b' 'x=$(./deploy prod)' 'x=`./deploy prod`' 'echo $(./deploy prod)' \
         './deploy >out.log prod' '>out.log ./deploy prod' '"./deploy" prod' 'dep"loy" prod' 'd\eploy prod' 'ls a#b; ./deploy prod' \
         $'ls\n./deploy prod' $'ls\r\n./deploy prod\r\n' $'# note\n./deploy prod' './deploy \
 prod' $'kubectl apply \\\n  -f x.yaml \\\n  -n production' 'terraform   apply   -var env=prod' $'terraform\tapply\t-var env=prod' \
         'git commit -m "x" && ./deploy prod' 'ls -la | ./deploy.sh production' \
         'kubectl apply -f - < k8s/prod/app.yaml' 'kubectl apply -f - <k8s/prod/app.yaml' '< k8s/prod/app.yaml kubectl apply -f -' \
         'kubectl apply -f - 0< k8s/prod/app.yaml' 'helm upgrade app ./chart -f - < values-production.yaml' 'terraform apply < prod.tfvars' \
         'docker run --rm alpine echo release to prod' 'ssh host cat deploy-prod.log' 'xargs grep -l deploy prod/' 'sudo -u cat ./deploy prod' \
         'make deploy ENV=prod head' \
         'ENV=prod make deploy' 'DEPLOY_TARGET=production ./run.sh release' 'env ENV=production ./deploy.sh' 'sudo -u production ./release.sh'; do
  gate_block "$c"
done

# the text tools: each one skipped, with and without a path, and every neighbor still checked
for t in $TEXT_TOOLS; do
  gate_pass "$t release to prod"
  gate_pass "/usr/bin/$t deploy production"
done
for t in xargs make npm yarn pnpm python python3 node perl ruby tee gh kubectl docker rm mv cp ln touch mkdir chmod tar scp rsync \
         pip go cargo mvn gradle open code vim export set bash sh zsh dash ksh ssh less more; do
  gate_block "$t release to prod"
done
# 1.2: these only read while nothing on the line makes them run a program — a deploy word among their arguments is a name, a pattern
# or a path (sed and awk without -i / an e command / system(); find without -exec; ls, cd, test; a test runner; curl without -X POST/-d)
for t in sed awk find ls pytest curl cd test; do
  gate_pass "$t release to prod"
done
# a pager is no text tool: it runs what a ! line or LESSOPEN says
gate_block '/usr/bin/less deploy production'
gate_block '/usr/bin/more deploy production'
gate_block 'sudo less release to prod'
gate_block 'git log | less +/deploy prod'
gate_block "LESSOPEN='|./deploy-prod.sh %s' less x.txt"
gate_pass 'git log --grep=deploy -- prod/ | less'                 # a pager at the end of a pipe is no part of the git command
# a name that only contains a text tool, or is a piece of one, is not that tool
for t in catalog echoes grepx rgrep wcx ech ec ca g r e jqq xjq headers tailor sorted uniqx diffstat; do
  gate_block "$t release to prod"
done

# wrappers are looked through — for the text tool and for the deploy alike
for w in 'sudo' 'sudo -n' 'sudo -u root' 'sudo -E env FOO=1 nice -n 5' 'env' 'env FOO=1' 'env -u FOO' 'time' 'time -p' 'nice' 'nice -n 5' 'command' \
         'nohup' 'exec' 'timeout 60' 'timeout -k 5 60' 'stdbuf -o L' 'doas -u root' 'FOO=1' 'FOO=1 BAR=2' '/usr/bin/env'; do
  gate_pass "$w grep -r deploy prod/"
  gate_block "$w ./deploy.sh prod"
done

# …but a text tool that is told to run a program is checked like any command: rg --pre and --hostname-bin, ag and ack --pager,
# sort --compress-program — as a separate word or after =, wherever in the line, in whatever wrapper
for c in 'rg --pre ./deploy-prod.sh foo' 'rg --pre=./deploy-prod.sh foo' "rg --pre './deploy prod' foo" 'rg --hostname-bin ./deploy-prod.sh foo' \
         'rg --hostname-bin=./deploy-prod.sh foo' 'ag --pager ./deploy-prod.sh foo' "ag --pager='./deploy prod' foo" 'ack --pager=./deploy-prod.sh foo' \
         'sort --compress-program ./deploy-prod.sh big.txt' 'sort --compress-program=./deploy-prod.sh big.txt' 'rg foo --pre ./deploy-prod.sh' \
         'rg -i foo src/ --pre=./deploy-prod.sh' 'sudo rg --pre ./deploy-prod.sh foo' '/usr/bin/rg --pre=./deploy-prod.sh foo' \
         'LC_ALL=C rg --pre=./deploy-prod.sh foo' 'timeout 5 rg --pre ./deploy-prod.sh foo' 'env -i ag --pager ./deploy-prod.sh foo' \
         'FOO=1 sort --compress-program=./deploy-prod.sh x' 'cat x.txt | rg --pre ./deploy-prod.sh foo'; do
  gate_block "$c"
done
for t in $TEXT_TOOLS; do                                       # the option decides, not the tool: any text tool
  gate_block "$t --pre ./deploy-prod.sh x"
  gate_block "$t --pager=./deploy-prod.sh x"
done
gate_pass 'rg --pre ./deploy-prod.sh foo' RELEASE_APPROVAL=REL-4711                # with a release authorization it goes through
for c in 'rg --pretty deploy prod/' "rg --pre-glob '*.gz' deploy prod/" 'rg --hostname deploy prod/' 'ag --pagers deploy prod/' \
         'sort --compress deploy-prod.txt' 'sort --compress-programs deploy-prod.txt' 'sort -T /tmp deploy-prod.txt' "echo '--pre deploy prod'" \
         'echo "use --pager to deploy prod"' 'LC_ALL=C grep -rn deploy prod/' 'grep -r --include=*.yml deploy prod/' 'head -n 5 deploy-prod.log'; do
  gate_pass "$c"                                               # …and the same letters in another word, or inside one, are text
done

# git: what only reads or records is text; push, what runs the program it is given, and anything unknown are checked
GIT_TEXT=('commit -m "release prod"' 'log --grep=release -- prod/' 'show HEAD:deploy/prod.yml' 'diff release..prod' 'grep -r deploy prod/' 'checkout -b release-prod'
  'switch -c deploy-prod' 'branch release-prod' 'tag deploy-prod' 'status' 'stash push -m "deploy prod"' 'add deploy/prod.yml' 'rm deploy/prod.yml'
  'mv deploy/prod.yml deploy/live.yml' 'merge release-prod' 'rebase main' 'fetch origin release/prod' 'pull origin release/prod'
  'remote add deploy-prod https://example.test/r.git' 'cherry-pick deploy-prod' 'restore deploy/prod.yml' 'reset --hard release-prod' 'revert deploy-prod'
  '-C deploy-prod status' '--no-pager log --grep=deploy -- prod/' '-C prod/x commit -m "deploy prod"'
  'commit -m "deploy to production"' 'log --grep=production' 'log --grep="deploy to production"')
for g in "${GIT_TEXT[@]}"; do gate_pass "git $g"; done
for g in $GIT_LOCAL; do
  gate_pass "git $g release to prod"
  gate_pass "/usr/bin/git -C deploy-prod $g release to prod"
done
gate_pass 'git --version'
gate_pass 'git help deploy'
for g in deploy release ship publish promote rollout flow hf zzz comm rev ch st pul fet; do          # …and names that are only a piece of a listed one
  gate_block "git $g release production"
  gate_block "git -C x --no-pager $g deploy prod"
  gate_block "git -C x -c a=b $g deploy prod"
done
gate_block 'git release prod'
gate_block 'git push-prod release prod'
gate_pass "git push origin feature/x"
gate_pass "git push"
for g in 'push origin release/production' 'push -u origin deploy-prod' 'push deploy-prod main' '-C prod-repo push origin deploy' 'push --tags origin release-live' \
         '-c a=b push origin HEAD:release/prod' 'push origin :release/prod' 'push --force origin deploy/production'; do
  gate_block "git $g"
done

# …and so are the git commands that run what they are given: those that are not on the list (--exec, run, foreach, a tool, an alias) …
for g in $GIT_RUNS; do
  gate_block "git $g release to prod"
  gate_block "/usr/bin/git -C deploy-prod $g release to prod"
done
for g in "submodule foreach 'make deploy-prod'" 'rebase -i --exec "./deploy prod"' 'rebase --exec ./deploy-prod.sh main' 'bisect run ./release-production.sh' \
         "config alias.d '!deploy prod'" 'config core.pager "./deploy prod"' 'difftool -x ./deploy-prod.sh' 'difftool --extcmd="./deploy prod"' \
         'mergetool --tool=deploy-prod'; do
  gate_block "git $g"
done
gate_pass 'git rebase main'
gate_pass 'git rebase --exec ./deploy-prod.sh main' RELEASE_APPROVAL=REL-4711        # with a release authorization it goes through
# … -c and --config-env before the subcommand (an alias, a pager, an ssh command … set on the line: any git command can run one) …
for g in "-c core.pager='deploy prod' log" '-c core.pager=./deploy-prod.sh log' '-C prod/x -c a=b commit -m "deploy prod"' \
         "-c alias.d='!deploy prod' d" '-c a=b commit -m "deploy to production"' '--config-env=core.pager=DEPLOY_PROD_PAGER log' \
         '--config-env core.pager=DEPLOY_PROD_PAGER log'; do
  gate_block "git $g"
done
gate_block 'git -c log log --grep=deploy -- prod/'             # what follows -c is its value, not the subcommand (even when it reads like one)
gate_block 'git --config-env log log --grep=deploy -- prod/'
gate_block 'git --exec-path=deploy-prod -c'                    # a -c with no value after it: checked (and no crash reading past the last word)
# … except a -c that only sets who the committer is: user.name and user.email run nothing. Any other -c on the line still decides.
for g in 'commit -m "release prod"' 'log --grep=deploy -- prod/' 'status deploy/prod' 'diff release..prod'; do
  gate_pass "git -c user.name=x $g"
  gate_pass "git -c user.email=a@b.test $g"
  gate_pass "git -c user.name=x -c user.email=a@b.test $g"
  gate_pass "git -c user.email=a@b.test -c user.name=x $g"
done
gate_pass 'git -c user.name="Deploy Prod" commit -m "x"'       # the value is a name, whatever it says
gate_pass 'git -c user.name=release-prod log'
gate_pass 'git -c user.name=x -c user.name=y commit -m "release prod"'
gate_pass 'git -C deploy/prod -c user.name=x log'
gate_pass 'git -c user.name=x -C deploy/prod log'
gate_pass 'git -c user.name=x --no-pager log --grep=deploy -- prod/'
gate_pass 'sudo git -c user.name=x log --grep=deploy -- prod/'
gate_pass '/usr/bin/git -c user.email=x@y.test status deploy/prod'
gate_pass 'if git -c user.name=x commit -m "release prod"; then ls; fi'
for g in "-c user.name=x -c core.pager='./deploy prod' log" '-c core.pager=./deploy-prod.sh -c user.name=x log' "-c user.name=x -c alias.d='!deploy prod' d" \
         '-c user.name=x -c user.email=y -c core.pager=./deploy-prod.sh log' '-c user.name=x -c a=b commit -m "deploy to production"' \
         '-c user.name=x -c log log --grep=deploy -- prod/' '-c user.name log --grep=deploy -- prod/' '-c user.nameX=y log --grep=deploy -- prod/' \
         '-c user.names=y log --grep=deploy -- prod/' '-c user.email.x=y log --grep=deploy -- prod/' '-c core.user.name=x log --grep=deploy -- prod/' \
         '-c User.Name=x log --grep=deploy -- prod/' '-c user.signingkey=x commit -m "deploy to production"' \
         '-c user.useConfigOnly=true commit -m "deploy to production"' '--config-env=user.name=NAME_VAR log --grep=deploy -- prod/' \
         '--config-env user.name=NAME_VAR log --grep=deploy -- prod/' '-c user.name=x push origin deploy-prod' \
         '-c user.name=x fetch --upload-pack ./deploy-prod.sh ./repo' '-c user.name=x rebase --exec ./deploy-prod.sh main' \
         '-c user.name=x clone -u ./deploy-prod.sh ./a ./b'; do
  gate_block "git $g"                                          # …and the line is still checked by everything else
done
gate_block "sudo git -c core.pager='./deploy prod' -c user.name=x log"
for k in core.pager core.sshCommand core.editor core.fsmonitor core.hooksPath core.askPass alias.d alias.x gpg.program diff.external credential.helper \
         protocol.ext.allow url.x.insteadOf http.proxy sequence.editor commit.gpgsign advice.detachedHead color.ui core.quotepath init.defaultBranch \
         user.signingkey user.useConfigOnly; do
  gate_block "git -c $k=x log --grep=deploy -- prod/"          # a listed subcommand: it is the setting that decides, whatever it sets
done
gate_pass 'git log -c --grep=deploy -- prod/'                  # the -c of a subcommand is not a global option, -C is not -c, --exec-path is not --exec
gate_pass 'git commit -c HEAD -m "release prod"'
gate_pass 'git grep -c deploy prod/'
gate_pass 'git -C deploy/prod log --grep=deploy'
gate_pass 'git --exec-path=/usr/lib/git-core log --grep=deploy -- prod/'
gate_pass 'git --git-dir deploy/prod/.git log --oneline'       # the other global options take their value as the next word, or after =
gate_pass 'git --work-tree deploy/prod status'
gate_pass 'git --namespace deploy-prod log'
gate_pass 'git --exec-path deploy/prod log'
gate_pass 'git --super-prefix deploy/prod/ status'
gate_pass 'git --git-dir=deploy/prod/.git --work-tree=deploy/prod status'
# … --upload-pack, --receive-pack and --exec (fetch, pull, clone and archive run that program, on a local path), the config of a clone
# (its own ssh command) and the pager of grep …
for g in "fetch --upload-pack 'deploy prod' ./repo" "fetch --upload-pack='./deploy prod' ./repo" 'pull --upload-pack ./deploy-prod.sh ./repo' \
         'pull --upload-pack=./deploy-prod.sh ./repo' 'clone --upload-pack="./deploy prod" ./a ./b' 'clone -u ./deploy-prod.sh ./a ./b' \
         'clone -u./deploy-prod.sh ./a ./b' "fetch --receive-pack='./deploy prod' ./repo" 'fetch --receive-pack ./deploy-prod.sh ./repo' \
         "archive --remote=./repo --exec='./deploy prod' HEAD" 'archive --exec ./deploy-prod.sh --remote=./r HEAD' \
         'clone -c core.sshCommand=./deploy-prod.sh git@h:r.git' 'clone -ccore.sshCommand=./deploy-prod.sh git@h:r.git' \
         "clone --config core.sshCommand='./deploy prod' x" 'clone --config=core.sshCommand=./deploy-prod.sh x' \
         'grep -O./deploy-prod.sh foo' 'grep -O ./deploy-prod.sh foo' "grep --open-files-in-pager='./deploy prod' foo" \
         'grep --open-files-in-pager ./deploy-prod.sh foo'; do
  gate_block "git $g"
done
for g in 'fetch -u origin release/prod' 'pull -u origin release/prod' 'status -uno deploy/prod' 'commit -u -m "release prod"' 'grep -c "deploy.*prod" -- src/' \
         'diff -O deploy/prod.order' 'log --execute release prod' 'clone git@github.com:acme/deploy-prod.git' \
         'clone -q --depth 1 https://example.test/release-prod.git' 'archive -o deploy-prod.tar HEAD' 'commit -m "--exec release prod"' \
         'log "--grep=--exec" -- prod/deploy'; do
  gate_pass "git $g"                                           # …and the same letters elsewhere are text
done
# … and a VAR=value in front of git (GIT_SSH_COMMAND, GIT_PAGER, GIT_EXTERNAL_DIFF, GIT_CONFIG_* … configure it as well)
for c in "GIT_SSH_COMMAND='./deploy prod' git fetch" "env GIT_PAGER='./deploy prod' git log" 'sudo GIT_EXTERNAL_DIFF=./deploy-prod.sh git diff' \
         "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0='./deploy prod' git log" 'FOO=1 git commit -m "deploy to production"' \
         'FOO=1 BAR=2 /usr/bin/git status deploy/prod'; do
  gate_block "$c"
done
for c in 'sudo --user=root git log --grep=deploy -- prod/' 'sudo -u root git commit -m "release prod"' 'env git log --grep=deploy -- prod/' \
         'env -i git log --grep=deploy -- prod/' 'env -u FOO git log --grep=deploy -- prod/' 'time git commit -m "release prod"' \
         'command git log --grep=deploy -- prod/' 'nice -n 5 git diff release..prod'; do
  gate_pass "$c"                                               # …a wrapper and its options are no assignment
done
# … except one that runs nothing: a language, time zone, terminal or colour setting, who the author and committer are (and when), or a pager that is cat or empty (git starts none)
for c in 'LANG=C' 'LANG=en_US.UTF-8' 'LC_ALL=C' 'LC_MESSAGES=C' 'LC_CTYPE=C.UTF-8' 'TZ=UTC' 'TERM=dumb' 'NO_COLOR=1' 'COLUMNS=200' 'GIT_PAGER=cat' \
         'PAGER=cat' 'GIT_PAGER=' 'PAGER=' "GIT_PAGER=''" 'GIT_PAGER="cat"' 'LANG=C LC_ALL=C TZ=UTC' \
         'LANG=C TERM=dumb NO_COLOR=1 COLUMNS=80 GIT_PAGER=cat PAGER=' 'env LC_ALL=C' 'sudo LANG=C' 'env -i LANG=C' 'env LANG=C PAGER=cat'; do
  gate_pass "$c git log --grep=deploy -- prod/"
done
gate_pass 'LANG=C git commit -m "deploy to production" && ls'
gate_pass 'if LANG=C git log --grep=deploy -- prod/; then ls; fi'
gate_pass 'LANG=C git -c user.name=x log --grep=deploy -- prod/'
gate_pass 'LC_ALL=C GIT_PAGER=cat git -C deploy/prod log --grep=deploy'
for v in GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_AUTHOR_DATE GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL GIT_COMMITTER_DATE; do
  gate_pass "$v=x git commit -m \"release prod\""              # who the author and committer are (and when): runs nothing
  gate_pass "$v= git commit -m \"release prod\""               # …an empty value too
done
gate_pass 'GIT_AUTHOR_NAME=x GIT_COMMITTER_NAME=x git commit -m "release prod"'
gate_pass "GIT_AUTHOR_NAME='Deploy Prod' GIT_AUTHOR_EMAIL=dp@example.test git commit -m x"      # …a name with a deploy in it is a name
gate_pass 'GIT_COMMITTER_DATE="2026-01-01 00:00:00" git commit -m "release prod"'
gate_pass 'env GIT_AUTHOR_NAME=x GIT_COMMITTER_NAME=x git commit -m "release prod"'
# … any other variable, by name, and a pager that is anything but cat or empty, still decide
for v in PATH HOME EDITOR VISUAL GIT_EDITOR GIT_SEQUENCE_EDITOR GIT_SSH GIT_SSH_COMMAND GIT_EXTERNAL_DIFF GIT_ASKPASS SSH_ASKPASS GIT_PROXY_COMMAND \
         GIT_CONFIG_GLOBAL GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS GIT_DIR GIT_WORK_TREE GIT_EXEC_PATH GIT_TEMPLATE_DIR GIT_TRACE LESS LESSOPEN \
         LD_PRELOAD BASH_ENV ENV SHELL COLORTERM CLICOLOR LINES FOO; do
  gate_block "$v=x git log --grep=deploy -- prod/"
done
for c in 'GIT_PAGER=less' 'PAGER=less' "GIT_PAGER='cat -v'" 'GIT_PAGER=/bin/cat' 'GIT_PAGER=CAT' 'GIT_PAGER=catx' 'GIT_PAGER=xcat' 'PAGER=xcat' \
         'GIT_PAGER=cat PAGER=less' 'GIT_PAGER=cat FOO=1' 'FOO=1 GIT_PAGER=cat' 'LANG=C GIT_SSH_COMMAND=x' 'LC_ALL=C GIT_PAGER=less' \
         'LANGUAGE=en' 'TZDIR=/x' 'TERMINFO=/x' 'TERMCAP=x' 'NOCOLOR=1' 'NO_COLORS=1' 'COLUMN=1' 'COLUMNS2=1' 'LCX=1' 'LC=1' 'TERM2=x' 'TZ2=x' 'LANG2=x' \
         'GIT_PAGER2=cat' 'PAGER2=cat' 'XPAGER=cat' 'env GIT_PAGER=less' 'sudo PAGER=less' 'env LANG=C GIT_SSH_COMMAND=x' \
         'FOO=' 'GIT_SSH_COMMAND=' 'LESS=' 'LANG=C FOO=' 'GIT_AUTHOR_X=x' 'GIT_COMMITTER_X=x' 'GIT_AUTHOR=x' 'GIT_COMMITTER=x'; do        # …an empty value is no exception for any other variable
  gate_block "$c git log --grep=deploy -- prod/"
done
for v in GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_AUTHOR_DATE GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL GIT_COMMITTER_DATE; do
  gate_block "${v}S=x git commit -m \"release prod\""          # …the six names are exact: a longer or a prefixed one is another variable
  gate_block "X$v=x git commit -m \"release prod\""
done
gate_block "GIT_PAGER='./deploy prod' git log"
gate_block "PAGER='cat; ./deploy prod' git log"
gate_block 'GIT_AUTHOR_NAME=x GIT_COMMITTER_NAME=x GIT_SSH_COMMAND=./deploy-prod.sh git commit -m "release prod"'      # any other variable beside them decides
gate_block 'GIT_SSH_COMMAND=./deploy-prod.sh GIT_AUTHOR_NAME=x git commit -m "release prod"'
gate_block 'env GIT_AUTHOR_NAME=x GIT_SSH_COMMAND=./deploy-prod.sh git commit -m "release prod"'
gate_block 'GIT_AUTHOR_NAME=$(./deploy-prod.sh) git commit -m x'                      # a value is any text, a substitution in it is still a command of its own
gate_block 'GIT_AUTHOR_NAME="$(./deploy-prod.sh)" git commit -m x'
gate_block 'GIT_COMMITTER_EMAIL=`./deploy-prod.sh` git commit -m x'
gate_block 'LANG=C git push origin deploy-prod'                # the exceptions are for the prefix: the rest of the line is checked as before
gate_block 'LANG=C git rebase --exec ./deploy-prod.sh main'
gate_block 'LANG=C git fetch --upload-pack ./deploy-prod.sh ./repo'
gate_block "TZ=UTC git -c core.pager='./deploy prod' log"
# …wherever git stands in the command line
gate_block "sudo git -c core.pager='deploy prod' log"
gate_block "/usr/bin/git -c core.pager='deploy prod' log"
gate_block 'sudo git fetch --upload-pack ./deploy-prod.sh ./repo'
gate_block "if GIT_PAGER='./deploy prod' git log; then ls; fi"
gate_pass 'if git commit -m "release prod"; then ls; fi'
gate_pass 'sudo git commit -m "release prod" && ls'

# heredocs: the body is data — unless its command is a shell, which runs it
for s in $SHELL_CMDS; do
  gate_block "$(printf '%s <<EOF\n./deploy prod\nEOF' "$s")"
  gate_block "$(printf "%s <<'EOF'\nls\n./deploy prod\nEOF" "$s")"
  gate_block "$(printf '%s <<-EOF\n\tls\n\t./deploy prod\n\tEOF' "$s")"
  gate_block "$(printf '%s -s -- arg <<EOF\n./deploy prod\nEOF' "$s")"
  gate_pass "$(printf '%s <<EOF\nls -la\necho release to prod\nEOF' "$s")"
done
gate_block $'sudo bash <<EOF\n./deploy prod\nEOF'
gate_block $'env X=1 bash <<EOF\n./deploy prod\nEOF'
# a <<- body with a command substitution: what follows the body is read from the right place, a body line is not taken for a command
gate_pass $'cat <<-EOF\n\t$(date)\n\trelease to prod\n\tEOF\nls'
gate_pass $'cat <<-EOF\n\t`date`\n\trelease to prod\n\tEOF\nls'
gate_block $'cat <<-EOF\n\t$(date)\n\tEOF\n./deploy prod'
gate_block $'bash <<-EOF\n\t$(date)\n\t./deploy prod\n\tEOF'
gate_block $'/bin/bash <<EOF\nls\n./deploy prod\nEOF'
gate_block $'bash <<EOF | tee out.log\n./deploy prod\nEOF'
gate_block $'bash <<EOF\nif true; then ./deploy prod; fi\nEOF'
gate_block $'bash <<EOF\nfor e in a b; do make deploy ENV=prod; done\nEOF'
gate_block $'bash <<EOF\ncat <<INNER\nx\nINNER\n./deploy prod\nEOF'
gate_pass $'bash <<EOF\ncat <<INNER\n./deploy prod\nINNER\nEOF'
gate_block $'bash <<EOF\n./deploy prod'
gate_pass $'cat <<EOF\nrelease prod'
for o in 'cat' 'cat > notes.md' 'tee notes.md' 'python3 -' 'python3' 'ruby' 'node' 'perl' 'psql' 'mysql -u x' 'sort' 'wc -l' 'jq .' 'sed s/a/b/' 'awk 1'; do
  gate_pass "$(printf "%s <<'EOF'\nrelease the build to production\n./deploy prod\nEOF" "$o")"
  gate_pass "$(printf '%s <<EOF\nrelease the build to production\n./deploy prod\nEOF' "$o")"
done
gate_block $'cat <<EOF\nx\nEOF\n./deploy prod'
gate_block $'cat <<EOF\nrelease prod\nEOF\nmake deploy ENV=prod'
gate_block $'cat <<EOF > f.txt\nrelease prod\nEOF\nmake deploy ENV=prod'
gate_pass $'cat <<A; cat <<B\nrelease prod\nA\ndeploy production\nB'
gate_block $'cat <<A; bash <<B\nrelease prod\nA\n./deploy production\nB'
gate_block $'bash <<A; cat <<B\n./deploy production\nA\nrelease prod\nB'

# command substitutions run their commands wherever they sit; escaped or single-quoted ones are text
for c in 'echo "x $(./deploy prod) y"' 'echo "x `./deploy prod` y"' 'git commit -m "msg $(./deploy prod)"' 'echo "$(echo "$(./deploy prod)")"' \
         'echo "$(echo "$(echo "$(echo "$(echo "$(./deploy prod)")")")")"' $'cat <<EOF\n$(./deploy prod)\nEOF' $'cat <<EOF\nnow `./deploy prod`\nEOF' \
         $'cat <<EOF\n$(echo $(./deploy prod))\nEOF' $'git commit -m "$(cat <<EOF\n$(./deploy prod)\nEOF\n)"'; do
  gate_block "$c"
done
for c in 'echo "x \$(./deploy prod) y"' 'git commit -m "document \`./deploy.sh prod\` usage"' "echo '\$(./deploy prod)'" $'cat <<\'EOF\'\n$(./deploy prod)\nEOF' \
         $'cat <<\\EOF\n$(./deploy prod)\nEOF' $'cat <<EOF\n\\$(./deploy prod)\nEOF'; do
  gate_pass "$c"
done
# 16 looked-into substitutions per command line; the rest is matched as text, so a deploy behind them is still seen
c='echo'; n=0; while [ "$n" -lt 20 ]; do c="$c \"\$(date)\""; n=$((n + 1)); done
gate_pass "$c"
gate_block "$c \"\$(./deploy prod)\""
gate_block "$c \"\$(echo release to prod)\""                          # …and past the 16th a mention is text to match, not a command to read
c='echo'; n=0; while [ "$n" -lt 10 ]; do c="$c \"\$(date)\""; n=$((n + 1)); done
gate_pass "$c \"\$(echo release to prod)\""                          # …below them it is read like any command
# 1000 simple commands per command line are read; what follows is matched as text
BIG_CMDS=$(awk 'BEGIN { for (i = 0; i < 1100; i++) printf "ls ; " }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_CMDS echo \"release to prod\"")"
assert_rc "past 1000 simple commands a mention is matched as text and blocked" 2
BIG_CMDS=$(awk 'BEGIN { for (i = 0; i < 900; i++) printf "ls ; " }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_CMDS echo \"release to prod\"")"
assert_rc "below 1000 simple commands the same mention is text" 0
unset BIG_CMDS

# a shell that reads its commands from a pipe, a here-string or a file: the tokenizer cannot follow them, the whole line is matched as text
for s in $SHELL_CMDS; do
  gate_block "echo \"./deploy prod\" | $s"
  gate_block $'cat <<EOF | '"$s"$'\n./deploy prod\nEOF'
  gate_block "$s <<< \"./deploy prod\""
  gate_block "$s < deploy-prod.sh"
  gate_pass "echo \"ls\" | $s"
done
gate_block 'cat prod-deploy.sh | sh'
gate_block 'cat prod-deploy.sh | sudo bash -s'
gate_pass 'cat prod-deploy.sh'
gate_pass 'grep -rl release prod/ | xargs wc -l'

# a command that only mentions it: approved or not, nothing is logged for it
P=$(fresh)
run_hook guard-bash "$P" "$(j_bash "$P" 'echo "release to prod"')"
assert_true "a mention is silent: no gate event" "$([ -z "$OUT$ERR" ] && [ "$(log_has "$P" hook.gate)" = false ] && echo true || echo false)" "out=$OUT err=$ERR"
run_hook guard-bash "$P" "$(j_bash "$P" 'echo "release to prod"')" RELEASE_APPROVAL=REL-9
assert_true "…also with an approval set: no 'approved' event for a command that deploys nothing" "$([ "$(log_has "$P" hook.gate)" = false ] && echo true || echo false)" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
gate_pass './deploy.sh prod' RELEASE_APPROVAL=REL-9
gate_pass $'bash <<EOF\n./deploy prod\nEOF' RELEASE_APPROVAL=REL-9
gate_pass 'git push origin release/production' RELEASE_APPROVAL=REL-9
assert_true "an approved deploy is logged as allow" "$(log_has "$GP" '"event":"hook.gate".*"decision":"allow"')" "$(cat "$GP/.sdlc/logs/events.jsonl" 2>/dev/null)"

# the message and the log say which part of the command line matched
run_hook guard-bash "$P" "$(j_bash "$P" 'cd x && ./deploy.sh prod')"
assert_rc "message: a deploy behind other commands is blocked" 2
assert_has "message: names the simple command that matched" "$ERR" ", in './deploy.sh prod'"
assert_has "message: names the pattern" "$ERR" "(deploy|release|rollout|promote)"
assert_has "message: still names the approval env and the config key" "$ERR" "RELEASE_APPROVAL"
assert_has "message: points at protect.deploy_patterns" "$ERR" "protect.deploy_patterns"
assert_has "message: says which commands are not looked at" "$ERR" "except text and read-only tools"
assert_has "message: says that a program's prose arguments are skipped" "$ERR" "prose arguments are skipped"
assert_true "log: the matched part is in the event" "$(log_has "$P" '"part":"\./deploy\.sh prod"')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
run_hook guard-bash "$P" "$(j_bash "$P" './deploy.sh prod')"
assert_lacks "message: one simple command needs no 'in …'" "$ERR" ", in '"
run_hook guard-bash "$P" "$(j_bash "$P" "bash -c 'ls; ./deploy.sh prod'")"
assert_has "message: a quoted argument is shown as the words the gate read" "$ERR" ", in 'bash -c ls; ./deploy.sh prod'"

# a custom pattern list works the same way
R=$(fresh); set_cfg "$R" protect.deploy_patterns '["ship-it"]'
run_hook guard-bash "$R" "$(j_bash "$R" 'echo ship-it now')"
assert_rc "custom pattern: a text tool is skipped here too" 0
run_hook guard-bash "$R" "$(j_bash "$R" 'git commit -m "ship-it"')"
assert_rc "custom pattern: git commit is skipped here too" 0
run_hook guard-bash "$R" "$(j_bash "$R" './ship-it now')"
assert_rc "custom pattern: a command is gated" 2
run_hook guard-bash "$R" "$(j_bash "$R" 'bash -c "ship-it now"')"
assert_rc "custom pattern: a quoted argument is gated" 2
run_hook guard-bash "$R" "$(j_bash "$R" $'bash <<EOF\nship-it now\nEOF')"
assert_rc "custom pattern: a shell heredoc body is gated" 2
run_hook guard-bash "$R" "$(j_bash "$R" $'cat <<EOF\nship-it now\nEOF')"
assert_rc "custom pattern: a data heredoc body is not" 0

# the tokenizer cannot be run or says nothing: the command line is matched as plain text, as the gate did before
AWKSHIM="$WORK/awk-shim"; mkdir -p "$AWKSHIM"; REAL_AWK=$(command -v awk)
shim_awk() { printf '#!/bin/sh\ncase "$*" in *bodies=1*) %s;; esac\nexec %s "$@"\n' "$1" "$REAL_AWK" > "$AWKSHIM/awk"; chmod +x "$AWKSHIM/awk"; }
shim_awk 'exit 1'
run_hook guard-bash "$GP" "$(j_bash "$GP" 'echo "release to prod"')" PATH="$AWKSHIM:$PATH"
assert_rc "tokenizer fails: the whole text is matched (a mention is blocked again)" 2
run_hook guard-bash "$GP" "$(j_bash "$GP" './deploy prod')" PATH="$AWKSHIM:$PATH"
assert_rc "tokenizer fails: a deploy is blocked" 2
run_hook guard-bash "$GP" "$(j_bash "$GP" $'bash <<EOF\n./deploy prod\nEOF')" PATH="$AWKSHIM:$PATH"
assert_rc "tokenizer fails: a shell heredoc deploy is blocked" 2
run_hook guard-bash "$GP" "$(j_bash "$GP" 'ls -la')" PATH="$AWKSHIM:$PATH"
assert_true "tokenizer fails: an ordinary command still passes, silently" "$([ "$RC" = 0 ] && [ -z "$OUT$ERR" ] && echo true || echo false)" "rc=$RC out=$OUT err=$ERR"
shim_awk 'exit 0'
run_hook guard-bash "$GP" "$(j_bash "$GP" 'echo "release to prod"')" PATH="$AWKSHIM:$PATH"
assert_rc "tokenizer prints nothing: the whole text is matched" 2
run_hook guard-bash "$GP" "$(j_bash "$GP" 'ls -la')" PATH="$AWKSHIM:$PATH"
assert_rc "tokenizer prints nothing: an ordinary command passes" 0
shim_awk 'printf "Wls\nC\n"; exit 2'
run_hook guard-bash "$GP" "$(j_bash "$GP" './deploy prod')" PATH="$AWKSHIM:$PATH"
assert_rc "tokenizer fails after a part of its output: the whole text is matched, not the part" 2
run_hook guard-bash "$GP" "$(j_bash "$GP" 'ls -la')" PATH="$AWKSHIM:$PATH"
assert_rc "…and an ordinary command still passes" 0
shim_awk 'printf "Wls\nC\n!\n"; exit 0'
run_hook guard-bash "$GP" "$(j_bash "$GP" './deploy prod')" PATH="$AWKSHIM:$PATH"
assert_rc "tokenizer stops at its record cap: the whole text is matched" 2
run_hook guard-bash "$GP" "$(j_bash "$GP" 'ls -la')" PATH="$AWKSHIM:$PATH"
assert_rc "…and an ordinary command still passes" 0
shim_awk 'printf "W"; head -c 45000 /dev/zero | tr "\0" "a"; printf "\nC\n"; exit 0'
run_hook guard-bash "$GP" "$(j_bash "$GP" 'echo "release to prod"')" PATH="$AWKSHIM:$PATH"
assert_rc "tokenizer prints more than its size cap: the whole text is matched" 2
run_hook guard-bash "$GP" "$(j_bash "$GP" 'ls -la')" PATH="$AWKSHIM:$PATH"
assert_rc "…and an ordinary command still passes (size cap)" 0
printf '#!/bin/sh\nexit 127\n' > "$AWKSHIM/awk"
run_hook guard-bash "$GP" "$(j_bash "$GP" './deploy prod')" PATH="$AWKSHIM:$PATH"
assert_rc "no usable awk at all: a deploy is still blocked" 2
rm -f "$AWKSHIM/awk"
# the tokenizer's own record cap (4000: every word, every end of a simple command and every substitution count one): past it the
# text is matched as a whole — a mention is blocked as it was before, and a deploy behind the cap is not lost
WORDS_4100=$(awk 'BEGIN { for (i = 0; i < 4100; i++) printf " a%d", i }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "ls$WORDS_4100 ; echo \"release to prod\"")"
assert_rc "past 4000 words a mention is matched as text and blocked" 2
run_hook guard-bash "$GP" "$(j_bash "$GP" "ls$WORDS_4100 ; ./deploy prod")"
assert_rc "…and a deploy behind the words is found" 2
WORDS_3000=$(awk 'BEGIN { for (i = 0; i < 3000; i++) printf " a%d", i }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "ls$WORDS_3000 ; echo \"release to prod\"")"
assert_rc "below 4000 words the same mention is text" 0
BLANK_4100=$(awk 'BEGIN { for (i = 0; i < 4100; i++) printf " \n"; printf "x" }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "ls${BLANK_4100} ; echo \"release to prod\"")"
assert_rc "past 4000 line ends a mention is matched as text and blocked" 2
unset WORDS_4100 WORDS_3000 BLANK_4100
# the file a simple command reads its input from belongs to that command alone
gate_pass 'sort < deploy-prod.txt ; ls -la'
gate_pass 'wc -l < prod/deploy.log && ls'
# a name that is only a piece of a shell's name is no shell: its heredoc is text
for t in s h as ash ba bas zs ks da ss; do
  gate_pass "$(printf '%s <<EOF\n./deploy prod\nEOF' "$t")"
  gate_pass "echo \"release to prod\" | $t"
done
gate_pass $'   '
gate_pass ':'
gate_block '# ./deploy prod'
gate_pass 'echo "release prod'
gate_pass "echo 'release prod"
gate_pass $'echo release prod\r\nls\r\n'
gate_pass 'echo "배포 release prod ✓"'
gate_block './deploy.sh 운영 prod'
# non-ASCII text is read as bytes: macOS awk stops dead on a regular expression over half a character unless it runs in the C locale,
# and a long text always has a 256-byte stretch that ends inside a character — the gate must not fall back to the whole text for it
KO=$(awk 'BEGIN { for (i = 0; i < 120; i++) printf "배포" }')
ko_case() { run_hook guard-bash "$GP" "$(j_bash "$GP" "$3")"; assert_rc "$2" "$1"; }
ko_case 0 "non-ASCII text: a long commit message is text" "git commit -m \"$KO release to production\""
ko_case 0 "non-ASCII text: …whatever its alignment in the window (1)" "git commit -m \"x$KO release to production\""
ko_case 0 "non-ASCII text: …(2)" "git commit -m \"xx$KO release to production\""
ko_case 0 "non-ASCII text: a long unquoted text and a mention in echo" "echo $KO release to prod"
ko_case 0 "non-ASCII text: a backslash in front of a character does not end the reading" "echo \\$KO ; echo release to prod"
ko_case 2 "non-ASCII text: a deploy behind a long quoted text is found" "echo \"$KO\" ; ./deploy prod"
ko_case 2 "non-ASCII text: a deploy behind a backslash and a character is found" "echo \\$KO ; ./deploy prod"
ko_case 2 "non-ASCII text: a deploy inside a long quoted argument is found" "bash -c \"$KO ./deploy prod\""
unset -f ko_case; unset KO

# big command lines stay fast: 200 KB of text for a data heredoc, a long shell script, thousands of simple commands or words, a 300 KB
# word, 300 KB of white space, hundreds of substitutions, a long commit message
BIG_TEXT=$(awk 'BEGIN { for (i = 0; i < 3000; i++) printf "line %d about the release of production and the deploy to prod\n", i }')
BIG_SH=$(awk 'BEGIN { for (i = 0; i < 1500; i++) printf "echo step %d\n", i }')
BIG_CMDS=$(awk 'BEGIN { for (i = 0; i < 3000; i++) printf "ls d%d ; ", i; print "true" }')
BIG_MSG=$(awk 'BEGIN { for (i = 0; i < 100; i++) printf "line %d: tidy the deploy script for production\n", i }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "cat > f.txt <<'EOF'"$'\n'"$BIG_TEXT"$'\nEOF')"
assert_true "200 KB data heredoc: passes in under a second" "$([ "$RC" = 0 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "bash <<EOF"$'\n'"$BIG_SH"$'\nEOF')"
assert_true "1500-line shell heredoc: passes in under a second" "$([ "$RC" = 0 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "bash <<EOF"$'\n'"$BIG_SH"$'\n./deploy prod\nEOF')"
assert_true "…and still finds the deploy at its end, in under a second" "$([ "$RC" = 2 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_CMDS")"
assert_true "3000 simple commands: passes in under a second" "$([ "$RC" = 0 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "git commit -m \"\$(cat <<'EOF'"$'\n'"$BIG_MSG"$'\nEOF\n)"')"
assert_true "100-line commit message in a heredoc: passes in under a second" "$([ "$RC" = 0 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
BIG_WORDS=$(awk 'BEGIN { printf "make build"; for (i = 0; i < 8000; i++) printf " a%d", i; print "" }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_WORDS")"
assert_true "8000 words in one command: passes in under a second" "$([ "$RC" = 0 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_WORDS ; ./deploy prod")"
assert_true "…and a deploy behind them is still found, in under a second" "$([ "$RC" = 2 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
BIG_GIT=$(awk 'BEGIN { printf "git add"; for (i = 0; i < 3500; i++) printf " src/f%d.txt", i; print "" }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_GIT")"
assert_true "3500 words of a git command: passes in under a second" "$([ "$RC" = 0 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_GIT --exec ./deploy-prod.sh")"
assert_true "…and the program it is told to run is still found, in under a second" "$([ "$RC" = 2 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
BIG_ECHO=$(awk 'BEGIN { printf "echo"; for (i = 0; i < 3500; i++) printf " w%d", i; print "" }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_ECHO")"
assert_true "3500 words of a text tool: passes in under a second" "$([ "$RC" = 0 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_ECHO --pager ./deploy-prod.sh")"
assert_true "…and the program it is told to run is still found, in under a second" "$([ "$RC" = 2 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
BIG_WORD=$(head -c 300000 /dev/zero | tr '\0' 'A')
run_hook guard-bash "$GP" "$(j_bash "$GP" "make build $BIG_WORD ; ./deploy prod")"
assert_true "a 300 KB word and a deploy behind it: found in under a second" "$([ "$RC" = 2 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
BIG_SPACE=$(head -c 300000 /dev/zero | tr '\0' ' ')
run_hook guard-bash "$GP" "$(j_bash "$GP" "make build${BIG_SPACE}x ; ./deploy prod")"
assert_true "300 KB of white space and a deploy behind it: found in under a second" "$([ "$RC" = 2 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "make build >${BIG_SPACE}out.txt ; ./deploy prod")"
assert_true "300 KB of white space after a redirect operator and a deploy behind it: found in under a second" "$([ "$RC" = 2 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
BIG_SUBST=$(awk 'BEGIN { printf "echo"; for (i = 0; i < 300; i++) printf " \"$(date)\""; print "" }')
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_SUBST")"
assert_true "300 command substitutions in one command: passes in under a second" "$([ "$RC" = 0 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$GP" "$(j_bash "$GP" "$BIG_SUBST \"\$(./deploy prod)\"")"
assert_true "…and a deploy in one more is still found, in under a second" "$([ "$RC" = 2 ] && [ "$MS" -lt 1000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
unset BIG_TEXT BIG_SH BIG_CMDS BIG_MSG BIG_WORDS BIG_GIT BIG_ECHO BIG_WORD BIG_SPACE BIG_SUBST

# ======================================================================
echo "-- guard-bash: no direct push to the default branch"
P=$(fresh_git)
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin main")"
assert_rc "git push origin main blocked" 2
assert_has "direct push message routes to a PR" "$ERR" "gh pr create"
assert_has "direct push message names the 1.0 entry points" "$ERR" "/sdlc:go"
run_hook guard-bash "$P" "$(j_bash "$P" "git push -u origin HEAD:main")"
assert_rc "git push HEAD:main blocked" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git push")"
assert_rc "bare git push while on main blocked" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git push -u origin feature/x")"
assert_rc "push to a feature branch allowed" 0
gitp "$P" checkout -q -b feature/y
run_hook guard-bash "$P" "$(j_bash "$P" "git push")"
assert_rc "bare git push on a feature branch allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin --delete feature/y")"
assert_rc "deleting a remote feature branch allowed" 0
gitp "$P" checkout -q main
for c in "git push origin +main" "git push origin refs/heads/main" "git push -u origin HEAD:refs/heads/main" "git push origin --delete main" "git -C . push origin main" "git push --force-with-lease origin main" "git push origin"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "bypass attempt blocked: $c" 2
done
gitp "$P" checkout -q feature/y
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
echo "-- guard-bash: test-file lock for shell commands (during sdlc work)"
P=$(fresh); mk "$P" bulk/a_test.py; mk "$P" qa/smoke.sh        # existing tests: they are in the default branch (used further down)
git_init "$P"; activate "$P" planned                          # sdlc work: the work doc applies and the tests are locked
mk "$P" node_modules/pkg/test/a.js                         # untracked, like build output and dependencies
BLOCKED=(
  'rm tests/x_test.py'
  'rm -f tests/x_test.py'
  'rm "tests/x_test.py"'
  'rm tests/x_*.py'
  'rm foo.spec.ts'
  'rm src/test/Foo.java'
  'mv tests/x_test.py /tmp/y'
  'mv /tmp/a tests/x_test.py'
  'cp /tmp/a tests/x_test.py'
  'ln -sf /tmp/a tests/x_test.py'
  "sed -i 's/a/b/' tests/x_test.py"
  "sed -i '' 's/a/b/' tests/x_test.py"
  "perl -pi -e 's/a/b/' tests/x_test.py"
  'truncate -s 0 tests/x_test.py'
  'dd if=/dev/zero of=tests/x_test.py'
  'git rm tests/x_test.py'
  'git rm -r tests'
  'git -C . rm tests/x_test.py'
  'git -C tests rm x_test.py'
  'git checkout -- tests/x_test.py'
  'git checkout HEAD~1 -- tests/x_test.py'
  'git restore tests/x_test.py'
  'git restore --source=HEAD~1 tests/x_test.py'
  'echo x > tests/x_test.py'
  'echo x >> tests/x_test.py'
  'echo x>tests/x_test.py'
  'echo x | tee tests/x_test.py'
  'echo x | tee -a tests/x_test.py'
  'cd tests && rm x_test.py'
  'rm -rf tests'
  'rm -rf src'
  'mv tests /tmp/oldtests'
  'rm ./tests/x_test.py'
  'rm tests/../tests/x_test.py'
  "rm $P/tests/x_test.py"
  'echo hi && rm tests/x_test.py'
  'ls; rm tests/x_test.py'
  'sudo rm tests/x_test.py'
  'sudo -u root rm tests/x_test.py'
  'nice -n 5 rm tests/x_test.py'
  'env -u FOO rm tests/x_test.py'
  'time -p rm tests/x_test.py'
  'timeout -k 5 60 rm tests/x_test.py'
  'stdbuf -o L rm tests/x_test.py'
  'doas -u root rm tests/x_test.py'
  'sudo -E env FOO=1 nice -n 5 rm tests/x_test.py'
  '/usr/bin/env rm tests/x_test.py'
  '/usr/bin/sudo -u root /bin/rm tests/x_test.py'
  'FOO=1 rm tests/x_test.py'
  'env FOO=1 rm tests/x_test.py'
  'bash -c "rm tests/x_test.py"'
  "sh -c 'cd tests && rm x_test.py'"
  'eval "rm tests/x_test.py"'
  $'cat <<EOF > tests/x_test.py\nhello\nEOF'
  $'echo start\nrm tests/x_test.py\n'
)
for c in "${BLOCKED[@]}"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "blocked on an existing test: $(short "$c")" 2
done
# non-ASCII text does not end the reading early: a long text (some 256-byte stretch of it ends inside a character) or a backslash in
# front of a character must not hide what comes after (macOS awk stops dead on half a character unless it runs in the C locale)
KO=$(awk 'BEGIN { for (i = 0; i < 120; i++) printf "배포" }')
ko_n=0
for c in "echo \"$KO\" ; rm tests/x_test.py" "echo x$KO ; rm tests/x_test.py" "echo \"xx$KO\" ; rm tests/x_test.py" \
         "echo \\$KO ; rm tests/x_test.py" "git commit -m \"$KO\" ; rm tests/x_test.py"; do
  ko_n=$((ko_n + 1))
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "blocked on an existing test behind non-ASCII text (case $ko_n)" 2
done
PK=$(fresh); mk "$PK" 'tests/한글_test.py'; git_init "$PK"; activate "$PK" planned
run_hook guard-bash "$PK" "$(j_bash "$PK" 'rm tests/한글_test.py')"
assert_rc "a test file with a non-ASCII name is locked like any other" 2
run_hook guard-bash "$PK" "$(j_bash "$PK" "echo \"$KO\" ; rm 'tests/한글_test.py'")"
assert_rc "…also behind a long non-ASCII text" 2
run_hook guard-bash "$PK" "$(j_bash "$PK" 'rm tests/한글_new.py')"
assert_rc "…and a new test with a non-ASCII name is free" 0
unset KO ko_n PK
ALLOWED=(
  'rm tests/new_test.py'
  'mv /tmp/a tests/new_test.py'
  'cp /tmp/a tests/new_test.py'
  'cp /tmp/a tests/'
  'cp tests/x_test.py /tmp/b'
  'sed -i s/a/b/ tests/new_test.py'
  'sed -n 1p tests/x_test.py'
  'git restore --staged tests/x_test.py'
  'echo x > tests/new_test.py'
  'echo x | tee tests/new_test.py'
  'pytest tests/ > /tmp/out.txt 2>&1'
  'cat tests/x_test.py'
  'grep -n x tests/x_test.py'
  'git status && git diff tests/x_test.py'
  'git log --oneline -5'
  'git checkout main'
  'git checkout -b sdlc/foo'
  'ls -la'
  'rm src/app.py'
  'rm -rf src/gen'
  'rm -rf build dist'
  'rm -rf node_modules'
  'sed -i s/a/b/ src/app.py'
  'echo x > src/app.py'
  'cd src && rm app.py'
  'npm test -- --ci'
  'make test'
  'echo "rm tests/x_test.py"'
  'git commit -m "rm tests/x_test.py; mv tests/x_test.py y"'
  $'git commit -m "$(cat <<\'EOF\'\nfix: stop calling rm tests/x_test.py\n\nrm tests/x_test.py\nEOF\n)"'
  $'cat > tests/new_test.py <<\'EOF\'\nrm tests/x_test.py\nEOF'
  $'cat > notes.md <<-EOF\n\trm tests/x_test.py\n\tEOF\necho done'
)
for c in "${ALLOWED[@]}"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "allowed (new path, read-only or no test involved): $(short "$c")" 0
done
run_hook guard-bash "$P" "$(j_bash "$P/tests" "rm x_test.py")"
assert_rc "blocked: relative path from a working directory inside tests/" 2
run_hook guard-bash "$P" "$(j_bash "$P/src" "rm ../tests/x_test.py")"
assert_rc "blocked: ../ path from another working directory" 2
run_hook guard-bash "$P" "$(j_bash "$P/src" "rm x_test.py")"
assert_rc "allowed: the same file name relative to a directory without that test" 0
run_hook guard-bash "$P" "$(j_bash "$P/tests" "rm x_*.py")"
assert_rc "blocked: a glob from a working directory inside tests/" 2
run_hook guard-bash "$P" "$(j_bash "$P/src" "rm ../tests/x_*.py")"
assert_rc "blocked: a glob with ../" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm $P/tests/x_*.py")"
assert_rc "blocked: an absolute glob" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/nomatch_*.py")"
assert_rc "allowed: a glob that matches nothing" 0
( cd "$P/bulk" && seq 1 2300 | sed 's/^/f/' | xargs touch )                  # next to the existing bulk/a_test.py
run_hook guard-bash "$P" "$(j_bash "$P" "rm -f bulk/*")"
assert_rc "blocked: a glob over a big directory still finds the test file in it" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm -f bulk/f*")"
assert_rc "allowed, and fast (timing check below): a glob that expands to 2300 non-test files" 0
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_has "block message names the file" "$ERR" "tests/x_test.py"
assert_has "block message names the action" "$ERR" "delete (rm)"
assert_has "block message names the unlock in the work doc" "$ERR" "test_changes: allowed"
assert_has "block message names the active work doc" "$ERR" "work/planned.md"
assert_has "block message names the last-resort env" "$ERR" "SDLC_ALLOW_TEST_EDIT=1"
assert_lacks "block message does not mention removed skills" "$ERR" "/sdlc:verify"
assert_true "shell test lock was logged" "$(log_has "$P" 'test_lock_bash')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")" SDLC_ALLOW_TEST_EDIT=1
assert_rc "unlock: SDLC_ALLOW_TEST_EDIT=1 in the environment the hook runs in" 0
run_hook guard-bash "$P" "$(j_bash "$P" "SDLC_ALLOW_TEST_EDIT=1 rm tests/x_test.py")"
assert_rc "no unlock: the variable written into the command (a tool call cannot set it for the hook)" 2
run_hook guard-bash "$P" "$(j_bash "$P" "export SDLC_ALLOW_TEST_EDIT=1; rm tests/x_test.py")"
assert_rc "no unlock: …nor an export in front of it" 2
mkdir -p "$P/.sdlc/state"; touch "$P/.sdlc/state/allow-test-edit"
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "no unlock: the 0.x file .sdlc/state/allow-test-edit (an agent could create it itself)" 2
rm -f "$P/.sdlc/state/allow-test-edit"
activate "$P" allowed
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "unlock: active doc says test_changes: allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P" "sed -i s/a/b/ tests/x_test.py")"
assert_rc "unlock: in-place edit with the active allowing doc" 0
activate "$P" planned
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "no unlock: active doc says test_changes: none" 2
gitp "$P" checkout -q main
printf 'allowed\n' > "$P/.sdlc/state/active-work"
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "no sdlc work: a stale active-work on main — nothing is locked" 0
assert_true "…and silently" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
rm -f "$P/.sdlc/state/active-work"
run_hook guard-bash "$P" "$(j_bash "$P" "sed -i s/a/b/ tests/x_test.py")"
assert_rc "no sdlc work: main without an active-work file — an in-place edit of an existing test is allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P" "echo x > tests/x_test.py")"
assert_rc "…and so is a redirect onto it" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin main")"
assert_rc "…while the direct-push guard stays on" 2
activate "$P" planned
printf 'planned\n' > "$P/.sdlc/state/active-work"
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "sdlc work again: locked" 2
set_cfg "$P" protect.protect_tests false
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "unlock: protect_tests=false" 0
set_cfg "$P" protect.protect_tests true
set_cfg "$P" protect.test_paths '["qa/"]'
run_hook guard-bash "$P" "$(j_bash "$P" "rm qa/smoke.sh")"
assert_rc "custom test_paths apply to shell commands" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "custom test_paths replace the auto set for shell commands" 0
set_cfg "$P" protect.test_paths '["src/*"]'
run_hook guard-bash "$P" "$(j_bash "$P" "rm src/app.py")"
assert_rc "custom test_paths glob for shell commands: src/* takes src/app.py" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm src/gen/client.txt")"
assert_rc "…and * stops at /: src/gen/client.txt is no test of it (the merge gate reads the glob the same way)" 0
Q=$(fresh_plain)
run_hook guard-bash "$Q" "$(j_bash "$Q" "rm tests/x_test.py")"
assert_rc "uninitialized project: shell test lock is off" 0

# ======================================================================
echo "-- guard-bash: tests this change adds are free, existing ones stay locked"
P=$(fresh_git); activate "$P" planned
mk "$P" tests/session_test.py; mk "$P" tests/fresh/a_test.py; mk "$P" tests/fresh/b_test.py
FREE=(
  'rm tests/session_test.py'
  'mv tests/session_test.py tests/renamed_test.py'
  'mv /tmp/a tests/session_test.py'
  'cp /tmp/a tests/session_test.py'
  'sed -i s/a/b/ tests/session_test.py'
  'echo more >> tests/session_test.py'
  'truncate -s 0 tests/session_test.py'
  'git rm -f tests/session_test.py'
  'rm tests/fresh/*'
  'rm -rf tests/fresh'
  'mv tests/fresh tests/fresh2'
)
for c in "${FREE[@]}"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "free, the test is this change's own: $(short "$c")" 0
done
HELD=(
  'rm tests/x_test.py'
  'mv tests/session_test.py tests/x_test.py'
  'cp tests/session_test.py tests/x_test.py'
  'rm tests/*_test.py'
  'rm -rf tests'
  'mv tests /tmp/oldtests'
  'sed -i s/a/b/ tests/session_test.py tests/x_test.py'
  'git checkout -- tests/x_test.py'
)
for c in "${HELD[@]}"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "locked, an existing test is in the command: $(short "$c")" 2
done
gitp "$P" add tests/session_test.py tests/fresh >/dev/null 2>&1; gitp "$P" commit -qm "wip" >/dev/null 2>&1
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/session_test.py")"
assert_rc "free: still the change's own test after a WIP commit on the session branch" 0
run_hook guard-bash "$P" "$(j_bash "$P" "rm -rf tests/fresh")"
assert_rc "free: a directory of tests the change committed itself" 0
gitp "$P" checkout -q main; gitp "$P" merge -q --ff-only sdlc/planned >/dev/null 2>&1
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/session_test.py")"
assert_rc "free: on the default branch itself no work doc applies, nothing is locked" 0
gitp "$P" checkout -q -B sdlc/planned main >/dev/null 2>&1          # the next piece of sdlc work starts from the merged default branch
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/session_test.py")"
assert_rc "locked: the same test once the change is on the default branch (the next sdlc branch)" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm -rf tests/fresh")"
assert_rc "locked: …and a directory holding such tests" 2
P=$(fresh_git); activate "$P" planned
mkdir -p "$P/tests/few" "$P/tests/lots"
( cd "$P/tests" && seq 1 20 | sed 's/^/few\/t/; s/$/_test.py/' | xargs touch && seq 1 45 | sed 's/^/lots\/t/; s/$/_test.py/' | xargs touch )
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/few/*")"
assert_rc "free: a glob over 20 of the change's own tests" 0
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/lots/*")"
assert_rc "free: a glob over 45 of the change's own tests (the base is listed once, there is no per-file lookup cap to hit)" 0
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py tests/few/t1_test.py")"
assert_rc "locked: an existing test next to an own one" 2
P=$(fresh); gitp "$P" init -q . >/dev/null 2>&1; gitp "$P" symbolic-ref HEAD refs/heads/sdlc/planned
mkdir -p "$P/.sdlc/state"; printf 'planned\n' > "$P/.sdlc/state/active-work"
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "a repository without any commit: no base, existing tests are locked for shell commands too" 2

# ======================================================================
echo "-- guard-bash: design first for shell commands (work doc ready or proposed)"
P=$(fresh_git); activate "$P" ready
DENIED=(
  "sed -i '' 's/a + b/a+b/' src/app.py"
  "sed -i 's/a/b/' src/app.py"
  'sed -i.bak -e s/a/b/ src/app.py'
  'sed --in-place s/a/b/ src/app.py'
  "perl -pi -e 's/a/b/' src/app.py"
  'cp /dev/null src/app.py'
  'cp /tmp/a src/'
  'cp src/app.py src/app.bak'
  'mv /tmp/a src/b.py'
  'mv src/app.py /tmp/'
  'mv src/app.py src/b.py'
  'ln -s /tmp/a src/link'
  'install -m 755 /tmp/a src/a'
  "echo '# x' >> src/app.py"
  'echo x > src/new_module.py'
  'echo x > ./src/app.py'
  'echo x > work/../src/app.py'
  'echo x > notes.txt'
  'echo x > CLAUDE.md'
  'echo x > .claude/settings.json'
  $'cat > src/n.py <<\'EOF\'\nx = 1\nEOF'
  'echo x | tee src/x.py'
  'printf x | tee -a src/app.py'
  'truncate -s 0 src/app.py'
  'dd if=/dev/zero of=src/app.py'
  'rm src/app.py'
  'rm -rf src'
  'rm -f src/*.py'
  'git rm src/app.py'
  'git mv src/app.py src/b.py'
  'git checkout -- src/app.py'
  'git checkout HEAD -- src/app.py'
  'git restore src/app.py'
  'cd src && echo x > app.py'
  'echo hi && echo x >> src/app.py'
  'sudo rm src/app.py'
  'FOO=1 sed -i s/a/b/ src/app.py'
  "bash -c 'echo x > src/app.py'"
  'eval "echo x > src/app.py"'
)
for c in "${DENIED[@]}"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "ready doc: shell write outside work/ and .sdlc/ refused: $(short "$c")" 2
done
run_hook guard-bash "$P" "$(j_bash "$P" "echo x >> src/app.py")"
assert_has "design-first message (shell)" "$ERR" "design first — fill Plan and Proof, then set status planned"
assert_has "…names the doc and its status" "$ERR" "work/ready.md has status 'ready'"
assert_has "…names the path and what the command does to it" "$ERR" "would change 'src/app.py' (output redirection)"
assert_has "…says what stays editable" "$ERR" "everything under work/ and .sdlc/ stay editable"
assert_lacks "…does not mention removed skills" "$ERR" "/sdlc:plan"
assert_true "design-first block (shell) was logged with reason design_first" "$(log_has "$P" '"decision":"deny".*"reason":"design_first".*"tool":"Bash"')" "$(tail -3 "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
ALLOWED_D=(
  'ls -la' 'cat src/app.py' 'grep -rn x src/' 'git status' 'git diff' 'git log --oneline -5' 'git diff HEAD~1 -- src/app.py'
  'sed -n 1,5p src/app.py' "sed 's/a/b/' src/app.py > /tmp/new.py"
  'pytest tests/ > /tmp/out.txt 2>&1' 'git diff > /tmp/d.patch' 'ls src 2>/dev/null' 'ls src > /dev/null' 'echo "x > src/app.py"'
  'echo x >> work/ready.md' "sed -i 's/a/b/' work/ready.md" "sed -i '' 's/a + b/a+b/' work/ready.md" 'cp /tmp/a work/new-item.md'
  'mv work/ready.md work/old.md' 'rm -f work/stale.md' 'echo x | tee .sdlc/state/note' 'cd work && echo x > ready.md' 'echo x > src/../work/ready.md'
  $'cat > work/ready.md <<\'EOF\'\n# Plan\nEOF'
  'echo x > /tmp/outside.txt' 'cp src/app.py /tmp/app.bak' 'sed -i s/a/b/ /tmp/x' 'cd /tmp && echo x > y' 'rm -f /tmp/x'
  'echo $HOME > $OUT' 'echo x > "$TMP/x"' 'rm src/nomatch*'
  'git add work/ready.md' 'git commit -m "plan"' 'git commit -m "rm src/app.py; echo x > src/app.py"' 'git checkout main' 'git checkout -b sdlc/other' 'git switch -c x'
  'git restore --staged src/app.py'
)
for c in "${ALLOWED_D[@]}"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "ready doc: allowed (reads, work/ and .sdlc/, outside the project, no file named): $(short "$c")" 0
done
run_hook guard-bash "$P" "$(j_bash "$P/src" "echo x > app.py")"
assert_rc "ready doc: a relative target from a working directory inside the project is resolved" 2
run_hook guard-bash "$P" "$(j_bash "$P/work" "echo x > ready.md")"
assert_rc "ready doc: …and one inside work/ is allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P/work" "echo x > ../src/app.py")"
assert_rc "ready doc: ../ out of work/ into src/ is refused" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "ready doc: an existing test is refused by the test lock first" 2
assert_has "…with the test lock's message" "$ERR" "existing test file"
run_hook guard-bash "$P" "$(j_bash "$P" "echo x > tests/new_test.py")"
assert_rc "ready doc: even a new test file waits for the design" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm src/app.py")" SDLC_ALLOW_TEST_EDIT=1
assert_rc "ready doc: the test-edit unlock does not lift design first" 2
activate "$P" proposed
run_hook guard-bash "$P" "$(j_bash "$P" "echo x >> src/app.py")"
assert_rc "proposed doc: shell write refused" 2
assert_has "proposed doc: message carries the status" "$ERR" "status 'proposed'"
for slug in planned built; do
  activate "$P" "$slug"
  run_hook guard-bash "$P" "$(j_bash "$P" "echo x >> src/app.py")"
  assert_rc "$slug doc: design first is over, the write goes through" 0
  run_hook guard-bash "$P" "$(j_bash "$P" "sed -i s/a/b/ src/app.py")"
  assert_rc "$slug doc: in-place edit goes through" 0
done
activate "$P" ready; gitp "$P" checkout -q main
run_hook guard-bash "$P" "$(j_bash "$P" "echo x >> src/app.py")"
assert_rc "ready doc named by a stale active-work on main: ignored" 0
activate "$P" ghost
run_hook guard-bash "$P" "$(j_bash "$P" "echo x >> src/app.py")"
assert_rc "active slug without a doc: nothing to wait for" 0
Q=$(fresh_git)
for c in 'echo x >> CLAUDE.md' 'echo x > REVIEW.md' 'echo x > .claude/settings.json' 'echo x > work/new-item.md' 'echo x > .sdlc/config.json' 'echo x >> src/app.py'; do
  run_hook guard-bash "$Q" "$(j_bash "$Q" "$c")"
  assert_rc "no active doc: shell write not denied for lacking a work doc: $(short "$c")" 0
done
Q=$(fresh)
mkdir -p "$Q/.sdlc/state"; printf 'ready\n' > "$Q/.sdlc/state/active-work"
run_hook guard-bash "$Q" "$(j_bash "$Q" "echo x >> src/app.py")"
assert_rc "no git repository: no branch, so no active doc" 0
Q=$(fresh_plain)
run_hook guard-bash "$Q" "$(j_bash "$Q" "echo x >> src/app.py")"
assert_rc "uninitialized project: design first is off for shell commands" 0
Q=$(fresh_git)
set_cfg "$Q" paths.work '"tasks"'; mkdir -p "$Q/tasks"; cp "$Q/work/ready.md" "$Q/tasks/ready.md"; activate "$Q" ready
run_hook guard-bash "$Q" "$(j_bash "$Q" "echo x >> tasks/ready.md")"
assert_rc "custom paths.work: that dir is writable" 0
run_hook guard-bash "$Q" "$(j_bash "$Q" "echo x > work/next.md")"
assert_rc "custom paths.work: the default dir is no longer special" 2
assert_has "custom paths.work: the message names the configured dir" "$ERR" "everything under tasks/ and .sdlc/ stay editable"
Q=$(fresh_git); activate "$Q" ready
mkdir -p "$Q/many"; ( cd "$Q/many" && seq 1 2300 | sed 's/^/f/' | xargs touch )
run_hook guard-bash "$Q" "$(j_bash "$Q" "rm -f many/f*")"
assert_rc "ready doc: a glob over a big directory is refused (and fast: see the timing check)" 2
run_hook guard-bash "$Q" "$(j_bash "$Q" "rm -f many/nomatch*")"
assert_rc "ready doc: a glob that matches nothing changes nothing" 0

# ======================================================================
echo "-- guard-bash: commit secret scan (temp git repo)"
P=$(fresh_git)
printf 'token = "%s"\n' "$GH_TOKEN" > "$P/src/leak.py"; gitp "$P" add src/leak.py
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'add client'")"
assert_rc "git commit with staged secret blocked" 2
assert_has "commit block names a pattern" "$ERR" "pattern#"
assert_lacks "commit block never echoes the secret" "$ERR" "${GH_TOKEN:0:14}"
for c in "GIT_AUTHOR_NAME=x git commit -m 'add client'" "GIT_AUTHOR_NAME=x GIT_COMMITTER_NAME=x git commit -m 'add client'" \
         "env GIT_COMMITTER_EMAIL=x git commit -m 'add client'" "git -c user.name=x commit -m 'add client'"; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "a commit that names its author is scanned like any other: $(short "$c")" 2         # what the deploy gate lets through, the scan still reads
done
{ gitp "$P" restore --staged src/leak.py 2>/dev/null || gitp "$P" reset -q src/leak.py; }; rm -f "$P/src/leak.py"
printf 'x = 1\n' >> "$P/src/app.py"; gitp "$P" add src/app.py
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'clean change'")"
assert_rc "clean staged commit allowed" 0
printf 'caf\xb0\xa1 legacy encoding\naws = "%s"\n' "$AWS_KEY" > "$P/src/legacy.txt"; gitp "$P" add src/legacy.txt
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'legacy encoded file'")"
assert_rc "git commit: a secret behind a line in a legacy encoding is found" 2
{ gitp "$P" restore --staged src/legacy.txt 2>/dev/null || gitp "$P" reset -q src/legacy.txt; }; rm -f "$P/src/legacy.txt"
printf 'aws = "%s"\n' "$AWS_KEY" >> "$P/src/app.py"     # tracked, NOT staged
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'only staged'")"
assert_rc "unstaged secret does not block a plain commit" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -am 'commit all'")"
assert_rc "git commit -am scans unstaged tracked changes too" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git commit --all -m 'commit all'")"
assert_rc "git commit --all scans unstaged tracked changes too" 2
gitp "$P" checkout -q -- src/app.py
set_cfg "$P" secrets.scan_commits false
printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/leak2.py"; gitp "$P" add src/leak2.py
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m x")"
assert_rc "scan_commits=false disables commit scan" 0

# ======================================================================
echo "-- guard-bash: the commit scan covers what the commit could take (git add before the commit, -a)"
P=$(fresh_git)
printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/settings.py"                  # untracked: nothing is staged when the hook runs
run_hook guard-bash "$P" "$(j_bash "$P" "git add src/settings.py && git commit -m x")"
assert_rc "git add <file> && git commit: the untracked file with a secret is caught" 2
assert_has "…the block names a pattern" "$ERR" "pattern#"
assert_lacks "…and never echoes the secret" "$ERR" "${AWS_KEY:0:12}"
assert_has "…and says the scan covered what git add could take" "$ERR" "stages before it commits"
assert_true "…logged with scope all+untracked, without the secret" "$(log_has "$P" '"reason":"commit_secret".*all+untracked')" "$(tail -2 "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
assert_true "…the audit log does not hold the secret either" "$(grep -q "${AWS_KEY:0:12}" "$P/.sdlc/logs/events.jsonl" 2>/dev/null && echo false || echo true)" "the secret is in events.jsonl"
for c in 'git add -A && git commit -m x' 'git add . && git commit -m x' 'git add --all && git commit -m x' 'git add src && git commit -m x' 'git add -A; git commit -m x' \
         'git add . || git commit -m x' 'git add . && git commit -am x' 'git add . && git commit --all -m x' "git -C $P add . && git -C $P commit -m x" 'git -c core.quotepath=off add . && git -c core.quotepath=off commit -m x' \
         'cd src && git add . && git commit -m x' 'echo go && git add . && echo staged && git commit -m x' "bash -c 'git add . && git commit -m x'" $'git add .\ngit commit -m x' \
         $'git add -A && git commit -m "$(cat <<\'EOF2\'\nfix: the thing\n\nBody text.\nEOF2\n)"'; do
  run_hook guard-bash "$P" "$(j_bash "$P" "$c")"
  assert_rc "add before commit, untracked secret caught: $(short "$c")" 2
done
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m x")"
assert_rc "a plain git commit scans the staged diff only: the untracked secret is not in it" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -am x")"
assert_rc "git commit -a takes tracked changes only: the untracked secret is not in it" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m x && git add .")"
assert_rc "git add after the commit does not widen the scan" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git add . && git status")"
assert_rc "git add without a commit is not scanned" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git -C $P log --oneline")"
assert_rc "other git commands are not scanned" 0
rm -f "$P/src/settings.py"
printf 'x = 1\n' > "$P/src/clean_module.py"; printf 'plain notes\n' > "$P/notes.txt"
run_hook guard-bash "$P" "$(j_bash "$P" "git add -A && git commit -m x")"
assert_rc "clean untracked files: add + commit passes" 0
assert_true "…and the passing run is silent" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
printf 'local.env\n*.secret\n' > "$P/.gitignore"; gitp "$P" add .gitignore >/dev/null 2>&1; gitp "$P" commit -qm "ignore" >/dev/null 2>&1
printf 'aws = "%s"\n' "$AWS_KEY" > "$P/local.env"; printf 'aws = "%s"\n' "$AWS_KEY" > "$P/prod.secret"
run_hook guard-bash "$P" "$(j_bash "$P" "git add -A && git commit -m x")"
assert_rc "an untracked file that .gitignore covers is not scanned" 0
printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/settings.py"
run_hook guard-bash "$P" "$(j_bash "$P" "git add -A && git commit -m x")"
assert_rc "…while the not-ignored file next to it still is" 2
rm -f "$P/src/settings.py" "$P/local.env" "$P/prod.secret"
printf 'aws = "%s"\n' "$AWS_KEY" >> "$P/src/app.py"                      # tracked, unstaged
run_hook guard-bash "$P" "$(j_bash "$P" "git add notes.txt && git commit -m x")"
assert_rc "add of another file: an unstaged secret in a tracked file is in the scan (git diff HEAD)" 2
gitp "$P" checkout -q -- src/app.py
head -c 300000 /dev/zero | tr '\0' 'a' > "$P/big.txt"; printf '\naws = "%s"\n' "$AWS_KEY" >> "$P/big.txt"
printf '\001\002\000 aws = "%s"\000' "$AWS_KEY" > "$P/blob.bin"
ln -s "$P/src/app.py" "$P/link.txt"
run_hook guard-bash "$P" "$(j_bash "$P" "git add -A && git commit -m x")"
assert_rc "limits: a file over 256 KB, a binary file and a symlink are not read" 0
rm -f "$P/big.txt" "$P/blob.bin" "$P/link.txt"
set_cfg "$P" secrets.scan_commits false
printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/settings.py"
run_hook guard-bash "$P" "$(j_bash "$P" "git add -A && git commit -m x")"
assert_rc "scan_commits=false switches the widened scan off too" 0
P=$(fresh_git)
printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/settings.py"
run_hook guard-bash "$P" "$(j_bash "$P/src" "git add . && git commit -m x")"
assert_rc "a working directory inside the project: the project is scanned" 2
# git -C <dir> add|commit: the repository named there is scanned as well as the project's own
P=$(fresh_git); mkdir "$P/sub"; printf 'x\n' > "$P/sub/a.txt"; git_init "$P/sub"; printf 'aws = "%s"\n' "$AWS_KEY" > "$P/sub/settings.py"   # a repository of its own below the project
run_hook guard-bash "$P" "$(j_bash "$P" "git add . && git commit -m x")"
assert_rc "git -C: without it the nested repository is no part of the project's commit, nothing is found" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git -C sub add . && git -C sub commit -m x")"
assert_rc "git -C <relative dir>: resolved against the working directory, the nested repository's secret is found" 2
assert_has "git -C: the block names the repository the secret is in" "$ERR" "in $P/sub"
assert_lacks "git -C: …and never prints the secret" "$ERR" "$AWS_KEY"
O=$(fresh_git); printf 'aws = "%s"\n' "$AWS_KEY" > "$O/src/settings.py"; Q=$(fresh_git)
run_hook guard-bash "$Q" "$(j_bash "$Q" "git -C $O add . && git -C $O commit -m x")"
assert_rc "git -C <another repository>: that repository is scanned too, though the project is clean" 2
run_hook guard-bash "$Q" "$(j_bash "$Q" "git -C $O commit -m x")"
assert_rc "git -C <another repository> commit with nothing staged there: nothing to scan" 0
git -C "$O" add src/settings.py >/dev/null 2>&1
run_hook guard-bash "$Q" "$(j_bash "$Q" "git -C $O commit -m x")"
assert_rc "git -C <another repository> commit: the secret staged there is found, not the project's index" 2
run_hook guard-bash "$O" "$(j_bash "$O" "git -C $Q add . && git add . && git commit -m x")"
assert_rc "a command that also commits the project's own: the project is scanned as well" 2
run_hook guard-bash "$O" "$(j_bash "$O" "git -C $O add . && git -C $O commit -m x")"
assert_rc "git -C <the project itself>: scanned once, secret found" 2
P=$(fresh_git); printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/settings.py"
run_hook guard-bash "$P" "$(j_bash "$P" "git -C /nonexistent/dir add . && git -C /nonexistent/dir commit -m x")"
assert_rc "git -C <not a repository>: left to the scan of the project, which holds the secret" 2
P=$(fresh_git); mkdir "$P/sp ace"; printf 'x\n' > "$P/sp ace/a.txt"; git_init "$P/sp ace"; printf 'aws = "%s"\n' "$AWS_KEY" > "$P/sp ace/settings.py"
run_hook guard-bash "$P" "$(j_bash "$P" 'git -C "sp ace" add . && git -C "sp ace" commit -m x')"
assert_rc "git -C \"<dir with a space>\": the quoted word is one argument, that repository is scanned" 2
run_hook guard-bash "$P" "$(j_bash "$P" "git -C 'sp ace' add . && git -C 'sp ace' commit -m x")"
assert_rc "git -C '<dir with a space>': single quotes too" 2
run_hook guard-bash "$P" "$(j_bash "$P" 'git -C sp\ ace add . && git -C sp\ ace commit -m x')"
assert_rc "git -C <dir with a backslash-escaped space>: same" 2
P=$(fresh_git); printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/settings.py"; gitp "$P" add src/settings.py >/dev/null 2>&1
run_hook guard-bash "$P" "$(j_bash "$P" 'git -c user.name="A B" commit -m x')"
assert_rc "git -c k=\"quoted value\" commit: the option's quoted value does not hide the commit" 2
Q=$(fresh_plain); git_init "$Q"; printf 'aws = "%s"\n' "$AWS_KEY" > "$Q/settings.py"
run_hook guard-bash "$Q" "$(j_bash "$Q" "git add -A && git commit -m x")"
assert_rc "uninitialized project: the commit scan is off" 0

# ======================================================================
echo "-- secret-scan.sh --staged --untracked"
SS="$PLUGIN/scripts/secret-scan.sh"
R=$(fresh_git)
printf 'aws = "%s"\n' "$AWS_KEY" > "$R/src/u.py"
out=$(bash "$SS" --staged "$R" 2>&1); rc=$?
assert_eq "without --untracked an untracked file is not scanned" "$rc" 0
out=$(bash "$SS" --staged "$R" --all 2>&1); rc=$?
assert_eq "--all does not read untracked files either (commit -a does not take them)" "$rc" 0
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: the secret in an untracked file exits 1" "$rc" 1
assert_has "--untracked: the hit names a pattern" "$out" "pattern#"
assert_lacks "--untracked: the secret is never printed" "$out" "${AWS_KEY:0:12}"
assert_has "--untracked: the hit names the file and the line (1.2)" "$out" "src/u.py:1"
out=$(bash "$SS" --staged "$R" --all --untracked 2>&1); rc=$?
assert_eq "--all --untracked together" "$rc" 1
rm -f "$R/src/u.py"
printf 'aws = "%s"\n' "$AWS_KEY" > "$R/my file.py"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a file name with a space" "$rc" 1
rm -f "$R/my file.py"
printf 'aws = "%s"\n' "$AWS_KEY" > "$R/日本語.py"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a non-ASCII file name" "$rc" 1
rm -f "$R/日本語.py"
mkdir -p "$R/deep/er"; printf 'aws = "%s"\n' "$AWS_KEY" > "$R/deep/er/s.py"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a file in a nested untracked directory" "$rc" 1
out=$(bash "$SS" --staged "$R/deep" --untracked 2>&1); rc=$?
assert_eq "--untracked: a root below the repository top scans its own untracked files" "$rc" 1
rm -rf "$R/deep"
printf 'caf\xb0\xa1 legacy encoding\naws = "%s"\n' "$AWS_KEY" > "$R/cp949.txt"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a text file in a legacy encoding is still read (bytes, not characters)" "$rc" 1
printf 'caf\xb0\xa1 legacy encoding\nnothing secret here\n' > "$R/cp949.txt"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: …and a clean one in a legacy encoding is not a false hit" "$rc" 0
rm -f "$R/cp949.txt"
# the staged diff is read as bytes too: awk and tr (macOS, UTF-8 locale) stop dead on a line in a legacy encoding, and what came after it
# was never scanned
printf 'caf\xb0\xa1 legacy encoding\naws = "%s"\n' "$AWS_KEY" > "$R/cp949.txt"; gitp "$R" add cp949.txt
out=$(bash "$SS" --staged "$R" 2>&1); rc=$?
assert_eq "--staged: a secret behind a line in a legacy encoding is found" "$rc" 1
printf '\xb0\xa1 legacy first\naws = "%s"\n' "$AWS_KEY" > "$R/cp949.txt"; gitp "$R" add cp949.txt
out=$(bash "$SS" --staged "$R" 2>&1); rc=$?
assert_eq "--staged: …also when the file starts with one" "$rc" 1
printf 'caf\xb0\xa1 legacy encoding\nnothing secret here\n' > "$R/cp949.txt"; gitp "$R" add cp949.txt
out=$(bash "$SS" --staged "$R" 2>&1); rc=$?
assert_eq "--staged: …and a clean staged file in a legacy encoding is not a false hit" "$rc" 0
gitp "$R" reset -q cp949.txt; rm -f "$R/cp949.txt"
printf 'aws = "%s"\n' "$AWS_KEY" > "$R/ignored.txt"; printf 'ignored.txt\n' >> "$R/.git/info/exclude"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a file covered by .git/info/exclude is not scanned" "$rc" 0
rm -f "$R/ignored.txt"
head -c 300000 /dev/zero | tr '\0' 'a' > "$R/big.txt"; printf '\naws = "%s"\n' "$AWS_KEY" >> "$R/big.txt"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a file over 256 KB is skipped (documented limit)" "$rc" 0
rm -f "$R/big.txt"
printf 'x\000y aws = "%s"\n' "$AWS_KEY" > "$R/blob.bin"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a binary file is skipped" "$rc" 0
rm -f "$R/blob.bin"
printf 'aws = "%s"\n' "$AWS_KEY" > "$R/real.txt"; ln -s "$R/real.txt" "$R/link.txt"; mv "$R/real.txt" "$R/hidden.bin.d"; printf 'x\000' >> "$R/hidden.bin.d"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a symlink is not followed (its target here is binary and skipped on its own)" "$rc" 0
rm -f "$R/link.txt" "$R/hidden.bin.d"
for i in $(seq 1 205); do printf 'line %s\n' "$i" > "$R/f$i.txt"; done
printf 'aws = "%s"\n' "$AWS_KEY" > "$R/zzz.txt"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: only the first 200 files git lists are read (documented limit)" "$rc" 0
rm -f "$R"/f*.txt "$R/zzz.txt"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: a tree with no untracked text exits 0" "$rc" 0
assert_eq "--untracked: …and prints nothing" "$out" ""
Q=$(fresh_plain)
out=$(bash "$SS" --staged "$Q" --untracked 2>&1); rc=$?
assert_eq "--untracked outside a git work tree exits 0" "$rc" 0
R=$(fresh); git_init "$R"; gitp "$R" rm -q --cached -r . >/dev/null 2>&1; gitp "$R" commit -qm "empty" >/dev/null 2>&1
printf 'aws = "%s"\n' "$AWS_KEY" > "$R/src/app.py"
out=$(bash "$SS" --staged "$R" --untracked 2>&1); rc=$?
assert_eq "--untracked: every file is untracked after the tree was emptied" "$rc" 1
R=$(fresh); gitp "$R" init -q . >/dev/null 2>&1; printf 'aws = "%s"\n' "$AWS_KEY" > "$R/src/settings.py"
out=$(bash "$SS" --staged "$R" --all --untracked 2>&1); rc=$?
assert_eq "--all --untracked in a repository without any commit" "$rc" 1

# ======================================================================
echo "-- version-1 config (a 0.x repository that has not run init yet)"
P=$(fresh_v1); git_init "$P"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/gen/client.txt" "x")"
assert_rc "v1: frozen path blocked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "x")"
assert_rc "v1: no sdlc work (no active work doc): an existing test is not locked" 0
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "v1: …nor for a shell command" 0
mkdir -p "$P/work"; printf -- '---\nslug: demo\nstatus: planned\ntest_changes: none\n---\n' > "$P/work/demo.md"
activate "$P" demo                                           # sdlc work in a 0.x repository: its config reads like a sparse 1.0 one
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "x")"
assert_rc "v1: during sdlc work an existing test is locked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/spec/demo.md" "x")"
assert_rc "v1: paths.spec is no exemption — spec/ counts as tests, as it does for the merge gate" 2
run_hook guard-edit "$P" "$(j_write "$P" "$P/tests/new_test.py" "def test_new(): pass")"
assert_rc "v1: a new test file is allowed" 0
mkdir -p "$P/plan"; printf -- '---\nstatus: approved\ntest_changes: allowed\n---\n' > "$P/plan/allowed.md"; printf 'plan/allowed.md' > "$P/.sdlc/state/active-plan"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "x")"
assert_rc "v1: the old active-plan no longer unlocks existing tests" 2
set_cfg "$P" plan.require_for_edits true; printf 'plan/draft.md' > "$P/.sdlc/state/active-plan"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "v1: plan.require_for_edits is ignored (no plan gate)" 0
rm -f "$P/.sdlc/state/active-plan"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")"
assert_rc "v1: no active plan, edit allowed" 0
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "v1: shell test lock applies" 2
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")"
assert_rc "v1: production gate blocks (environments.production.patterns)" 2
assert_has "v1: message names RELEASE_APPROVAL" "$ERR" "RELEASE_APPROVAL"
run_hook guard-bash "$P" "$(j_bash "$P" "echo terraform apply -var env=prod")"
assert_rc "v1: a text tool printing the pattern words is not a deploy" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'terraform apply -var env=prod'")"
assert_rc "v1: …nor is git without push" 0
run_hook guard-bash "$P" "$(j_bash "$P" "bash -c 'cd x && terraform apply -var env=prod'")"
assert_rc "v1: …while a quoted argument of a command still counts" 2
run_hook guard-bash "$P" "$(j_bash "$P" $'bash <<EOF\nterraform apply -var env=prod\nEOF')"
assert_rc "v1: …and so does the body of a shell heredoc" 2
run_hook guard-bash "$P" "$(j_bash "$P" $'cat <<EOF\nterraform apply -var env=prod\nEOF')"
assert_rc "v1: …but not the body of a data heredoc" 0
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")" RELEASE_APPROVAL=REL-1
assert_rc "v1: RELEASE_APPROVAL unlocks" 0
run_hook guard-bash "$P" "$(j_bash "$P" "helm upgrade api ./chart -n staging")"
assert_rc "v1: staging patterns are ignored" 0
assert_true "v1: staging deploy is silent" "$([ -z "$OUT$ERR" ] && echo true || echo false)" "out=$OUT err=$ERR"
set_cfg "$P" gate.mode '"ask"'
run_hook guard-bash "$P" "$(j_bash "$P" "kubectl apply -f x -n production")"
assert_rc "v1: gate.mode is ignored" 2
set_cfg "$P" environments.production.approval_env '"CHANGE_TICKET"'
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")" CHANGE_TICKET=CHG-1
assert_rc "v1: environments.production.approval_env honored" 0
run_hook guard-bash "$P" "$(j_bash "$P" "terraform apply -var env=prod")" RELEASE_APPROVAL=x
assert_rc "v1: default approval env ignored when the v1 one is custom" 2
set_cfg "$P" environments.production.patterns '["zzz-ship"]'
run_hook guard-bash "$P" "$(j_bash "$P" "./zzz-ship.sh")"
assert_rc "v1: environments.production.patterns honored" 2
run_hook guard-bash "$P" "$(j_bash "$P" "kubectl apply -f x -n production")"
assert_rc "v1: custom production patterns replace the default list" 0
set_cfg "$P" protect.deploy_patterns '["yyy-ship"]'
run_hook guard-bash "$P" "$(j_bash "$P" "./yyy-ship.sh")"
assert_rc "v1+v2 keys: protect.deploy_patterns wins" 2
run_hook guard-bash "$P" "$(j_bash "$P" "./zzz-ship.sh")"
assert_rc "v1+v2 keys: the v1 list is no longer read" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin main")"
assert_rc "v1: direct push to main blocked" 2
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_rc "v1: session-start exits 0" 0
assert_has "v1: session-start shows the 1.0 entry points" "$OUT" "/sdlc:go"
assert_lacks "v1: no plan line" "$OUT" "Active plan"
assert_lacks "v1: no pending-intents line" "$OUT" "Pending intents"
set_cfg "$P" plan.enforce_sync true; touch -t 202601010000 "$P/plan/demo.md"
run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")"
assert_rc "v1: post-edit exits 0" 0
assert_true "v1: post-edit still records the edit" "$([ "$(state_of "$P" edited)" = true ] && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json" 2>/dev/null)"
run_hook stop-verify "$P" "$(j_stop "$P" false)"
assert_true "v1: plan.enforce_sync is ignored (no departures question)" "$([ -z "$OUT" ] && [ "$RC" = 0 ] && echo true || echo false)" "rc=$RC out=$OUT"

# ======================================================================
echo "-- uninitialized project"
P=$(fresh_plain)
run_hook guard-bash "$P" "$(j_bash "$P" "kubectl rollout restart deploy/api -n prod")"
assert_rc "uninitialized: production gate still blocks (built-in patterns)" 2
run_hook guard-bash "$P" "$(j_bash "$P" "kubectl rollout restart deploy/api -n prod")" RELEASE_APPROVAL=REL-1
assert_rc "uninitialized: RELEASE_APPROVAL unlocks" 0
run_hook guard-bash "$P" "$(j_bash "$P" "helm upgrade api ./chart -n staging")"
assert_rc "uninitialized: staging is not gated" 0
run_hook guard-bash "$P" "$(j_bash "$P" "echo kubectl rollout restart deploy/api -n prod")"
assert_rc "uninitialized: a text tool printing the words is not a deploy" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'release to production'")"
assert_rc "uninitialized: git without push is not checked" 0
run_hook guard-bash "$P" "$(j_bash "$P" "git push origin release/production")"
assert_rc "uninitialized: git push is" 2
run_hook guard-bash "$P" "$(j_bash "$P" $'bash <<EOF\n./deploy prod\nEOF')"
assert_rc "uninitialized: a shell heredoc body is read as commands" 2
run_hook guard-bash "$P" "$(j_bash "$P" $'cat <<EOF\n./deploy prod\nEOF')"
assert_rc "uninitialized: a data heredoc body is not" 0
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
run_hook post-edit "$P" "$(j_postedit "$P" "$P/work/planned.md")"
assert_true "editing a work doc is an ordinary edit (no plan-sync state)" "$([ -z "$(state_of "$P" plan_sync_checked)" ] && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json")"
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
assert_true "events.jsonl has verify.run pass" "$(log_has "$P" '"event":"verify.run".*"decision":"pass"')" "$(cat "$P/.sdlc/logs/events.jsonl")"
run_hook post-bash "$P" "$(j_postbashfail "$P" "npm test" "FAIL src/app.test.js")"
assert_true "a failed verify command (PostToolUseFailure) → last_verify_ok=false" "$([ "$(state_of "$P" last_verify_ok)" = false ] && echo true || echo false)" "$(state_of "$P" last_verify_ok)"
run_hook post-bash "$P" "$(j_postbash "$P" "npm test | tail -20" "= 1 failed, 20 passed =" "")"
assert_true "'1 failed' in the output of a piped run → false (the pipe hides the exit code)" "$([ "$(state_of "$P" last_verify_ok)" = false ] && echo true || echo false)" "$(state_of "$P" last_verify_ok)"
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
assert_lacks "block reason does not mention removed skills" "$OUT" "/sdlc:verify"
run_hook stop-verify "$P" "$(j_stop "$P" true)"
assert_true "stop_hook_active=true → always allowed" "$([ -z "$OUT" ] && [ "$RC" = 0 ] && echo true || echo false)" "rc=$RC out=$OUT"
run_hook post-bash "$P" "$(j_postbashfail "$P" "npm test" "FAIL")"
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
assert_true "required but no verify command configured → allowed (logged skip)" "$([ -z "$OUT" ] && [ "$(log_has "$Q" '"event":"hook.stop".*"decision":"skip"')" = true ] && echo true || echo false)" "out=$OUT log=$(cat "$Q/.sdlc/logs/events.jsonl")"

# ======================================================================
echo "-- session-start / session-end"
P=$(fresh)
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_rc "session-start exits 0" 0
assert_has "context: plugin version line" "$OUT" "sdlc plugin v"
assert_has "context: names /sdlc:go" "$OUT" "/sdlc:go"
assert_has "context: names /sdlc:run" "$OUT" "/sdlc:run"
assert_has "context: names /sdlc:status" "$OUT" "/sdlc:status"
assert_has "context: no active work yet" "$OUT" "Active work: none"
assert_has "context: verify commands" "$OUT" "Verify commands:"
assert_has "context: guards — frozen paths" "$OUT" "frozen paths: 2 entries"
assert_has "context: guards — no active work: the test lock is for sdlc work only" "$OUT" "existing test files: locked during sdlc work only"
assert_lacks "context: …so it does not claim a lock that is not on" "$OUT" "existing test files locked,"
assert_has "context: guards — production gate with its approval variable" "$OUT" "production deploys gated (RELEASE_APPROVAL)"
assert_has "context: rule — one work doc per change" "$OUT" "One work doc per change"
assert_has "context: rule — fix the code, not the test" "$OUT" "fix the code, not the test"
for gone in "/sdlc:plan" "/sdlc:spec" "/sdlc:intent" "/sdlc:verify" "/sdlc:review" "/sdlc:lesson" "/sdlc:release" "/sdlc:monitor" "/sdlc:triage" "Pending intents" "Active plan" "plan.md"; do
  assert_lacks "context: no mention of '$gone'" "$OUT" "$gone"
done
assert_true "context block is ≤ 12 lines" "$([ "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" -le 12 ] && echo true || echo false)" "$(printf '%s\n' "$OUT" | wc -l)"
assert_true "session.start logged" "$(log_has "$P" '"event":"session.start"')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
assert_true "session state file has started" "$(state_of "$P" started | grep -Eq '^[0-9]{4}-' && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json" 2>/dev/null)"
set_cfg "$P" protect.deploy_patterns '[]'
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_lacks "context: no production-gate note when the gate is off" "$OUT" "production deploys gated"
P=$(fresh_git); activate "$P" planned
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: active work doc with slug and status" "$OUT" "Active work: work/planned.md (slug: planned, status: planned)"
assert_lacks "context: a planned doc has no design-first note" "$OUT" "design first: edits outside"
assert_has "context: sdlc work → existing tests locked, new ones free" "$OUT" "existing test files locked, new ones are free"
run_hook session-start "$P" "$(j_session "$P" SessionStart)" SDLC_ALLOW_TEST_EDIT=1
assert_has "context: SDLC_ALLOW_TEST_EDIT=1 in the environment → tests unlocked" "$OUT" "existing test files: UNLOCKED"
activate "$P" ready
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: ready doc → design-first note" "$OUT" "design first: edits outside work/"
activate "$P" allowed
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: allowing doc → tests unlocked" "$OUT" "existing test files: UNLOCKED"
activate "$P" ghost
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: active slug without a doc is reported" "$OUT" "file missing"
assert_has "context: …and its tests stay locked" "$OUT" "existing test files locked, new ones are free"
activate "$P" planned; gitp "$P" checkout -q main
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: stale active-work on another branch → none" "$OUT" "Active work: none"
assert_has "context: …and no test lock" "$OUT" "existing test files: locked during sdlc work only"
printf '%s\n' "$$" > "$P/.sdlc/state/loop.pid"
printf '{"state":"running","item":"demo","phase":"build","pid":%s}\n' "$$" > "$P/.sdlc/state/loop-status.json"
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: live loop → Loop line with state, item, phase and pid" "$OUT" "Loop: running — demo (build), pid $$"
assert_true "context with the loop line is still ≤ 12 lines" "$([ "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" -le 12 ] && echo true || echo false)" "$(printf '%s\n' "$OUT" | wc -l)"
( exit 0 ) & dead=$!; wait "$dead" 2>/dev/null
printf '%s\n' "$dead" > "$P/.sdlc/state/loop.pid"
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_lacks "context: dead loop pid → no Loop line" "$OUT" "Loop:"
printf 'not a pid\n' > "$P/.sdlc/state/loop.pid"
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_rc "context: garbage in loop.pid does not break the hook" 0
assert_lacks "context: garbage loop.pid → no Loop line" "$OUT" "Loop:"
set_cfg "$P" hooks.session_context false
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_true "session_context=false → no context output (false honored)" "$([ -z "$OUT" ] && [ "$RC" = 0 ] && echo true || echo false)" "$OUT"
run_hook session-end "$P" "$(j_session "$P" SessionEnd)"
assert_rc "session-end exits 0" 0
assert_true "session.end logged" "$(log_has "$P" '"event":"session.end"')" "$(cat "$P/.sdlc/logs/events.jsonl")"
set_cfg "$P" hooks.log_events false
before=$(wc -l < "$P/.sdlc/logs/events.jsonl")
run_hook session-end "$P" "$(j_session "$P" SessionEnd)"
after=$(wc -l < "$P/.sdlc/logs/events.jsonl")
assert_true "log_events=false suppresses logging" "$([ "$before" = "$after" ] && echo true || echo false)" "before=$before after=$after"

# ======================================================================
echo "-- robustness: empty stdin never blocks, garbage stdin blocks only in the two guards (they fail closed)"
P=$(fresh)
for h in session-start session-end guard-edit guard-bash post-edit post-bash stop-verify; do
  run_hook "$h" "$P" ""
  assert_rc "empty stdin: $h exits 0" 0
done
for h in session-start session-end post-edit post-bash stop-verify; do         # the guards block on garbage (1.2: they fail closed)
  run_hook "$h" "$P" "not json at all {"
  assert_rc "garbage stdin: $h exits 0" 0
done
run_hook guard-bash "$P" "$(j_bash "$P" "rm \"unterminated tests/x_test.py")"
assert_rc "an unbalanced quote does not crash the guard (the shell would reject that command anyway)" 0
run_hook guard-bash "$P" "$(j_bash "$P" "echo 'unterminated > tests/new_test.py")"
assert_rc "an unbalanced single quote on a new path stays allowed" 0

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
    P=$(fresh_git)
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/gen/client.txt" "edited")" PATH="$NOJQ"
    assert_rc "no-jq: frozen path blocked" 2
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "edited")" PATH="$NOJQ"
    assert_rc "no-jq: no sdlc work: the test is not locked" 0
    activate "$P" planned
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "edited")" PATH="$NOJQ"
    assert_rc "no-jq: test lock blocked during sdlc work" 2
    run_hook guard-edit "$P" "$(j_write "$P" "$P/tests/new_test.py" "def test_new(): pass")" PATH="$NOJQ"
    assert_rc "no-jq: new test file allowed" 0
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "aws_key = '$AWS_KEY'")" PATH="$NOJQ"
    assert_rc "no-jq: secret blocked" 2
    activate "$P" allowed
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "edited")" PATH="$NOJQ"
    assert_rc "no-jq: active doc test_changes: allowed unlocks" 0
    activate "$P" ready
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")" PATH="$NOJQ"
    assert_rc "no-jq: design first blocks while the doc is ready" 2
    run_hook guard-bash "$P" "$(j_bash "$P" "sed -i s/a/b/ src/app.py")" PATH="$NOJQ"
    assert_rc "no-jq: design first blocks a shell write while the doc is ready" 2
    run_hook guard-bash "$P" "$(j_bash "$P" "echo x >> work/ready.md")" PATH="$NOJQ"
    assert_rc "no-jq: …and lets the work doc through" 0
    activate "$P" planned
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")" PATH="$NOJQ"
    assert_rc "no-jq: planned doc lets the edit through" 0
    run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")" PATH="$NOJQ"
    assert_rc "no-jq: shell test lock blocks rm" 2
    run_hook guard-bash "$P" "$(j_bash "$P" "echo x > tests/new_test.py")" PATH="$NOJQ"
    assert_rc "no-jq: shell redirect to a new test path allowed" 0
    printf 'aws = "%s"\n' "$AWS_KEY" > "$P/src/settings.py"
    run_hook guard-bash "$P" "$(j_bash "$P" "git add src/settings.py && git commit -m x")" PATH="$NOJQ"
    assert_rc "no-jq: add + commit with an untracked secret is caught" 2
    rm -f "$P/src/settings.py"
    run_hook guard-bash "$P" "$(j_bash "$P" "kubectl apply -f x.yaml -n production")" PATH="$NOJQ"
    assert_rc "no-jq: production gate blocked" 2
    run_hook guard-bash "$P" "$(j_bash "$P" "helm upgrade api ./chart -n staging")" PATH="$NOJQ"
    assert_true "no-jq: staging not gated, silent" "$([ "$RC" = 0 ] && [ -z "$OUT$ERR" ] && echo true || echo false)" "rc=$RC out=$OUT err=$ERR"
    run_hook guard-bash "$P" "$(j_bash "$P" "echo release to prod")" PATH="$NOJQ"
    assert_rc "no-jq: a text tool is not a deploy" 0
    run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'release to production'")" PATH="$NOJQ"
    assert_rc "no-jq: git without push is not checked" 0
    run_hook guard-bash "$P" "$(j_bash "$P" $'bash <<EOF\n./deploy prod\nEOF')" PATH="$NOJQ"
    assert_rc "no-jq: a shell heredoc body is read as commands" 2
    run_hook guard-bash "$P" "$(j_bash "$P" $'cat <<EOF\n./deploy prod\nEOF')" PATH="$NOJQ"
    assert_rc "no-jq: a data heredoc body is not" 0
    run_hook guard-bash "$P" "$(j_bash "$P" "cd x && ./deploy prod")" PATH="$NOJQ"
    assert_rc "no-jq: a deploy behind another command is blocked" 2
    set_cfg "$P" protect.deploy_patterns '["ship-it"]'
    run_hook guard-bash "$P" "$(j_bash "$P" "ship-it now")" PATH="$NOJQ"
    assert_rc "no-jq: custom deploy_patterns read from the config array" 2
    run_hook guard-bash "$P" "$(j_bash "$P" "kubectl apply -f x.yaml -n production")" PATH="$NOJQ"
    assert_rc "no-jq: custom deploy_patterns replace the default list" 0
    run_hook session-start "$P" "$(j_session "$P" SessionStart)" PATH="$NOJQ"
    assert_has "no-jq: session-start names the active work doc" "$OUT" "Active work: work/planned.md (slug: planned, status: planned)"
    set_cfg "$P" verify.required_before_stop true; set_cfg "$P" commands.test '"npm test"'
    run_hook post-edit "$P" "$(j_postedit "$P" "$P/src/app.py")" PATH="$NOJQ"
    assert_true "no-jq: post-edit state written" "$([ "$(state_of "$P" edited)" = true ] && echo true || echo false)" "$(cat "$P/.sdlc/state/session-$SID.json" 2>/dev/null)"
    run_hook stop-verify "$P" "$(j_stop "$P" false)" PATH="$NOJQ"
    assert_has "no-jq: stop-verify blocks" "$OUT" '"decision":"block"'
    run_hook post-bash "$P" "$(j_postbash "$P" "npm test" "ok" "")" PATH="$NOJQ"
    run_hook stop-verify "$P" "$(j_stop "$P" false)" PATH="$NOJQ"
    assert_true "no-jq: stop-verify allows after verify" "$([ -z "$OUT" ] && echo true || echo false)" "$OUT"
    set_cfg "$P" protect.protect_tests false
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "edited")" PATH="$NOJQ"
    assert_rc "no-jq: protect_tests=false honored" 0
  fi
else
  echo "SKIP  python fallback (python3 not installed)"
fi

# ======================================================================
# 1.2 — hook fixes. One section per fix; the arrays between @@ markers are generated from the probe files the fixes were developed with.
echo "-- 1.2 deploy gate: prose, read-only commands and test runs pass — real deploys stay blocked (with and without .sdlc/config.json)"
GI=$(fresh); GN=$(fresh_plain)
gate2() { # gate2 <0|2> <command> [VAR=val …] — the same verdict in an initialized project and in one without .sdlc/config.json
  local want="$1" c="$2"; shift 2
  run_hook guard-bash "$GI" "$(j_bash "$GI" "$c")" "$@"; assert_rc "1.2 deploy gate ($want, initialized): $(short "$c")" "$want"
  run_hook guard-bash "$GN" "$(j_bash "$GN" "$c")" "$@"; assert_rc "1.2 deploy gate ($want, no config): $(short "$c")" "$want"
}
DG=(
  $'0|gh pr create --title "Add banner" --body "Before we deploy to prod, check the banner"'
  $'0|gh pr create --title "Notes" --body "$(cat <<\'EOF\'\n## Summary\nReady to release to production next week.\nEOF\n)"'
  $'0|gh pr create --fill --body-file notes/deploy-production.md'
  $'0|gh issue create --title "deploy prod broke" --body "rollout to live failed"'
  $'0|gh pr comment 12 --body "please deploy to production after review"'
  $'0|gh pr view 7 --json body --jq .body'
  $'0|gh run list --workflow deploy-production.yml'
  $'0|gh run view 123 --log'
  $'0|gh api "repos/o/r/deployments?environment=Production"'
  $'0|gh api repos/o/r/deployments --jq \'.[] | select(.environment=="Production") | .sha\''
  $'0|curl -s https://my-app-production.vercel.app/x'
  $'0|curl -sI https://my-app-production.vercel.app/release-notes'
  $'0|curl -s "https://api.example.com/v1/deployments?target=production"'
  $'0|ls deploy/prod'
  $'0|ls .github/workflows/deploy-production.yml'
  $'0|find .github/workflows -name "deploy-production*"'
  $'0|find deploy/prod -type f'
  $'0|stat deploy/prod'
  $'0|cd deploy/prod && ls'
  $'0|test -f deploy/prod/values.yaml'
  $'0|[ -d deploy/prod ] && echo yes'
  $'0|[[ -d deploy/production ]] && echo yes'
  $'0|awk \'/release/ && /production/\' CHANGELOG.md'
  $'0|awk -F: \'{print $1}\' deploy/prod/hosts'
  $'0|sed -n \'1,40p\' .github/workflows/deploy-production.yml'
  $'0|sed -n 1,40p deploy/prod/values.yaml'
  $'0|yq \'.jobs.deploy\' .github/workflows/deploy-production.yml'
  $'0|yq e \'.release.production\' deploy/prod/values.yaml'
  $'0|node -e "console.log(\'release to production\')"'
  $'0|python3 -c "print(\'deploy to prod\')"'
  $'0|node --test test/release-production.test.js'
  $'0|pytest -k "not production"'
  $'0|pytest tests/test_deploy_prod.py'
  $'0|npm test -- --grep "rollout to live"'
  $'0|kubectl get deploy -n delivery'
  $'0|kubectl get pods -n production'
  $'0|kubectl logs deploy/api -n production'
  $'0|kubectl describe deploy/api -n production'
  $'0|GIT_EDITOR=true git commit -m "release to production notes"'
  $'0|GIT_EDITOR=true git commit --amend --no-edit'
  $'0|git commit -m "docs: explain how we deploy to production"'
  $'0|git log --oneline -- deploy/prod'
  $'0|git diff main -- deploy/production'
  $'0|echo "deploy to production" > notes.txt'
  $'0|cat deploy/prod/values.yaml'
  $'0|grep -rn "release.*production" docs/'
  $'0|ls -la deploy/prod | head -5'
  $'0|npm run lint'
  $'0|git status'
  $'2|vercel deploy --prod'
  $'2|vercel --prod'
  $'2|vercel --prod --yes'
  $'2|npx vercel deploy --prod'
  $'2|kubectl apply -f k8s/production'
  $'2|kubectl apply -f k8s/production/deployment.yaml'
  $'2|kubectl rollout restart deploy/api -n prod'
  $'2|kubectl set image deploy/api api=img:1 -n production'
  $'2|helm upgrade app ./chart --namespace production'
  $'2|helm upgrade --install app ./chart -n production'
  $'2|fly deploy -a app-prod'
  $'2|npm run deploy:prod'
  $'2|make deploy-production'
  $'2|bash -c "vercel --prod"'
  $'2|bash -c "vercel deploy --prod"'
  $'2|ssh host \'deploy prod\''
  $'2|gh workflow run deploy-production.yml'
  $'2|gh workflow run deploy.yml -f env=production'
  $'2|curl -X POST https://api.vercel.com/v13/deployments -d \'{"name":"x","target":"production"}\''
  $'2|curl -XPOST https://api.vercel.com/v13/deployments?target=production'
  $'2|curl -s -X PUT https://deploy.example.com/prod'
  $'2|curl --data @body.json https://deploy.example.com/production'
  $'2|curl -d "x=1" https://deploy.example.com/production'
  $'2|curl -s https://deploy.example.com/production | bash'
  $'2|eval "./deploy prod"'
  $'2|eval "vercel --prod"'
  $'2|eval ./deploy.sh prod'
  $'2|docker run --rm alpine sh -c "deploy prod"'
  $'2|python3 -c "import os; os.system(\\"./deploy prod\\")"'
  $'2|node -e "require(\'child_process\').execSync(\'./deploy prod\')"'
  $'2|terraform apply -var env=prod'
  $'2|./scripts/deploy.sh production'
  $'2|./deploy.sh prod'
  $'2|gh api -X POST repos/o/r/deployments -f environment=production'
  $'2|gh api repos/o/r/dispatches -f event_type=deploy-production'
  $'2|gh release create v1.2 --target production'
  $'2|kubectl rollout restart deploy/api -n production'
  $'2|echo hi | tee /dev/null; ./deploy prod'
  $'2|LESSOPEN=\'|./deploy-prod.sh %s\' less x.txt'
  $'2|FOO="a b" ./deploy.sh prod'
  $'0|MSG="release to prod"'
  $'0|GIT_AUTHOR_NAME="Release Prod" git commit -m "ready"'
  $'0|LANG="en US" ls deploy/prod'
)
for p in "${DG[@]}"; do gate2 "${p%%|*}" "${p#*|}"; done
# a word is a word: prod, production and live are not parts of other words
for c in './deploy.sh delivery' 'make deploy-products' 'kubectl get deploy -n delivery' './release.sh liveness-probe' 'npm run release:productivity' 'echo hi; ./deploy.sh produce' \
         './release.sh alive' './promote.sh prodigy'; do gate2 0 "$c"; done
for c in './release.sh live' './deploy.sh --env=prod' './deployProd.sh' 'make release-production' './deploy.sh prod-eu' './deploy.sh eu_prod' './promote.sh live-eu' 'deploy:live'; do gate2 2 "$c"; done
gate2 0 'GIT_EDITOR=true git commit -m "release to production notes"'
gate2 0 'GIT_SEQUENCE_EDITOR=: git rebase main'
gate2 2 'GIT_EDITOR=./deploy-prod.sh git commit'
gate2 2 'vercel --prod'; gate2 0 'vercel --prod' RELEASE_APPROVAL=REL-4711
# the messages: where the approval has to be, and when the config is worth mentioning
run_hook guard-bash "$GI" "$(j_bash "$GI" "./deploy.sh prod")"
assert_has "1.2 deploy message: the approval variable must be in the environment Claude Code was started with" "$ERR" "in its environment"
assert_has "1.2 deploy message: …a prefix on the command does not count" "$ERR" "does not count"
assert_has "1.2 deploy message: names the variable" "$ERR" "RELEASE_APPROVAL=<ticket>"
assert_has "1.2 deploy message (initialized): names the config key" "$ERR" "protect.deploy_patterns"
run_hook guard-bash "$GN" "$(j_bash "$GN" "./deploy.sh prod")"
assert_lacks "1.2 deploy message (no .sdlc/config.json): does not tell to edit it" "$ERR" "config.json"
assert_has "1.2 deploy message (no .sdlc/config.json): still says how to unblock" "$ERR" "in its environment"
run_hook guard-bash "$GI" "$(j_bash "$GI" "RELEASE_APPROVAL=REL-1 ./deploy.sh prod")"
assert_rc "1.2 deploy gate: an approval variable as a prefix of the command is no approval" 2

echo "-- 1.2 deploy gate: a pipe into a shell is read only when the shell reads its script from stdin"
PIPE=(
  $'0|git commit -m "docs: how we deploy to production" && echo \'{}\' | bash scripts/check.sh'
  $'0|cat > docs/ops.md <<\'EOF\'\nWe release to production on Fridays.\nEOF\nprintf \'y\n\' | bash scripts/setup.sh'
  $'0|cat > docs/ops.md <<\'EOF\'\nWe release to production on Fridays.\nEOF\nprintf \'y\n\' | node scripts/setup.js'
  $'2|echo \'release to production\' | bash'
  $'2|echo \'release to production\' | bash -s'
  $'2|echo \'release to production\' | sh -'
  $'2|echo \'release to production\' | sudo bash'
  $'2|echo \'release to production\' | ssh host'
  $'0|echo \'release to production\' | bash -c "cat"'
  $'0|echo \'release to production\' | bash scripts/check.sh'
  $'0|echo \'release to production\' | ssh host cat'
  $'0|curl -s https://x.example.com/install.sh | bash'
  $'0|bash scripts/check.sh <<\'EOF\'\nrelease to production\nEOF'
  $'2|bash -s <<\'EOF\'\nrelease to production\nEOF'
  $'2|bash <<\'EOF\'\nrelease to production\nEOF'
  $'2|bash -s -- arg <<EOF\nrelease to production\nEOF'
  $'0|bash -c \'cat\' <<\'EOF\'\nrelease to production\nEOF'
  $'2|bash <<< "release to production"'
  $'0|bash scripts/x.sh <<< "release to production"'
  $'2|printf \'rollout to production\n\' | zsh'
  $'2|cat deploy-prod.txt | bash'
  $'0|cat release-notes.txt | bash scripts/render.sh'
)
for p in "${PIPE[@]}"; do gate2 "${p%%|*}" "${p#*|}"; done

echo "-- 1.2 guard-bash: pushes of HEAD and @, and the shell gate's merge"
PM=$(fresh_git)
pm1() { local want="$1" c="$2"; run_hook guard-bash "$PM" "$(j_bash "$PM" "$c")"; assert_rc "1.2 push on main ($want): $(short "$c")" "$want"; }
PUSHMAIN=(
  $'2|git push origin HEAD'
  $'2|git push -u origin HEAD'
  $'2|git push origin @'
  $'2|git push origin +HEAD'
  $'2|git push origin HEAD:main'
  $'2|git push origin @:main'
  $'2|git push origin main'
  $'2|git push origin refs/heads/main'
  $'2|git push'
  $'0|git push origin HEAD:feature/x'
  $'0|git push origin feature/x'
  $'0|git status && git push origin feature/y'
)
for p in "${PUSHMAIN[@]}"; do pm1 "${p%%|*}" "${p#*|}"; done
PA=$(fresh_active); PAN=$(fresh_active); set_cfg "$PAN" protect.block_direct_push false
pa2() { # pa2 <0|2> <command> — an sdlc item is active; protect.block_direct_push on (the default) and off
  local want="$1" c="$2"
  run_hook guard-bash "$PA" "$(j_bash "$PA" "$c")"; assert_rc "1.2 active work ($want, block_direct_push default): $(short "$c")" "$want"
  run_hook guard-bash "$PAN" "$(j_bash "$PAN" "$c")"; assert_rc "1.2 active work ($want, block_direct_push=false): $(short "$c")" "$want"
}
PUSHACTIVE=(
  $'0|git push origin HEAD'
  $'0|git push -u origin HEAD'
  $'0|git push origin sdlc/planned'
  $'0|git push'
  $'0|gh pr create --fill'
  $'0|gh pr view 12'
  $'0|gh pr list'
  $'0|gh pr checks 12'
  $'0|echo "gh pr merge 12"'
  $'0|git commit -m "gh pr merge is the gate\'s job"'
  $'2|git push origin main'
  $'2|git push origin HEAD:main'
  $'2|git push origin @:main'
  $'2|git push -u origin HEAD:refs/heads/main'
  $'2|git push --all origin'
  $'2|git push origin sdlc/planned:main'
  $'2|gh pr merge 12'
  $'2|gh pr merge 12 --squash --delete-branch'
  $'2|gh pr merge --auto'
  $'2|gh -R o/r pr merge 3'
  $'2|gh pr merge'
  $'2|bash -c "gh pr merge 12"'
  $'2|sh -c \'git push origin main\''
  $'2|echo $(gh pr merge 12)'
  $'2|echo "$(gh pr merge 12)"'
  $'2|cd x && gh pr merge 12'
  $'2|git status; gh pr merge 12'
  $'2|gh api repos/o/r/pulls/3/merge -X PUT'
  $'2|gh api graphql -f query=\'mutation { mergePullRequest(input:{pullRequestId:"x"}) { clientMutationId } }\''
  $'2|bash <<\'EOF\'\ngh pr merge 12\nEOF'
  $'2|bash -s <<EOF\ngit push origin main\nEOF'
  $'2|eval "git push origin main"'
)
for p in "${PUSHACTIVE[@]}"; do pa2 "${p%%|*}" "${p#*|}"; done
run_hook guard-bash "$PAN" "$(j_bash "$PAN" "gh pr merge 12 --squash")"
assert_has "1.2 merge message: the merge is the shell gate's" "$ERR" "the merge is the shell gate's (\`sdlc.py ship\`); a session never merges"
run_hook guard-bash "$PAN" "$(j_bash "$PAN" "git push origin main")"
assert_has "1.2 push message while sdlc work is active: the same" "$ERR" "a session never merges"
PO=$(fresh_git); PON=$(fresh_git); set_cfg "$PON" protect.block_direct_push false
run_hook guard-bash "$PO" "$(j_bash "$PO" "gh pr merge 12 --squash")"
assert_rc "1.2 outside sdlc work gh pr merge is not blocked" 0
run_hook guard-bash "$PON" "$(j_bash "$PON" "git push origin main")"
assert_rc "1.2 outside sdlc work block_direct_push=false still allows the push" 0
run_hook guard-bash "$PO" "$(j_bash "$PO" "git push origin main")"
assert_has "1.2 outside sdlc work the old message stays" "$ERR" "gh pr create"

echo "-- 1.2 guard-bash: the test lock reaches \$(...), shell heredocs, pushd and directory operands of checkout/restore"
PL=$(fresh_active)
pl1() { local want="$1" c="$2"; run_hook guard-bash "$PL" "$(j_bash "$PL" "$c")"; assert_rc "1.2 test lock ($want): $(short "$c")" "$want"; }
LOCK=(
  $'2|echo "$(rm tests/x_test.py)"'
  $'2|echo `rm tests/x_test.py`'
  $'2|echo "`rm tests/x_test.py`"'
  $'2|echo "done: $(sed -i \'s/a/b/\' tests/x_test.py)"'
  $'2|bash <<\'EOF\'\nrm tests/x_test.py\nEOF'
  $'2|bash -s <<EOF\nrm tests/x_test.py\nEOF'
  $'2|sh <<\'EOF\'\nsed -i \'s/a/b/\' tests/x_test.py\nEOF'
  $'2|sudo bash -s <<\'EOF\'\nmv tests/x_test.py /tmp/\nEOF'
  $'0|bash scripts/foo.sh <<\'EOF\'\nrm tests/x_test.py\nEOF'
  $'0|bash -c \'cat\' <<\'EOF\'\nrm tests/x_test.py\nEOF'
  $'2|cat > notes.txt <<EOF\n$(rm tests/x_test.py)\nEOF'
  $'0|cat > notes.txt <<\'EOF\'\n$(rm tests/x_test.py)\nEOF'
  $'0|echo "$(ls tests)"'
  $'0|echo "$(cat tests/x_test.py)"'
  $'2|pushd tests && rm x_test.py'
  $'2|pushd tests >/dev/null; rm x_test.py'
  $'2|pushd tests; sed -i s/a/b/ x_test.py'
  $'0|pushd src && popd && rm x_test.py'
  $'2|pushd src && popd && rm tests/x_test.py'
  $'0|(cd src && rm x_test.py)'
  $'2|git checkout main -- tests'
  $'2|git checkout main -- tests/'
  $'2|git checkout -- tests'
  $'2|git checkout HEAD -- tests'
  $'2|git checkout main -- .'
  $'2|git restore tests'
  $'2|git restore tests/'
  $'2|git restore --source=main tests'
  $'2|git restore --source=HEAD~1 --worktree --staged tests'
  $'2|git restore src'
  $'2|git checkout main -- src'
  $'2|git restore --source=main .'
  $'0|git restore .'
  $'0|git checkout .'
  $'0|git checkout -- .'
  $'0|git restore --staged tests'
  $'0|git restore --staged .'
  $'0|git checkout feature/x'
  $'0|git checkout -b feature/new'
  $'0|git checkout main'
  $'0|git restore build'
  $'0|git restore schemas'
  $'2|rm -rf tests'
  $'2|rm -rf src'
  $'0|rm -rf build'
  $'0|rm -rf node_modules/*'
  $'0|rm -rf ./node_modules/*'
  $'0|rm -rf build/*'
  $'2|rm -rf tests/*'
  $'2|rm -rf ./tests/*'
  $'2|rm -rf src/*'
  $'2|rm -rf ./src/*'
  $'2|cd tests && rm *'
)
for p in "${LOCK[@]}"; do pl1 "${p%%|*}" "${p#*|}"; done

echo "-- 1.2 guard-bash: a lock check costs one listing of the base, not one per directory"
PF=$(fresh); mk "$PF" src/foo/tests/x_test.py; mk "$PF" pkgs/m5/tests/y_test.py
i=0; while [ "$i" -lt 800 ]; do mk "$PF" "pkgs/m$i/lib/a.py"; mk "$PF" "src/mod$i/a.py"; i=$((i + 1)); done
git_init "$PF"; activate "$PF" planned
i=0; while [ "$i" -lt 800 ]; do mkdir -p "$PF/node_modules/pkg$i" && echo '{}' > "$PF/node_modules/pkg$i/package.json"; i=$((i + 1)); done   # untracked, as dependencies are
unset i
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf node_modules/*")"
assert_true "1.2 rm -rf node_modules/* (800 untracked directories): allowed in under 2 s" "$([ "$RC" = 0 ] && [ "$MS" -lt 2000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf ./node_modules/*")"
assert_true "1.2 rm -rf ./node_modules/*: the same" "$([ "$RC" = 0 ] && [ "$MS" -lt 2000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf pkgs/*")"
assert_true "1.2 rm -rf pkgs/* (800 directories, one holds a test of the base): blocked in under 2 s" "$([ "$RC" = 2 ] && [ "$MS" -lt 2000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
assert_has "1.2 …and names the test" "$ERR" "pkgs/m5/tests/y_test.py"
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf src/*")"
assert_true "1.2 rm -rf src/* blocked: src/foo/tests/x_test.py existed at the base (800 more directories do not slow it)" "$([ "$RC" = 2 ] && [ "$MS" -lt 2000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf tests/*")"
assert_rc "1.2 rm -rf tests/* still blocked" 2
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf pkgs/m1* pkgs/m2*")"
assert_rc "1.2 a glob over directories without tests is allowed" 0
set_cfg "$PF" protect.test_paths '["auto", "qa/**"]'
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf pkgs/*")"
assert_true "1.2 explicit test_paths (matched per file of the listing): rm -rf pkgs/* blocked in under 2 s" "$([ "$RC" = 2 ] && [ "$MS" -lt 2000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf node_modules/*")"
assert_true "1.2 explicit test_paths: rm -rf node_modules/* allowed in under 2 s" "$([ "$RC" = 0 ] && [ "$MS" -lt 2000 ] && echo true || echo false)" "rc=$RC ${MS}ms"
set_cfg "$PF" protect.test_paths '["auto"]'
mk "$PF" pkgs/m7/tests/new_test.py; rm -f "$PF/pkgs/m5/tests/y_test.py"       # a test this change adds is free; the base's own test is already gone
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf pkgs/m7")"
assert_rc "1.2 a directory with only this change's own tests is free" 0
run_hook guard-bash "$PF" "$(j_bash "$PF" "rm -rf pkgs/m5")"
assert_rc "1.2 …and so is one whose base test is no longer there" 0
rm -rf "$PF/node_modules"

echo "-- 1.2 test-config files are test files (guard-edit and guard-bash)"
PC=$(fresh)
TCFG="conftest.py pkg/conftest.py a/b/c/conftest.py pytest.ini jest.config.js jest.config.ts jest.config.mjs vitest.config.ts vitest.config.mts vitest.workspace.ts vitest.workspace.json karma.conf.js .mocharc .mocharc.json .mocharc.yml playwright.config.ts cypress.config.js packages/web/cypress.config.ts phpunit.xml phpunit.xml.dist app/phpunit.xml.dist"
NOTCFG="notjest.config.js jest.config/readme.md conftest.pyc phpunit.xml.bak mocharc.json src/my.mocharc.json"
for f in $TCFG $NOTCFG; do mk "$PC" "$f"; done
git_init "$PC"; activate "$PC" planned
for f in $TCFG; do
  run_hook guard-edit "$PC" "$(j_edit "$PC" "$PC/$f" "changed")"
  assert_rc "1.2 test-config file locked (edit): $f" 2
  run_hook guard-bash "$PC" "$(j_bash "$PC" "sed -i s/x/y/ $f")"
  assert_rc "1.2 test-config file locked (sed -i): $f" 2
done
for f in $NOTCFG; do
  run_hook guard-edit "$PC" "$(j_edit "$PC" "$PC/$f" "changed")"
  assert_rc "1.2 not a test-config file (edit allowed): $f" 0
done
run_hook guard-edit "$PC" "$(j_write "$PC" "$PC/newpkg/conftest.py" "import pytest")"
assert_rc "1.2 a NEW conftest.py is free" 0
run_hook guard-bash "$PC" "$(j_bash "$PC" "echo x > newpkg2/pytest.ini")"
assert_rc "1.2 …and a new pytest.ini written by a shell command" 0

echo "-- 1.2 no jq: a big payload goes through, an unreadable one is blocked"
PJ=$(fresh_git)
if [ -n "${NOJQ:-}" ] && [ -d "$NOJQ" ]; then
  BIG=$(awk 'BEGIN { for (i = 0; i < 24000; i++) printf "line %d of a long file with some words\n", i }')
  run_hook guard-edit "$PJ" "$(j_write "$PJ" "$PJ/src/big.txt" "$BIG")" PATH="$NOJQ"
  assert_rc "1.2 no-jq: a 900 KB Write goes through guard-edit (the payload is not an environment variable any more)" 0
  run_hook guard-edit "$PJ" "$(j_write "$PJ" "$PJ/src/big.txt" "$BIG
aws_key = '$AWS_KEY'")" PATH="$NOJQ"
  assert_rc "1.2 no-jq: …and a secret at the end of it is found" 2
  BIGC=$(awk 'BEGIN { for (i = 0; i < 4000; i++) printf "line %d of a long file with some words\n", i }')
  run_hook guard-bash "$PJ" "$(j_bash "$PJ" "echo '$BIGC' > /dev/null && ./deploy.sh prod")" PATH="$NOJQ"
  assert_rc "1.2 no-jq: a 150 KB command line through guard-bash (over the 128 KB a single environment string may have on Linux), the deploy at its end is found" 2
  unset BIG BIGC
  NOTOOL="$WORK/nojq-nopy-bin"; mkdir -p "$NOTOOL"
  for t in bash sh cat sed grep awk tr date stat mkdir mv rm basename dirname git head tail wc find ls sleep env cp touch cut sort uniq tee mktemp cmp xargs kill perl; do
    src=$(command -v "$t" 2>/dev/null); [ -n "$src" ] && ln -sf "$src" "$NOTOOL/$t"
  done
  run_hook guard-bash "$PJ" "$(j_bash "$PJ" "ls")" PATH="$NOTOOL"
  assert_rc "1.2 neither jq nor python3: guard-bash blocks (fails closed)" 2
  assert_has "1.2 …and says what is missing" "$ERR" "neither jq nor python3"
  run_hook guard-edit "$PJ" "$(j_edit "$PJ" "$PJ/src/app.py" "x = 1")" PATH="$NOTOOL"
  assert_rc "1.2 neither jq nor python3: guard-edit blocks" 2
fi
for h in guard-edit guard-bash; do
  run_hook "$h" "$PJ" "not json at all {"
  assert_rc "1.2 garbage stdin: $h blocks (a guard that cannot read a call does not let it through)" 2
  assert_has "1.2 …with a clear message ($h)" "$ERR" "not valid JSON"
  run_hook "$h" "$PJ" '[1,2,3]'
  assert_rc "1.2 a JSON array is no tool call: $h blocks" 2
  run_hook "$h" "$PJ" ''
  assert_rc "1.2 empty stdin: $h still exits 0" 0
done

echo "-- 1.2 secret scan: case-sensitive tokens, unquoted values and the credential shapes that were missed"
SS="$PLUGIN/scripts/secret-scan.sh"
# the samples are assembled from pieces, like the fake secrets above: this file carries no secret-shaped line itself
ss_hit() { # ss_hit <label> <text> [want 1|0]
  local out rc; out=$(printf '%s\n' "$2" | bash "$SS" --text 2>/dev/null); rc=$?
  assert_eq "1.2 secret scan (${3:-1}): $1" "$rc" "${3:-1}"
}
LOW_AWS=$(printf '%s' "$AWS_KEY" | tr 'A-Z' 'a-z')
ss_hit "an AWS key id" "id = \"$AWS_KEY\""
ss_hit "…but not its lower-case spelling" "x = \"$LOW_AWS\"" 0
ss_hit "…and not inside base64 data in another case" "blob = \"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA${LOW_AWS}BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\"" 0
ss_hit "a keyword in capitals, quoted" 'PASS''WORD = "Zx9Qk2Lm7Pw4Rt6Vbn"'
ss_hit "a keyword in mixed case, quoted" 'Pass''word: "Zx9Qk2Lm7Pw4Rt6Vbn"'
ss_hit "a .env value without quotes" 'DATABASE_PASS''WORD=QedUStPKR0CsTy4Qwb8D'
ss_hit "a YAML value without quotes" '  pass''word: wkNhFdnXsiVpzz63Ff'
ss_hit "a properties value" 'spring.datasource.pass''word=kCzJr4i0B3JrTAwR'
ss_hit "an unquoted AWS secret access key" "AWS_SECRET""_ACCESS_KEY=4y9ojfljoQoaF1LlqsajAIxNKu8iS2G8NPRVdD53"
ss_hit "an AWS secret access key in an ini file" "aws_secret""_access_key = X83RZJzzzzgEOzdmenCkhvMdgaKjIg8xNbe3nNyj"
ss_hit "an OpenAI project key" "OPENAI_API_KEY=sk-""proj-u8jzPde0IgxLd6GncfBAepfJBd0Kh8oOOL8dKLzdocJ2isAj_IhKtJ0RlgLKOmxgJ"
ss_hit "a Stripe live key" "STRIPE_SECRET_KEY=sk_""live_fKtHF4vUCsMehGAkWvj7FAc9"
ss_hit "a Stripe live key in code" "require('stripe')('sk_""live_QeWJKY40uvSwMFLZDe1f8rES')"
ss_hit "a Slack incoming webhook" "SLACK_WEBHOOK=https://hooks.""slack.com/services/TOQ9WMXEH/BH2FDEETF/jgVvVqE1SkHbn88HxjSI6bWH"
ss_hit "an npm token" "NPM_TOKEN=npm_""tP3fS2qHx6kwXoIIXGvOoNZYW2mZp0zVZomH"
ss_hit "a GitHub server token" "token: ghs_""FwUbbYrEqmSM9wCZ7Uw9xfogoEmvnEN5N1aE"
ss_hit "an encrypted private key" "-----BEGIN ENCRYPTED ""PRIVATE KEY-----"
ss_hit "a JSON api key with quotes" '"api_''key": "UkkibjL5DZPjN0MEQ7wjJJibaZUPgHV7"'
ss_hit "an Authorization header" "Author""ization: Bearer iB3m03nbqnsGpWLuqIA1id6Vw5DQL05HA064GiIj"
for ph in 'PASS''WORD=changeme-please-now' 'API_''KEY=your_api_key_goes_here1' 'TOK''EN=<token-from-the-portal>' 'SEC''RET=${SECRET_FROM_ENV}' 'pass''word: ***************' \
          'PASS''WORD=dummy-password-1234' 'sec''ret: test' 'pass''word: example-password-123' 'DB_PASS''WORD=REPLACE_ME_1234567' 'api_''key: xxxxxxxxxxxxxxxx1' \
          'tok''en: placeholder-token-123' 'SECRET_''KEY={{ secret_key_1234567 }}' 'PASS''WORD=short' 'pass''word=changeme' 'sec''ret: <your-secret-here-123>'; do
  ss_hit "a placeholder is not a credential: $ph" "$ph" 0
done
# real files: a lockfile of integrity hashes, a .env.example, a Dockerfile
SC_DIR="$WORK/sscorpus"; mkdir -p "$SC_DIR"
awk 'BEGIN { print "{\n  \"name\": \"demo\",\n  \"lockfileVersion\": 3,\n  \"packages\": {"
  for (i = 0; i < 400; i++) { h = ""; for (j = 0; j < 86; j++) h = h substr("0123456789abcdef", (i * 7 + j * 13 + int(i * j / 3)) % 16 + 1, 1)
    printf "    \"node_modules/pkg%d\": { \"version\": \"1.%d.0\", \"resolved\": \"https://registry.npmjs.org/pkg%d/-/pkg%d-1.%d.0.tgz\", \"integrity\": \"sha512-%s\" },\n", i, i, i, i, i, h }
  print "    \"\": {}\n  }\n}" }' > "$SC_DIR/package-lock.json"
{ printf '%s\n' '# copy to .env and fill in' "DATABASE_URL=post""gres://user:pass""word@localhost:5432/app" 'API_''KEY=your_api_key_here' 'SECRET_''KEY=change-me-to-a-long-random-string' \
    'JWT_SEC''RET=<generate with openssl rand -hex 32>' 'STRIPE_SECRET_''KEY=sk_''test_xxxxxxxxxxxxxxxxxxxxxxxx' 'SMTP_PASS''WORD=' 'AUTH_TOK''EN=${AUTH_TOKEN}'; } > "$SC_DIR/.env.example"
printf '%s\n' 'FROM node:20-alpine' 'ARG NPM_TOKEN' 'ENV NODE_ENV=production' 'RUN echo "//registry.npmjs.org/:_authToken=${NPM_TOKEN}" > .npmrc' > "$SC_DIR/Dockerfile"
for f in package-lock.json .env.example Dockerfile; do
  bash "$SS" --text < "$SC_DIR/$f" >/dev/null 2>&1; assert_eq "1.2 secret scan: no hit in the corpus file $f" "$?" "0"
done
# a hit names where it is, and says when it sits in encoded data
printf 'one\ntwo\nkey = "%s"\n' "$AWS_KEY" > "$SC_DIR/a.txt"
assert_has "1.2 secret scan --text: the line number is reported" "$(bash "$SS" --text < "$SC_DIR/a.txt")" "line 3"
assert_lacks "1.2 secret scan: the secret itself is never printed" "$(bash "$SS" --text < "$SC_DIR/a.txt")" "$AWS_KEY"
printf 'data = "%s%s%s"\n' "$(printf 'A%.0s' $(seq 1 70))" "$AWS_KEY" "$(printf 'B%.0s' $(seq 1 70))" > "$SC_DIR/b.txt"
assert_has "1.2 secret scan: a hit inside a long base64-like run is marked encoded" "$(bash "$SS" --text < "$SC_DIR/b.txt")" "encoded"
assert_lacks "1.2 secret scan: a plain hit is not marked encoded" "$(bash "$SS" --text < "$SC_DIR/a.txt")" "encoded"
SR=$(fresh_git); mkdir -p "$SR/cfg"; printf 'a = 1\nb = 2\naws = "%s"\n' "$AWS_KEY" > "$SR/cfg/keys.py"; gitp "$SR" add cfg/keys.py
assert_has "1.2 secret scan --staged: file and line" "$(bash "$SS" --staged "$SR")" "cfg/keys.py:3"
printf 'later = "%s"\n' "$AWS_KEY" > "$SR/new.py"
assert_has "1.2 secret scan --untracked: file and line" "$(bash "$SS" --staged "$SR" --untracked)" "new.py:1"
# this very file must pass the scanner it tests
bash "$SS" --text < "$HERE/hooks.sh" >/dev/null 2>&1; assert_eq "1.2 secret scan: tests/hooks.sh itself carries no secret-shaped line" "$?" "0"

echo "-- 1.2 commit scan: -a/--all only counts inside the git commit clause; the block says where and how to get out"
PK=$(fresh_git); printf 'aws = "%s"\n' "$AWS_KEY" >> "$PK/src/app.py"           # tracked, modified, not staged
run_hook guard-bash "$PK" "$(j_bash "$PK" "git commit -m x && ls -la")"
assert_rc "1.2 git commit -m x && ls -la: the -la of ls is not commit -a" 0
run_hook guard-bash "$PK" "$(j_bash "$PK" "git commit -m x; ls -a")"
assert_rc "1.2 git commit -m x; ls -a" 0
run_hook guard-bash "$PK" "$(j_bash "$PK" "git commit -m x | tee -a log.txt")"
assert_rc "1.2 …| tee -a is not commit -a" 0
run_hook guard-bash "$PK" "$(j_bash "$PK" 'git commit -m "-a"')"
assert_rc "1.2 a message that reads -a is not commit -a" 0
for c in 'git commit -am x' 'git commit -a -m x' 'git commit -m x -a' 'git commit --all -m x' 'git commit -m x --all' 'git commit -m x -qa' 'git -c user.name=x commit -a -m y' \
         'echo start && git commit -m x -a && ls' 'git commit -m "a b" --all'; do
  run_hook guard-bash "$PK" "$(j_bash "$PK" "$c")"
  assert_rc "1.2 commit -a sees the unstaged secret: $c" 2
done
run_hook guard-bash "$PK" "$(j_bash "$PK" "git commit -am x")"
assert_has "1.2 commit secret block names the file and line" "$ERR" "src/app.py:"
assert_has "1.2 commit secret block names the way out" "$ERR" "secrets.scan_commits: false"
assert_has "1.2 commit secret block says how to take the value out" "$ERR" "git restore --staged"
assert_lacks "1.2 commit secret block does not print the secret" "$ERR" "$AWS_KEY"
printf 'blob = "%s%s%s"\n' "$(printf 'A%.0s' $(seq 1 70))" "$AWS_KEY" "$(printf 'B%.0s' $(seq 1 70))" > "$PK/src/blob.py"; gitp "$PK" checkout -q -- src/app.py; gitp "$PK" add src/blob.py
run_hook guard-bash "$PK" "$(j_bash "$PK" "git commit -m x")"
assert_rc "1.2 a hit inside base64 data still blocks a commit" 2
assert_has "1.2 …and the message says it looks like encoded data" "$ERR" "encoded data"
run_hook guard-edit "$PK" "$(j_write "$PK" "$PK/src/blob2.py" "$(cat "$PK/src/blob.py")")"
assert_rc "1.2 the same blob in an edit blocks" 2
assert_has "1.2 …and the edit message says it looks like encoded data" "$ERR" "encoded data"
assert_lacks "1.2 …(taking the value from the environment is advice for credentials)" "$ERR" "take the value from the environment"
run_hook guard-edit "$PK" "$(j_write "$PK" "$PK/src/k.py" "aws = '$AWS_KEY'")"
assert_has "1.2 edit secret block: the line of the new content" "$ERR" "line 1"
assert_has "1.2 edit secret block: for a credential, take the value from the environment" "$ERR" "environment"

echo "-- 1.2 post-bash: what counts as a verify run and how it is judged"
PV=$(fresh); set_cfg "$PV" commands.test '"npm test"'; set_cfg "$PV" verify.commands '["node --test test/", "pytest -q"]'
vstate() { state_of "$PV" "$1"; }
jb() { # jb <event> <command> <stdout> <stderr> [extra tool_input json] [extra tool_response json]  — a PostToolUse/PostToolUseFailure payload
  local ev="$1" c="$2" so="$3" se="$4" ti="${5:-}" tr="${6:-}"
  if [ "$ev" = PostToolUseFailure ]; then
    printf '{%s,"hook_event_name":"PostToolUseFailure","tool_name":"Bash","tool_use_id":"t1","tool_input":{"command":%s%s},"error":%s,"is_interrupt":false}' "$(base "$PV")" "$(json_escape "$c")" "$ti" "$(json_escape "Exit code 1
$so$se")"
  else
    printf '{%s,"hook_event_name":"PostToolUse","tool_name":"Bash","tool_use_id":"t1","tool_input":{"command":%s%s},"tool_response":{"stdout":%s,"stderr":%s,"interrupted":false%s}}' "$(base "$PV")" "$(json_escape "$c")" "$ti" "$(json_escape "$so")" "$(json_escape "$se")" "$tr"
  fi
}
reset_v() { rm -f "$PV/.sdlc/state/session-$SID.json"; }
reset_v; run_hook post-bash "$PV" "$(jb PostToolUseFailure "npm test" "3 failing" "")"
assert_eq "1.2 PostToolUseFailure of a verify command: recorded as failed" "$(vstate last_verify_ok)" "false"
assert_eq "1.2 …under its name" "$(vstate last_verify_name)" "test"
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm test" "all good" "")"
assert_eq "1.2 PostToolUse of a verify command: a pass" "$(vstate last_verify_ok)" "true"
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm test" "" "Error: something logged but the run passed")"
assert_eq "1.2 PostToolUse: 'Error:' in the output does not make it a failure" "$(vstate last_verify_ok)" "true"
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm test" "FAIL: expected a banner (a test of the failure path)" "")"
assert_eq "1.2 PostToolUse: 'FAIL' in the output does not make it a failure either (no pipe)" "$(vstate last_verify_ok)" "true"
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm test | tail -20" "Tests: 3 failed, 10 passed" "")"
assert_eq "1.2 piped through tail: a failing summary in the output counts" "$(vstate last_verify_ok)" "false"
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm test | tail -20" "Tests: 13 passed" "")"
assert_eq "1.2 piped through tail: a passing summary is a pass" "$(vstate last_verify_ok)" "true"
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm test || echo failed" "" "")"
assert_eq "1.2 || is no pipe" "$(vstate last_verify_ok)" "true"
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm test" "" "" ',"run_in_background":true')"
assert_eq "1.2 a background run records nothing (run_in_background)" "$(vstate last_verify_name)" ""
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm test" "started" "" "" ',"backgroundTaskId":"b1"')"
assert_eq "1.2 a background run records nothing (backgroundTaskId in the response)" "$(vstate last_verify_name)" ""
for c in 'cd app && npm test' 'CI=1 npm test' 'time npm test' 'FOO=1 BAR="a b" npm test -- --watch=false' 'npm test | tail -5' 'echo go; npm test' 'sudo -n npm test' 'bash -c "npm test"' 'node --test test/' 'cd x && node --test test/ 2>&1'; do
  reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "$c" "ok" "")"
  assert_true "1.2 a simple command that starts with the verify command is a run: $c" "$([ -n "$(vstate last_verify_name)" ] && echo true || echo false)" "$(cat "$PV/.sdlc/state/session-$SID.json" 2>/dev/null)"
done
for c in $'cat >> work/x.md <<EOF\nrun node --test test/ and npm test\nEOF' 'echo "npm test"' 'cat >> work/x.md' 'git commit -m "npm test is green"' 'ls test/ npm' 'grep -r "node --test test/" .' 'echo npm test > notes.txt' 'cat npm-test-notes.txt' 'npm tests'; do
  reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "$c" "ok" "")"
  assert_eq "1.2 a command that only mentions the verify command is no run: $(short "$c")" "$(vstate last_verify_name)" ""
done
set_cfg "$PV" verify.commands '["npm run build && npm test"]'
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "cd web && npm run build && npm test" "ok" "")"
assert_true "1.2 a verify command of two clauses: both, in order" "$([ -n "$(vstate last_verify_name)" ] && echo true || echo false)" "$(cat "$PV/.sdlc/state/session-$SID.json" 2>/dev/null)"
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "npm run build" "ok" "")"
assert_eq "1.2 …only the first clause is not that command" "$(vstate last_verify_name)" ""
set_cfg "$PV" verify.commands '["pytest -k \"not slow\""]'
reset_v; run_hook post-bash "$PV" "$(jb PostToolUse "pytest -k 'not slow' -q" "ok" "")"
assert_true "1.2 quotes are normalized: pytest -k 'not slow' is pytest -k \"not slow\"" "$([ -n "$(vstate last_verify_name)" ] && echo true || echo false)" "$(cat "$PV/.sdlc/state/session-$SID.json" 2>/dev/null)"
assert_true "1.2 hooks.json registers post-bash for PostToolUseFailure" "$(grep -A3 '"PostToolUseFailure"' "$PLUGIN/hooks/hooks.json" | grep -q 'post-bash.sh' && echo true || echo false)" "$(cat "$PLUGIN/hooks/hooks.json")"

echo "-- 1.2 paths outside the project: no frozen/generated/test rule, no formatter; the secret scan still applies"
PX=$(fresh_git); activate "$PX" planned; OUTD="$WORK/outside-$$"; mkdir -p "$OUTD"
set_cfg "$PX" protect.generated_paths '["**/*.png", "**"]'
printf '%s\n' '**' >> "$PX/.sdlc/frozen-paths.txt"
run_hook guard-edit "$PX" "$(j_write "$PX" "$OUTD/og-image.png" "png")"
assert_rc "1.2 a Write outside the project is not 'generated' or 'frozen'" 0
run_hook guard-edit "$PX" "$(j_write "$PX" "$OUTD/x_test.py" "def test(): pass")"
assert_rc "1.2 …and not a project test" 0
run_hook guard-edit "$PX" "$(j_write "$PX" "$OUTD/notes.txt" "aws = '$AWS_KEY'")"
assert_rc "1.2 …but the secret scan follows the text wherever it is written" 2
run_hook guard-edit "$PX" "$(j_write "$PX" "$PX/img/og-image.png" "png")"
assert_rc "1.2 inside the project the same patterns still block" 2
activate "$PX" ready
run_hook guard-edit "$PX" "$(j_write "$PX" "$OUTD/og-image.png" "png")"
assert_rc "1.2 design first does not reach outside the project either" 0
printf '#!/bin/bash\ntouch "$(dirname "$0")/formatted-$(basename "$1")"\n' > "$WORK/fmt.sh"; chmod +x "$WORK/fmt.sh"
PF2=$(fresh_git); set_cfg "$PF2" commands.format "\"$WORK/fmt.sh\""
printf 'x\n' > "$OUTD/out.js"; printf 'x\n' > "$PF2/in.js"; rm -f "$WORK"/formatted-*
run_hook post-edit "$PF2" "$(j_postedit "$PF2" "$OUTD/out.js")"
assert_true "1.2 post-edit: no formatter run for a file outside the project" "$([ ! -f "$WORK/formatted-out.js" ] && echo true || echo false)" "$(ls "$WORK" | head -5)"
run_hook post-edit "$PF2" "$(j_postedit "$PF2" "$PF2/in.js")"
assert_true "1.2 post-edit: …but one inside is formatted" "$([ -f "$WORK/formatted-in.js" ] && echo true || echo false)" "$(ls "$WORK" | head -5)"
set_cfg "$PF2" commands.lint_file "\"$WORK/fmt.sh\""; rm -f "$WORK"/formatted-*; set_cfg "$PF2" commands.format '""'
run_hook post-edit "$PF2" "$(j_postedit "$PF2" "$OUTD/out.js")"
assert_true "1.2 post-edit: no linter run for a file outside the project" "$([ ! -f "$WORK/formatted-out.js" ] && echo true || echo false)" "$(ls "$WORK" | head -5)"

echo "-- 1.2 session-start: one line for macOS"
PS=$(fresh); UN="$WORK/uname-bin"; mkdir -p "$UN"
printf '#!/bin/bash\necho Darwin\n' > "$UN/uname"; chmod +x "$UN/uname"
run_hook session-start "$PS" "$(j_session "$PS" SessionStart)" PATH="$UN:$PATH"
assert_has "1.2 session-start on Darwin: the zsh and BSD tools line" "$OUT" "BSD sed/wc/cat"
assert_has "1.2 …names pipefail and PIPESTATUS" "$OUT" "PIPESTATUS is bash-only"
assert_has "1.2 …names wc -l padding and sed -i ''" "$OUT" "sed -i ''"
assert_true "1.2 …and the context stays within 12 lines" "$([ "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" -le 12 ] && echo true || echo false)" "$OUT"
printf '#!/bin/bash\necho Linux\n' > "$UN/uname"
run_hook session-start "$PS" "$(j_session "$PS" SessionStart)" PATH="$UN:$PATH"
assert_lacks "1.2 session-start on Linux: no such line" "$OUT" "BSD sed"

# ======================================================================
echo "-- hooks.json and defaults that must not drift"
HJ="$PLUGIN/hooks/hooks.json"
assert_true "hooks.json parses" "$([ -n "$(json_file_get "$HJ" .description)" ] && echo true || echo false)" "$(head -c 200 "$HJ")"
assert_eq "hooks.json: guard-edit entry has onFailure block" "$(json_file_get "$HJ" '.hooks.PreToolUse[0].hooks[0].onFailure')" "block"
assert_eq "hooks.json: guard-bash entry has onFailure block" "$(json_file_get "$HJ" '.hooks.PreToolUse[1].hooks[0].onFailure')" "block"
assert_has "hooks.json: PreToolUse[0] is the edit guard" "$(json_file_get "$HJ" '.hooks.PreToolUse[0].hooks[0].command')" "guard-edit.sh"
assert_has "hooks.json: PreToolUse[1] is the bash guard" "$(json_file_get "$HJ" '.hooks.PreToolUse[1].hooks[0].command')" "guard-bash.sh"
assert_eq "hooks.json: onFailure appears exactly twice" "$(grep -c '"onFailure"' "$HJ")" "2"
assert_eq "hooks.json: …and only on the two guards" "$(grep '"onFailure"' "$HJ" | grep -c 'guard-\(edit\|bash\)\.sh')" "2"
missing=""; for s in $(grep -o 'scripts/hooks/[a-z-]*\.sh' "$HJ" | sort -u); do [ -f "$PLUGIN/$s" ] || missing="$missing $s"; done
assert_true "hooks.json: every referenced hook script exists" "$([ -z "$missing" ] && echo true || echo false)" "missing:$missing"
if command -v python3 >/dev/null 2>&1; then
  py_patterns=$(PYTHONDONTWRITEBYTECODE=1 python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from sdlclib.config import DEPLOY_PATTERNS; print("\n".join(DEPLOY_PATTERNS))' "$PLUGIN/scripts" 2>/dev/null); py_rc=$?
  if [ "$py_rc" = 0 ] && [ -n "$py_patterns" ]; then
    sh_patterns=$( . "$PLUGIN/scripts/lib/hooks-extra.sh"; printf '%s' "$SDLC_DEFAULT_DEPLOY_PATTERNS" )
    assert_true "default deploy patterns: hooks-extra.sh equals DEPLOY_PATTERNS in sdlclib/config.py" "$([ "$py_patterns" = "$sh_patterns" ] && echo true || echo false)" "py: $(printf '%s' "$py_patterns" | tr '\n' '|') sh: $(printf '%s' "$sh_patterns" | tr '\n' '|')"
  else
    echo "SKIP  default deploy patterns drift check (python3 could not import sdlclib.config)"
  fi
else
  echo "SKIP  default deploy patterns drift check (python3 not installed)"
fi

# The built-in test-path set is one definition for the hooks and the merge gate (scripts/sdlclib/gate.py): the patterns are the same
# text, and over a corpus of paths — every alternative, near misses, the spec/ rule, the work dir — both give the same verdict.
if command -v python3 >/dev/null 2>&1; then
  TPD=$(fresh); TPW=$(fresh); set_cfg "$TPW" paths.work '"tasks"'
  tp_file="$WORK/test-path-corpus.txt"
  if PYTHONDONTWRITEBYTECODE=1 python3 -I - "$PLUGIN/scripts" "$TPD" "$TPW" > "$tp_file" 2>"$WORK/.tp-err" <<'PY'
import random, sys
sys.path.insert(0, sys.argv[1])
from sdlclib.config import Config
from sdlclib.gate import TEST_DIR_RE, TEST_FILE_RE, is_test_path

out = sys.stdout.buffer
out.write(("#patterns\t%s\t%s\n" % (TEST_DIR_RE.pattern, TEST_FILE_RE.pattern)).encode("utf-8"))
cfgs = [("d", Config(sys.argv[2])), ("w", Config(sys.argv[3]))]       # work dir "work" (the default) and "tasks"
dirs = ["", "src", "lib", "test", "tests", "__tests__", "androidTest", "testFixtures", "spec", "specs", "Test", "TESTS", "mytests", "contest",
        "tests_old", "work", "tasks", "work/tests", "tasks/spec", "a b", u"테스트", "app/src/androidTest/java/ui", "node_modules/x/test"]
stems = ["foo", "Foo", "test_foo", "foo_test", "FooTest", "FooTests", "FooSpec", "foo_spec", "Test", "Tests", "Spec", "test_", "_test", "x.test",
         "x.spec", "x.test.test", "x.test.a", "a.spec.b", "README", "conftest", "latest", "contest", "attest", "Footest", "FooTESTS"]
exts = ["", ".py", ".go", ".java", ".kt", ".swift", ".scala", ".rb", ".js", ".jsx", ".ts", ".tsx", ".d.ts", ".js.snap", ".snap", ".map", ".bak", ".md",
        ".json", ".test.js", ".spec.ts", ".test", ".spec", ".", ".JAVA", ".Py", ".test.", ".spec.", ".x1", ".js2", u".ü"]
hand = ["tests/x_test.py", "src/test/Foo.java", "foo.spec.ts", "lib/__tests__/a.js", "pkg/thing_test.go", "app/models/user_spec.rb", "spec/helper.rb",
        "test_util.py", "src/FooTest.java", "web/button.test.tsx", "app/src/androidTest/java/ui/ScreenTest.kt", "lib/testFixtures/data.json",
        "Sources/AppTests.swift", "src/UserSpec.kt", "src/UserSpec.scala", "web/button.test.d.ts", "web/button.test.js.snap", "web/x.spec.d.ts",
        "docs/spec_notes.md", "specs/a.md", "src/latest.py", "src/contest/main.py", "a/test_b.py", "a/test_b.pyc", "a/atest_b.py", "x_test.pyc",
        "Test.java", "Tests.kt", "FooTESTS.swift", "work/tests/a.py", "work/x_test.py", "tasks/tests/a.py", "tasks/x_test.py", "sub/work/tests/a.py",
        "worktests/a.py", "tests", "a/tests", "test", "spec", "a/spec", "Test/a.py", "TESTS/a.py", "a b/c_test.go", u"테스트/foo_test.py",
        "x.test.", "x.spec.", "x.test.js.test.js", "x.test.x1", "a.test.Js", u"ünï/test/ä.py", "foo_test.go.bak", "_test.go", ".test.js",
        "x/.spec.rb", "Footest.java", "FooTest.JAVA", "x.test.js/y.txt", "x.test.js/y_test.py",
        "conftest.py", "pkg/conftest.py", "a/b/conftest.py", "pytest.ini", "sub/pytest.ini", "jest.config.js", "jest.config.ts", "web/jest.config.mjs",
        "vitest.config.ts", "vitest.config.mts", "vitest.workspace.ts", "vitest.workspace.json", "karma.conf.js", ".mocharc", ".mocharc.json",
        ".mocharc.yml", "playwright.config.ts", "cypress.config.js", "packages/web/cypress.config.ts", "phpunit.xml", "phpunit.xml.dist",
        "app/phpunit.xml.dist", "notjest.config.js", "jest.config", "jest.config.", "jest.config/readme.md", ".mocharc.", "mocharc.json",
        "src/my.mocharc.json", "conftest.pyc", "phpunit.xml.bak", "sub/vitest.config", "x/conftest.py/y.txt", "xconftest.py", "Conftest.py",
        "work/conftest.py", "tasks/conftest.py", "pytest.ini.bak", "karma.conf", "cypress.config"]
paths, seen = [], set()
def add(p):
    if p and p not in seen:
        seen.add(p); paths.append(p)
for p in hand: add(p)
for st in stems:
    for ex in exts: add(st + ex)
for d in dirs: add(d + "/x.txt" if d else "x.txt")
rnd = random.Random(20261013)
for _ in range(6000):
    parts = [rnd.choice(dirs) for _ in range(rnd.choice((0, 1, 1, 2)))]
    add("/".join([d for d in parts if d] + [rnd.choice(stems) + rnd.choice(exts)]))
for cid, cfg in cfgs:
    for p in paths:
        out.write(("%s\t%d\t%s\n" % (cid, 1 if is_test_path(cfg, p) else 0, p)).encode("utf-8"))
PY
  then
    # tp_compare real|old → "<paths>|<tests among them>|<mismatches>": the hooks' is_test_path against the verdicts the gate gave.
    # "old" swaps in the set the hooks counted before they mirrored the gate, to show that the corpus can tell the two apart.
    tp_compare() {
      ( . "$PLUGIN/scripts/lib/hooks-extra.sh"
        if [ "$1" = old ]; then
          auto_test_path() {
            local rel="$1" base="${1##*/}"
            case "/$rel" in */test/*|*/tests/*|*/__tests__/*|*/spec/*) return 0;; esac
            case "$base" in *_test.go|*.test.*|*.spec.*|test_*.py|*_test.py|*Test.java|*Tests.java|*Spec.scala|*_spec.rb) return 0;; esac
            return 1
          }
        fi
        cur=""; n=0; pos=0; bad=""
        while IFS=$'\t' read -r id want path; do
          case "$id" in '#'*) continue;; esac
          if [ "$id" != "$cur" ]; then cur="$id"; case "$id" in d) test_paths_load "$TPD";; *) test_paths_load "$TPW";; esac; fi
          got=0; is_test_path "$path" && got=1
          n=$((n + 1)); [ "$want" = 1 ] && pos=$((pos + 1))
          [ "$got" = "$want" ] || bad="$bad [$id] gate=$want hooks=$got $path;"
        done < "$tp_file"
        printf '%s|%s|%s' "$n" "$pos" "$bad" )
    }
    tp_out=$(tp_compare real); tp_n=${tp_out%%|*}; tp_rest=${tp_out#*|}; tp_pos=${tp_rest%%|*}; tp_bad=${tp_rest#*|}
    assert_true "test paths: is_test_path in hooks-extra.sh gives the verdict of the gate's on all $tp_n corpus paths (work dir work and tasks)" "$([ -z "$tp_bad" ] && echo true || echo false)" "$(printf '%s' "$tp_bad" | head -c 700)"
    assert_true "test paths: the corpus is not vacuous ($tp_pos test paths among $tp_n)" "$([ "$tp_n" -gt 1500 ] && [ "$tp_pos" -gt 400 ] && [ $((tp_n - tp_pos)) -gt 400 ] && echo true || echo false)" "n=$tp_n tests=$tp_pos"
    tp_old=$(tp_compare old); tp_old_bad=${tp_old#*|}; tp_old_bad=${tp_old_bad#*|}
    tp_old_differs=false; case "$tp_old_bad" in *ScreenTest.kt*) tp_old_differs=true;; esac       # (not inside $( ): bash 3.2 ends the substitution at the first "pattern)")
    assert_true "test paths: …and it does tell the old hooks-only set apart (androidTest, Kotlin and Swift names, .test.d.ts)" "$tp_old_differs" "$(printf '%s' "$tp_old_bad" | head -c 300)"
    IFS=$'\t' read -r _ gate_dir_re gate_file_re <<< "$(head -n 1 "$tp_file")"
    sh_re=$( . "$PLUGIN/scripts/lib/hooks-extra.sh"; printf '%s\t%s' "$SDLC_TEST_DIR_RE" "$SDLC_TEST_FILE_RE" )
    assert_eq "test paths: SDLC_TEST_DIR_RE is TEST_DIR_RE of sdlclib/gate.py" "${sh_re%%$'\t'*}" "$gate_dir_re"
    assert_eq "test paths: SDLC_TEST_FILE_RE is TEST_FILE_RE of sdlclib/gate.py" "${sh_re#*$'\t'}" "$gate_file_re"
  else
    echo "SKIP  test-path drift check (python3 could not import sdlclib.gate: $(head -c 200 "$WORK/.tp-err" | tr '\n' ' '))"
  fi
else
  echo "SKIP  test-path drift check (python3 not installed)"
fi

# old_glob_match: what the hooks used before they read globs like the gate does (bash `case` patterns: `*` crossed /, a pattern matched at any depth)
old_glob_match() {
  local pat="$1" p="$2"
  case "$pat" in
    */) case "$p" in "$pat"*|"${pat%/}") return 0;; esac; return 1 ;;
  esac
  local simple="${pat//\*\*\//}"
  case "$p" in $pat) return 0;; $simple) return 0;; */$pat) return 0;; esac
  case "$pat" in */*) return 1;; esac
  case "${p##*/}" in $pat) return 0;; esac
  return 1
}

# The glob rules of the config lists (frozen paths, generated paths, protect.test_paths) are one definition with the merge gate
# (util.glob_match): over a corpus of patterns x paths — the template's examples, every wildcard in odd places, regex
# metacharacters, ./ and trailing / — both give the same verdict. Not in the corpus: paths with a newline and non-ASCII
# characters under `?` (the hooks match bytes, the gate characters).
if command -v python3 >/dev/null 2>&1; then
  gl_file="$WORK/glob-corpus.txt"
  if PYTHONDONTWRITEBYTECODE=1 python3 -I - "$PLUGIN/scripts" > "$gl_file" 2>"$WORK/.gl-err" <<'PY'
import random, sys
sys.path.insert(0, sys.argv[1])
from sdlclib.util import glob_match

PATS = [
    "src/gen/", "**/generated/**", "legacy/v1/", "schemas/*.avsc",                                             # the template's examples
    "qa/", "qa", "qa/*", "qa/*.sh", "qa/**", "qa/**/*.sh", "**/qa/*.sh", "*.sh", "smoke.sh", "tests/*.py", "tests/**/*.py", "**/tests/**",
    "src/**/gen/", "*_test.py", "e2e/**", "e2e/", "test?.py", "a/b/c.py", "**/c.py", "dist/**", "**/dist/**", "build/", "x/*/y.py",
    "a.b", "a+b", "a(b)", "a[b]", "[ab]", "a{b}", "{a,b}", "*.{js,ts}", "a$b", "a^b", "^a", "a$", "a|b", "a\\b", "\\", "a.*", ".*", "(a)",
    "a++", "a**b", "a?b", "a.b/", "(x)/", "[x]/", "*", "**", "***", "****", "?", "??", "?/?", "a/?", "?/a", "*/", "**/", "/**", "**/*",
    "*/**", "*/*", "**/**", "a/**/**/b", "a/**/b", "a**/b", "**a", "a**", "**/a/**", "**/*.py", "*/*.py", "**/*/*.py", "a/*/b", "a/**/",
    ".", "..", "./a", "a/.", ".env", "**/.env", ".*/a", "/", "/a", "a//b", "a/", "//", "", " src/ ", "\tsrc/gen/\t", "  a.b  ", "   ", "\t",
    u"테스트/", u"테*", u"*.테스트", "a b", "a b/", "a b/*", "dir with space/**", "#a", "a#b", "~", "&", "'", "\"", "`", "$HOME", "${x}",
    "a;b", "a<b>", "a=b", "!a", "@a", "%a", "a,b", "a:b", "-a", "--", "-n",
    "a{2}", "{1}", "x{1,2}", "a{2,}", "{,3}", "a\\w", "\\b", "a\\<b", "\\1", "a\\.b", "[*]", "[?]", "(*)", "*(a)", "a|*", "^*", "*$", "$*",
]
BASE = [
    "qa/smoke.sh", "qa/sub/smoke.sh", "qa/sub/deep/smoke.sh", "qa", "x/qa/smoke.sh", "smoke.sh", "a/smoke.sh", "tests/a.py", "tests/sub/a.py",
    "x/tests/a.py", "x/tests/sub/a.py", "foo_test.py", "a/foo_test.py", "src/gen/a.py", "src/gen", "src/gen/", "src/x/gen/a.py", "src/generated/a",
    "generated/b.txt", "a/generated/b.txt", "a/b/generated/c/d.txt", "generated", "legacy/v1/a", "legacy/v1", "legacy/v12/a", "x/legacy/v1/a",
    "schemas/a.avsc", "schemas/sub/a.avsc", "x/schemas/a.avsc", "schemas/a.avsc.bak", "e2e/a.js", "e2e", "e2e/sub/a.js", "test1.py", "test12.py",
    "a/b/c.py", "x/a/b/c.py", "c.py", "dist/a.js", "pkg/dist/a.js", "pkg/dist", "build/a", "x/build/a", "x/m/y.py", "x/m/n/y.py", "./qa/smoke.sh",
    "././qa/smoke.sh", "./", ".", "", "a.b", "aXb", "a+b", "a(b)", "ab", "a[b]", "b", "a{b}", "a$b", "a^b", "a|b", "a\\b", "\\", "a.py", ".env",
    "dir/.env", "a b", "a b/c", "dir with space/x/y", u"테스트/a", u"테스트", u"x.테스트", "/abs/x", "a//b", "a/b", "a/", "a", "a/b/", "x/y/z", "x/y",
    "-n", "--", "$HOME", "#a", "a#b", "a{2}", "aa", "x{1,2}", "x", "xx", "a{2,}", "{1}", "1", "a\\w", "aw", "\\b", "a\\<b", "a<b", "\\1", "a\\.b",
]
rnd = random.Random(20261013)

def expand(pat, cap):
    """paths that match the pattern, as it reads a wildcard in a few ways, and near misses of them"""
    toks, i, p = [], 0, pat.strip()
    while i < len(p):
        if p.startswith("**/", i): toks.append(["", "d/", "d/e/"]); i += 3
        elif p.startswith("**", i): toks.append(["", "x", "d/x", "d/e/x"]); i += 2
        elif p[i] == "*": toks.append(["", "x", "x.y"]); i += 1
        elif p[i] == "?": toks.append(["x", "."]); i += 1
        else: toks.append([p[i]]); i += 1
    outs = [""]
    for opts in toks:
        outs = [o + t for o in outs for t in opts]
        if len(outs) > 64: outs = rnd.sample(outs, 64)
    near = []
    for o in outs:
        near += [o, "x/" + o, o + "/x", o + "x", "./" + o, o + "/", o[1:]]
    near = sorted(set(near))
    return rnd.sample(near, min(cap, len(near)))

pairs = []
for pat in PATS:
    for path in expand(pat, 30) + BASE:
        pairs.append((pat, path))
SEG = ["a", "b", "x", ".", "/", "+", "(", "[", "{", "$", "^", "|", "\\", " ", "d/", "-"]
TOK = SEG + ["*", "**", "?", "**/", "]", "}", ")"]
for _ in range(300):
    pat = "".join(rnd.choice(TOK) for _ in range(rnd.randint(1, 6)))
    if not pat.strip(): continue
    paths = expand(pat, 6) + ["".join(rnd.choice(SEG) for _ in range(rnd.randint(0, 6))) for _ in range(6)]
    for path in paths: pairs.append((pat, path))
out = sys.stdout.buffer
for pat, path in pairs:
    out.write(("%s\x1f%s\x1f%d\n" % (pat, path, 1 if glob_match(pat, path) else 0)).encode("utf-8"))
PY
  then
    # gl_compare [old] → "<pairs>|<matching>|<mismatches>|<the first of them>": glob_match of common.sh against the verdicts of util.glob_match
    gl_compare() {
      ( . "$PLUGIN/scripts/lib/common.sh"
        [ "${1:-}" = old ] && glob_match() { old_glob_match "$@"; }
        n=0; pos=0; nbad=0; bad=""
        while IFS=$'\x1f' read -r pat path want; do
          got=0; glob_match "$pat" "$path" && got=1
          n=$((n + 1)); [ "$want" = 1 ] && pos=$((pos + 1))
          if [ "$got" != "$want" ]; then nbad=$((nbad + 1)); [ "${#bad}" -lt 700 ] && bad="$bad [$pat] $path gate=$want hooks=$got;"; fi
        done < "$gl_file"
        printf '%s|%s|%s|%s' "$n" "$pos" "$nbad" "$bad" )
    }
    gl_out=$(gl_compare); gl_n=${gl_out%%|*}; gl_rest=${gl_out#*|}; gl_pos=${gl_rest%%|*}; gl_rest=${gl_rest#*|}; gl_nbad=${gl_rest%%|*}; gl_bad=${gl_rest#*|}
    assert_true "glob: glob_match in common.sh gives the verdict of util.glob_match on all $gl_n corpus pairs" "$([ "$gl_nbad" = 0 ] && echo true || echo false)" "$gl_nbad differ: $gl_bad"
    assert_true "glob: the corpus is not vacuous ($gl_pos matching pairs among $gl_n)" "$([ "$gl_n" -gt 5000 ] && [ "$gl_pos" -gt 1000 ] && [ $((gl_n - gl_pos)) -gt 1000 ] && echo true || echo false)" "n=$gl_n matching=$gl_pos"
    gl_old=$(gl_compare old); gl_old_rest=${gl_old#*|}; gl_old_rest=${gl_old_rest#*|}; gl_old_nbad=${gl_old_rest%%|*}
    assert_true "glob: …and it does tell the old hooks-only matcher apart ($gl_old_nbad pairs differ)" "$([ "$gl_old_nbad" -gt 100 ] && echo true || echo false)" "$gl_old_rest"
  else
    echo "SKIP  glob drift check (python3 could not import sdlclib.util: $(head -c 200 "$WORK/.gl-err" | tr '\n' ' '))"
  fi
else
  echo "SKIP  glob drift check (python3 not installed)"
fi
# The pairs the old matcher got differently from the gate (`*` crossed /, a pattern matched at any depth, no ./ strip): the
# gate's verdict is spelled out here, and needs no python3.
GL_OLD_DIFF=('qa/|./qa/smoke.sh|1' 'qa/*|qa/sub/smoke.sh|0' 'qa/*|qa/sub/deep/smoke.sh|0' 'qa/*|x/qa/smoke.sh|0' 'qa/*.sh|qa/sub/smoke.sh|0'
  'qa/*.sh|qa/sub/deep/smoke.sh|0' 'qa/*.sh|x/qa/smoke.sh|0' 'qa/**|x/qa/smoke.sh|0' 'qa/**/*.sh|./qa/smoke.sh|1' '**/qa/*.sh|qa/sub/smoke.sh|0'
  '**/qa/*.sh|qa/sub/deep/smoke.sh|0' 'tests/*.py|tests/sub/a.py|0' 'tests/*.py|x/tests/a.py|0' 'tests/*.py|x/tests/sub/a.py|0'
  'tests/**/*.py|x/tests/sub/a.py|0' 'a/b/c.py|x/a/b/c.py|0' 'dist/**|pkg/dist/a.js|0' 'x/*/y.py|x/m/n/y.py|0')
gl_ok=true; gl_why=""
for t in "${GL_OLD_DIFF[@]}"; do
  IFS='|' read -r gl_pat gl_path gl_want <<< "$t"
  gl_new=0; glob_match "$gl_pat" "$gl_path" && gl_new=1
  gl_was=0; old_glob_match "$gl_pat" "$gl_path" && gl_was=1
  [ "$gl_new" = "$gl_want" ] && [ "$gl_was" != "$gl_want" ] || { gl_ok=false; gl_why="$gl_why [$gl_pat] $gl_path want=$gl_want now=$gl_new before=$gl_was;"; }
done
assert_true "glob: the ${#GL_OLD_DIFF[@]} pairs the old matcher got differently now follow the gate (* stops at /, anchored at the root, ./ dropped)" "$gl_ok" "$gl_why"
# bytes, not characters: a file name that is not valid UTF-8 (a legacy encoding) is a name like any other, whatever the locale
gl_bytes=0
for gl_loc in C en_US.UTF-8 C.UTF-8 C.utf8; do
  ( export LC_ALL=$gl_loc; . "$PLUGIN/scripts/lib/common.sh"
    glob_match '*.txt' $'dir/\xb0\xa1.txt' && glob_match '**/*.txt' $'a/\xb0\xa1/b.txt' && glob_match 'a/*' $'a/\xb0\xa1' \
      && ! glob_match 'a/*' $'a/\xb0\xa1/c' && ! glob_match 'b/*' $'a/\xb0\xa1' ) 2>/dev/null && gl_bytes=$((gl_bytes + 1))
done
# 1.2: a leading / or ./ roots the pattern, a pattern of slashes matches nothing, dir/ takes globs before the slash, no trailing-newline match
GL_NEW=('/qa/*|qa/a.sh|1' '/qa/*|x/qa/a.sh|0' './qa/*|qa/a.sh|1' './qa/*|x/qa/a.sh|0' '/*.tf|main.tf|1' '/*.tf|deep/x/main.tf|0' '*.tf|deep/x/main.tf|1'
  '/infra/**|infra/a/b.tf|1' '/infra/**|x/infra/a.tf|0' '//qa/|qa/a.sh|1' '/|a|0' '//|a|0' '///|a/b|0' '/|/|0' './|a|0' './/|a|0'
  '**/migrations/|db/migrations/0001.sql|1' '**/migrations/|migrations/0001.sql|1' '**/migrations/|db/migrations|1' '**/migrations/|db/migrationsx/a|0'
  'src/*/secrets/|src/app/secrets/k.pem|1' 'src/*/secrets/|src/app/x/secrets/k.pem|0' 'src/*/secrets/|src/secrets/k.pem|0' 'src/g?n/|src/gen/a.py|1'
  './src/**/gen/|src/d/gen/a|1' '/src/gen/|src/gen/a.py|1' '/src/gen/|x/src/gen/a.py|0' 'qa/|./qa/a.sh|1' 'smoke.sh|qa/deep/smoke.sh|1' '/smoke.sh|qa/smoke.sh|0')
gl_ok=true; gl_why=""
for t in "${GL_NEW[@]}"; do
  IFS='|' read -r gl_pat gl_path gl_want <<< "$t"
  gl_new=0; glob_match "$gl_pat" "$gl_path" && gl_new=1
  [ "$gl_new" = "$gl_want" ] || { gl_ok=false; gl_why="$gl_why [$gl_pat] $gl_path want=$gl_want got=$gl_new;"; }
done
assert_true "glob: rooted patterns (/ and ./), slash-only patterns, dir/ with globs before the slash — ${#GL_NEW[@]} verdicts, as in the gate" "$gl_ok" "$gl_why"
gl_nl=""                                                              # the newline is a character of the name: only a pattern that ends before it fails
glob_match 'qa/a.sh' $'qa/a.sh\n' && gl_nl="$gl_nl qa/a.sh-matched"
glob_match '*.sh' $'qa/a.sh\n' && gl_nl="$gl_nl *.sh-matched"
glob_match 'qa/*' $'qa/a.sh\n' || gl_nl="$gl_nl qa/*-missed"
glob_match 'qa/' $'qa/a.sh\n' || gl_nl="$gl_nl qa/-missed"
assert_eq "glob: a path with a trailing newline no longer matches a pattern that ends before it (qa/a.sh, *.sh); qa/* and qa/ take it as a name" "$gl_nl" ""
assert_true "glob: file names with bytes that are no valid UTF-8 match like any other, in every locale ($gl_bytes of 4 locales)" "$([ "$gl_bytes" = 4 ] && echo true || echo false)" "$gl_bytes"

# ======================================================================
echo "-- timing"
assert_true "every hook run completed in < 1s, the python fallback in < 2.5s (slowest: $MAX_MS_HOOK ${MAX_MS}ms)" "$([ -z "$SLOW" ] && echo true || echo false)" "slow runs:$SLOW"

echo "== $PASS passed, $FAIL failed (slowest hook run: $MAX_MS_HOOK ${MAX_MS}ms)"
[ "$FAIL" -eq 0 ]
