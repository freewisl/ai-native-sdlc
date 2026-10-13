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
assert_rc "commit message mentioning production is a gate false positive (documented)" 2
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
assert_rc "limit: a glob over more own tests than the per-command lookup cap (30) is treated as existing — blocked" 2
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
{ gitp "$P" restore --staged src/leak.py 2>/dev/null || gitp "$P" reset -q src/leak.py; }; rm -f "$P/src/leak.py"
printf 'x = 1\n' >> "$P/src/app.py"; gitp "$P" add src/app.py
run_hook guard-bash "$P" "$(j_bash "$P" "git commit -m 'clean change'")"
assert_rc "clean staged commit allowed" 0
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
assert_lacks "--untracked: neither is the file name or its text" "$out" "u.py"
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
assert_lacks "block reason does not mention removed skills" "$OUT" "/sdlc:verify"
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
echo "-- robustness: empty / garbage stdin never blocks"
P=$(fresh)
for h in session-start session-end guard-edit guard-bash post-edit post-bash stop-verify; do
  run_hook "$h" "$P" ""
  assert_rc "empty stdin: $h exits 0" 0
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
        "x/.spec.rb", "Footest.java", "FooTest.JAVA", "x.test.js/y.txt", "x.test.js/y_test.py"]
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
assert_true "glob: file names with bytes that are no valid UTF-8 match like any other, in every locale ($gl_bytes of 4 locales)" "$([ "$gl_bytes" = 4 ] && echo true || echo false)" "$gl_bytes"

# ======================================================================
echo "-- timing"
assert_true "every hook run completed in < 1s, the python fallback in < 2.5s (slowest: $MAX_MS_HOOK ${MAX_MS}ms)" "$([ -z "$SLOW" ] && echo true || echo false)" "slow runs:$SLOW"

echo "== $PASS passed, $FAIL failed (slowest hook run: $MAX_MS_HOOK ${MAX_MS}ms)"
[ "$FAIL" -eq 0 ]
