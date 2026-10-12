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

# ======================================================================
echo "-- guard-edit: test-file lock (existing files locked, new ones free)"
P=$(fresh)
mk "$P" lib/__tests__/a.js; mk "$P" pkg/thing_test.go; mk "$P" app/models/user_spec.rb; mk "$P" spec/helper.rb
mk "$P" test_util.py; mk "$P" src/FooTest.java; mk "$P" web/button.test.tsx; mk "$P" src/contest/main.py; mk "$P" src/latest.py
for f in tests/x_test.py src/test/Foo.java foo.spec.ts lib/__tests__/a.js pkg/thing_test.go app/models/user_spec.rb spec/helper.rb test_util.py src/FooTest.java web/button.test.tsx; do
  run_hook guard-edit "$P" "$(j_edit "$P" "$P/$f" "changed")"
  assert_rc "existing test path blocked: $f" 2
done
for f in src/app.py src/contest/main.py src/latest.py; do
  run_hook guard-edit "$P" "$(j_edit "$P" "$P/$f" "changed")"
  assert_rc "existing non-test path allowed: $f" 0
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
assert_has "test lock message points at the work doc" "$ERR" "work/<slug>.md"
assert_has "test lock message says tests this change adds are free" "$ERR" "test files this change adds stay free to edit"
assert_lacks "test lock message does not mention the verify skill" "$ERR" "/sdlc:verify"
assert_lacks "test lock message does not mention plan files" "$ERR" "plan/"
assert_true "test lock was logged" "$(log_has "$P" '"decision":"deny".*test_lock')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")" SDLC_ALLOW_TEST_EDIT=1
assert_rc "existing test edit allowed via SDLC_ALLOW_TEST_EDIT=1" 0
mkdir -p "$P/.sdlc/state"; touch "$P/.sdlc/state/allow-test-edit"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "existing test edit allowed via .sdlc/state/allow-test-edit" 0
rm -f "$P/.sdlc/state/allow-test-edit"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "lock is back once the unlock file is gone" 2
set_cfg "$P" protect.protect_tests false
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "protect_tests=false disables the lock (false must be honored)" 0
set_cfg "$P" protect.protect_tests true
mk "$P" qa/smoke.sh
set_cfg "$P" protect.test_paths '["qa/"]'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/qa/smoke.sh" "changed")"
assert_rc "custom test_paths prefix blocked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "custom test_paths replaces the auto set" 0
set_cfg "$P" protect.test_paths '[]'
run_hook guard-edit "$P" "$(j_edit "$P" "$P/qa/smoke.sh" "changed")"
assert_rc "an explicit empty test_paths locks nothing" 0
Q=$(fresh)
assert_true "the fixture's version-2 config is sparse (no protect.test_paths)" "$([ -z "$(json_file_get "$Q/.sdlc/config.json" .protect.test_paths)" ] && echo true || echo false)" "$(cat "$Q/.sdlc/config.json")"
run_hook guard-edit "$Q" "$(j_edit "$Q" "$Q/tests/x_test.py" "changed")"
assert_rc "sparse config: an unset test_paths means auto" 2

echo "-- guard-edit: test lock and the active work doc"
P=$(fresh_git)
activate "$P" allowed
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "existing test edit allowed: active doc says test_changes: allowed (comment after the value)" 0
activate "$P" planned
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "existing test edit blocked: active doc says test_changes: none" 2
assert_has "lock message names the active work doc" "$ERR" "work/planned.md"
gitp "$P" checkout -q main
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "stale active-work (planned) on main: lock stays" 2
printf 'allowed\n' > "$P/.sdlc/state/active-work"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "stale active-work naming an allowing doc on main does not unlock" 2
gitp "$P" checkout -q sdlc/planned
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "active-work naming another slug than the branch does not unlock" 2
activate "$P" ghost
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "active slug without a doc: lock stays, no crash" 2
printf '../../etc/passwd\n' > "$P/.sdlc/state/active-work"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")"
assert_rc "garbage active-work: lock stays, no crash" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "changed")" SDLC_ALLOW_TEST_EDIT=1
assert_rc "env unlock still works next to an active doc" 0

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
assert_rc "once the change is on the default branch its tests are existing tests: locked" 2
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
P=$(fresh); gitp "$P" init -q . >/dev/null 2>&1
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
echo "-- guard-bash: test-file lock for shell commands"
P=$(fresh_git)
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
mkdir -p "$P/bulk"; ( cd "$P/bulk" && seq 1 2300 | sed 's/^/f/' | xargs touch ); mk "$P" bulk/a_test.py
gitp "$P" add bulk/a_test.py >/dev/null 2>&1; gitp "$P" commit -qm "bulk test" >/dev/null 2>&1       # an existing test: it is in the default branch
run_hook guard-bash "$P" "$(j_bash "$P" "rm -f bulk/*")"
assert_rc "blocked: a glob over a big directory still finds the test file in it" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm -f bulk/f*")"
assert_rc "allowed, and fast (timing check below): a glob that expands to 2300 non-test files" 0
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_has "block message names the file" "$ERR" "tests/x_test.py"
assert_has "block message names the action" "$ERR" "delete (rm)"
assert_has "block message names the unlock in the work doc" "$ERR" "test_changes: allowed"
assert_has "block message names the last-resort env" "$ERR" "SDLC_ALLOW_TEST_EDIT=1"
assert_lacks "block message does not mention removed skills" "$ERR" "/sdlc:verify"
assert_true "shell test lock was logged" "$(log_has "$P" 'test_lock_bash')" "$(cat "$P/.sdlc/logs/events.jsonl" 2>/dev/null)"
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")" SDLC_ALLOW_TEST_EDIT=1
assert_rc "unlock: SDLC_ALLOW_TEST_EDIT=1" 0
mkdir -p "$P/.sdlc/state"; touch "$P/.sdlc/state/allow-test-edit"
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "unlock: .sdlc/state/allow-test-edit" 0
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
assert_rc "no unlock: stale active-work naming an allowing doc on main" 2
rm -f "$P/.sdlc/state/active-work"
set_cfg "$P" protect.protect_tests false
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "unlock: protect_tests=false" 0
set_cfg "$P" protect.protect_tests true
mk "$P" qa/smoke.sh; set_cfg "$P" protect.test_paths '["qa/"]'
gitp "$P" add qa/smoke.sh >/dev/null 2>&1; gitp "$P" commit -qm "qa test" >/dev/null 2>&1
run_hook guard-bash "$P" "$(j_bash "$P" "rm qa/smoke.sh")"
assert_rc "custom test_paths apply to shell commands" 2
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "custom test_paths replace the auto set for shell commands" 0
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
assert_rc "locked: the same test once the change is on the default branch" 2
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
P=$(fresh); gitp "$P" init -q . >/dev/null 2>&1
run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")"
assert_rc "a repository without any commit: no base, existing tests are locked for shell commands too" 2

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
echo "-- version-1 config (a 0.x repository that has not run init yet)"
P=$(fresh_v1); git_init "$P"
run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/gen/client.txt" "x")"
assert_rc "v1: frozen path blocked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/tests/x_test.py" "x")"
assert_rc "v1: existing test locked" 2
run_hook guard-edit "$P" "$(j_edit "$P" "$P/spec/demo.md" "x")"
assert_rc "v1: paths.spec dir is the artifact dir, not a test dir" 0
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
assert_has "context: guards — existing tests locked" "$OUT" "existing test files locked"
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
activate "$P" ready
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: ready doc → design-first note" "$OUT" "design first: edits outside work/"
activate "$P" allowed
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: allowing doc → tests unlocked" "$OUT" "existing test files: UNLOCKED"
activate "$P" ghost
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: active slug without a doc is reported" "$OUT" "file missing"
activate "$P" planned; gitp "$P" checkout -q main
run_hook session-start "$P" "$(j_session "$P" SessionStart)"
assert_has "context: stale active-work on another branch → none" "$OUT" "Active work: none"
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
    assert_rc "no-jq: test lock blocked" 2
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
    activate "$P" planned
    run_hook guard-edit "$P" "$(j_edit "$P" "$P/src/app.py" "x = 1")" PATH="$NOJQ"
    assert_rc "no-jq: planned doc lets the edit through" 0
    run_hook guard-bash "$P" "$(j_bash "$P" "rm tests/x_test.py")" PATH="$NOJQ"
    assert_rc "no-jq: shell test lock blocks rm" 2
    run_hook guard-bash "$P" "$(j_bash "$P" "echo x > tests/new_test.py")" PATH="$NOJQ"
    assert_rc "no-jq: shell redirect to a new test path allowed" 0
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

# ======================================================================
echo "-- timing"
assert_true "every hook run completed in < 1s, the python fallback in < 2.5s (slowest: $MAX_MS_HOOK ${MAX_MS}ms)" "$([ -z "$SLOW" ] && echo true || echo false)" "slow runs:$SLOW"

echo "== $PASS passed, $FAIL failed (slowest hook run: $MAX_MS_HOOK ${MAX_MS}ms)"
[ "$FAIL" -eq 0 ]
