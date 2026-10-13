#!/bin/bash
# sdlc plugin — helpers used by scripts/hooks/*.sh and scripts/secret-scan.sh.
# Source AFTER lib/common.sh:
#   . "$(dirname "$0")/../lib/common.sh"; . "$(dirname "$0")/../lib/hooks-extra.sh"
# bash 3.2 compatible (no associative arrays, no mapfile, no ${var,,}). Needs only jq OR python3.

# ---------- plugin location ----------
sdlc_plugin_root() {
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then printf '%s' "$CLAUDE_PLUGIN_ROOT"; return; fi
  printf '%s' "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
}
sdlc_plugin_version() {
  local v; v=$(json_file_get "$(sdlc_plugin_root)/.claude-plugin/plugin.json" .version)
  printf '%s' "${v:-unknown}"
}

# ---------- JSON helpers common.sh lacks ----------
# json_get_raw <json> <path>: like json_get, but a JSON `false` comes back as "false".
# (json_get uses jq's `//`, which treats false like null, so `cfg root .x true` can never observe x=false.)
json_get_raw() {
  local json="$1" path="$2"
  if has_cmd jq; then
    printf '%s' "$json" | jq -r "$path | if . == null then empty elif type == \"string\" then . else tojson end" 2>/dev/null
  else
    json_get "$json" "$path"   # the python fallback already prints false as "false"
  fi
}

# json_lines <json> <path>: array elements one per line (strings raw, other values as JSON); a scalar prints as one line.
json_lines() {
  local json="$1" path="$2"
  if has_cmd jq; then
    printf '%s' "$json" | jq -r "$path // empty | if type == \"array\" then .[] else . end | if type == \"string\" then . else tojson end" 2>/dev/null
  else
    SDLC_JSON="$json" python3 - "$path" <<'PY' 2>/dev/null
import json,os,sys
def walk(cur,path):
    for part in [p for p in path.strip('.').replace('[','.').replace(']','').split('.') if p]:
        if isinstance(cur,list):
            try: cur=cur[int(part)]
            except Exception: return None
        elif isinstance(cur,dict): cur=cur.get(part)
        else: return None
        if cur is None: return None
    return cur
try: cur=walk(json.loads(os.environ.get('SDLC_JSON') or 'null'),sys.argv[1])
except Exception: cur=None
items=cur if isinstance(cur,list) else ([] if cur is None else [cur])
for x in items:
    print(x if isinstance(x,str) else json.dumps(x))
PY
  fi
}

# ---------- config readers (same file as cfg(), but false-safe and array-aware) ----------
_cfg_json() { _cfg_text "$1"; }
# cfg_raw <root> <path> [default]
cfg_raw()  { local t v=""; t=$(_cfg_json "$1"); if cfg_mentions "$t" "$2"; then v=$(json_get_raw "$t" "$2"); fi; printf '%s' "${v:-${3:-}}"; }
# cfg_bool <root> <path> [default]  → prints true|false
cfg_bool() {
  local v; v=$(cfg_raw "$1" "$2" "${3:-false}")
  case "$v" in true|1|yes|on) printf 'true';; *) printf 'false';; esac
}
# cfg_lines <root> <path>  → array elements one per line
cfg_lines() { local t; t=$(_cfg_json "$1"); if cfg_mentions "$t" "$2"; then json_lines "$t" "$2"; fi; }

# ---------- verify command resolution ----------
# verify_commands <root> → lines "name<TAB>command" for verify.commands (default: test).
# Names test|build|lint|run resolve through commands.<name>; anything else is a literal command.
verify_commands() {
  local root="$1" names name cmd
  names=$(cfg_lines "$root" .verify.commands)
  [ -z "$names" ] && names="test"
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    case "$name" in
      test|build|lint|run) cmd=$(cfg "$root" ".commands.$name" "") ;;
      *) cmd="$name" ;;
    esac
    printf '%s\t%s\n' "$name" "$cmd"
  done <<< "$names"
}
# verify_commands_human <root> → "test: npm test | build: (not set)"
verify_commands_human() {
  local out="" name cmd
  while IFS=$'\t' read -r name cmd; do
    [ -z "$name" ] && continue
    [ -z "$cmd" ] && cmd="(not set — configure commands.$name in .sdlc/config.json)"
    out="${out:+$out | }$name: $cmd"
  done <<< "$(verify_commands "$1")"
  printf '%s' "$out"
}

# ---------- test lock (guard-edit, guard-bash) ----------
# During sdlc work — an active work doc applies: .sdlc/state/active-work names a slug and the branch is sdlc/<slug> — an
# existing test is locked: it may be neither changed nor deleted. Outside sdlc work nothing is locked: there is no gate behind
# the lock there, and the agent could switch it off in .sdlc/config.json anyway. Existing = already on the default branch, as
# of the point where this change branched off it — the engine's merge gate measures the same way. A test file this change adds
# is free to edit while it is being written (the gate only checks it for skip/only); a test file that does not exist yet may
# always be created. Without a git base to compare with, every test file that exists counts as existing.
# Unlocked when protect.protect_tests is off, SDLC_ALLOW_TEST_EDIT=1 (a person sets it in the environment Claude Code starts
# with — a tool call cannot set it for a hook), or the active work doc says `test_changes: allowed` (the design decides that,
# per change). There is no unlock file: the agent could create one itself.
SDLC_TEST_PATH_ARR=(auto); SDLC_TEST_WORK_DIR="work"; SDLC_TEST_BASE=""

# The built-in test-path set ("auto") is ONE definition with the merge gate. The first two patterns are the gate's own, character
# for character (TEST_DIR_RE and TEST_FILE_RE in scripts/sdlclib/gate.py); the third is its inline spec/ rule. tests/hooks.sh
# compares the patterns with the gate's, then compares the verdicts over a corpus of paths. Change the gate and these together.
# The gate never counts anything under the work dir (SDLC_TEST_WORK_DIR).
SDLC_TEST_DIR_RE='(^|/)(test|tests|__tests__|androidTest|testFixtures)/'
SDLC_TEST_FILE_RE='(_test\.go|\.test\.[A-Za-z]+|\.spec\.[A-Za-z]+|(^|/)test_[^/]*\.py|_test\.py|Tests?\.(java|kt|swift)|Spec\.(scala|kt)|_spec\.rb)$'
SDLC_TEST_SPEC_RE='(^|/)spec/'

# auto_test_path <rel> → 0 when <rel> is a test path of the built-in set; mirrors is_test_path() in gate.py for the "auto" entry
auto_test_path() {
  local LC_ALL=C rel="$1"                        # bytes, not characters: a legacy-encoded file name is a name like any other
  if [ -n "$SDLC_TEST_WORK_DIR" ]; then case "$rel" in "$SDLC_TEST_WORK_DIR"/*) return 1;; esac; fi
  [[ "$rel" =~ $SDLC_TEST_DIR_RE ]] && return 0
  [[ "$rel" =~ $SDLC_TEST_FILE_RE ]] && return 0
  [[ "$rel" =~ $SDLC_TEST_SPEC_RE ]] && return 0
  return 1
}

# test_paths_load <root> → SDLC_TEST_PATH_ARR (one pattern per element) and SDLC_TEST_WORK_DIR (paths.work without slashes,
# like the gate's cfg.rel("work")). A config that does not set protect.test_paths means "auto" (a version-2 file only holds
# what differs from the defaults); an explicit [] means none.
test_paths_load() {
  local lines pat w
  SDLC_TEST_PATH_ARR=()
  lines=$(cfg_lines "$1" .protect.test_paths)
  if [ -z "$lines" ] && [ -z "$(cfg_raw "$1" .protect.test_paths)" ]; then lines="auto"; fi
  while IFS= read -r pat; do
    pat=$(trim "$pat"); [ -n "$pat" ] && SDLC_TEST_PATH_ARR[${#SDLC_TEST_PATH_ARR[@]}]="$pat"
  done <<< "$lines"
  w=$(work_dir "$1")
  while [ "${w#/}" != "$w" ]; do w="${w#/}"; done
  while [ "${w%/}" != "$w" ]; do w="${w%/}"; done
  SDLC_TEST_WORK_DIR="$w"
  return 0
}
# is_test_path <rel> → 0 when the path matches the loaded test paths (cheap: no external command, no here-string). An explicit
# pattern is read by glob_match (lib/common.sh), the same glob rules as the gate's.
is_test_path() {
  local i=0 n=${#SDLC_TEST_PATH_ARR[@]} pat
  while [ "$i" -lt "$n" ]; do
    pat="${SDLC_TEST_PATH_ARR[$i]}"; i=$((i + 1))
    if [ "$pat" = "auto" ]; then auto_test_path "$1" && return 0
    else glob_match "$pat" "$1" && return 0; fi
  done
  return 1
}
# test_edit_allowed <root> → 0 when one of the unlocks is set: SDLC_ALLOW_TEST_EDIT=1 in the environment, or the active work
# doc's `test_changes: allowed`
test_edit_allowed() {
  [ "${SDLC_ALLOW_TEST_EDIT:-}" = "1" ] && return 0
  local doc; doc=$(active_work_file "$1")
  [ -n "$doc" ] && [ "$(frontmatter_get "$1/$doc" test_changes)" = "allowed" ] && return 0
  return 1
}
# test_lock_on <root> → 0 when existing test files are locked right now: during sdlc work (an active work doc applies, see
# active_work_slug — a missing doc file still counts: it must not be a way out) and not unlocked
test_lock_on() {
  [ "$(cfg_bool "$1" .protect.protect_tests true)" = "true" ] || return 1
  [ -n "$(active_work_slug "$1")" ] || return 1
  test_edit_allowed "$1" && return 1
  return 0
}
# test_base_rev <root> → SDLC_TEST_BASE (cached for the run): the commit existing tests are measured against — where HEAD
# branched off the default branch, which is origin/<protect.default_branch> when that exists, else the local branch. "-" when
# there is no such commit (not a repository, no commits, no default branch).
test_base_rev() {
  [ -n "$SDLC_TEST_BASE" ] && return 0
  local def ref b
  SDLC_TEST_BASE="-"
  def=$(cfg "$1" .protect.default_branch main)
  case "$def" in -*) def=main;; esac                       # never hand git something that reads as an option
  for ref in "origin/$def" "$def"; do
    git -C "$1" rev-parse --verify --quiet "$ref^{commit}" >/dev/null 2>&1 || continue
    b=$(git -C "$1" merge-base HEAD "$ref" 2>/dev/null) || b=""
    SDLC_TEST_BASE="${b:-$ref}"; return 0
  done
  return 0
}
# test_file_is_new <root> <rel> → 0 when the file is not in the base: a test this change adds
test_file_is_new() {
  test_base_rev "$1"
  [ "$SDLC_TEST_BASE" != "-" ] || return 1
  git -C "$1" cat-file -e "$SDLC_TEST_BASE:./$2" 2>/dev/null && return 1
  return 0
}
# test_file_locked <root> <rel> <path> → 0 when <path> is an existing test file and existing tests are locked now
test_file_locked() {
  case "$3" in /*) ;; *) set -- "$1" "$2" "$1/$3";; esac
  [ -e "$3" ] || [ -L "$3" ] || return 1      # a test file that does not exist yet may always be created
  test_paths_load "$1"; is_test_path "$2" || return 1
  test_lock_on "$1" || return 1
  test_file_is_new "$1" "$2" && return 1      # a test this change adds is not an existing one
  return 0
}

# ---------- production deploy gate (guard-bash) ----------
# One tier. A command matching a deploy pattern needs the approval env var (default RELEASE_APPROVAL). guard-bash matches the
# patterns against each simple command of the command line, not against the whole text (see deploy_hit there).
# Default patterns — keep identical to DEPLOY_PATTERNS in scripts/sdlclib/config.py (tests/hooks.sh compares them).
SDLC_DEFAULT_DEPLOY_PATTERNS='(deploy|release|rollout|promote)[^|;&]*(prod|production|live)
(prod|production|live)[^|;&]*(deploy|release|rollout|promote)
kubectl [^|;&]*(apply|rollout|set image)[^|;&]*(prod|production)
helm (upgrade|install)[^|;&]*(prod|production)
terraform apply[^|;&]*(prod|production)'
SDLC_DEPLOY_PATTERNS="$SDLC_DEFAULT_DEPLOY_PATTERNS"; SDLC_DEPLOY_ENV=RELEASE_APPROVAL

# valid_ident <name> → 0 when it is usable as a shell variable name (indirect expansion of anything else is an error)
valid_ident() { case "$1" in ''|[0-9]*|*[!A-Za-z0-9_]*) return 1;; esac; return 0; }

# deploy_gate_config <root> → SDLC_DEPLOY_PATTERNS (one per line) and SDLC_DEPLOY_ENV. Source order: protect.deploy_patterns,
# a version-1 config's environments.production.patterns, the default list; an explicit empty list switches the gate off.
deploy_gate_config() {
  local cj raw name
  SDLC_DEPLOY_PATTERNS="$SDLC_DEFAULT_DEPLOY_PATTERNS"; SDLC_DEPLOY_ENV=RELEASE_APPROVAL
  sdlc_initialized "$1" || return 0
  cj=$(_cfg_json "$1")
  case "$cj" in *deploy_patterns*|*deploy_approval_env*|*production*) ;; *) return 0;; esac   # nothing to read: the defaults stand
  raw=$(json_get_raw "$cj" .protect.deploy_patterns)
  if [ -n "$raw" ]; then
    SDLC_DEPLOY_PATTERNS=$(json_lines "$cj" .protect.deploy_patterns)
  else
    raw=$(json_get_raw "$cj" .environments.production.patterns)
    if [ -n "$raw" ]; then SDLC_DEPLOY_PATTERNS=$(json_lines "$cj" .environments.production.patterns); fi
  fi
  name=$(json_get "$cj" .protect.deploy_approval_env)
  [ -z "$name" ] && name=$(json_get "$cj" .environments.production.approval_env)
  if valid_ident "$name"; then SDLC_DEPLOY_ENV="$name"; fi
  return 0
}
# deploy_match <text> → prints the first deploy pattern a line of the text matches and returns 0; returns 1 on no match
# (guard-bash passes one line per simple command, so a pattern never spans two commands)
deploy_match() {
  local cmd="$1" pat rc
  [ -n "$SDLC_DEPLOY_PATTERNS" ] || return 1
  set --
  while IFS= read -r pat; do
    if [ -n "$pat" ]; then set -- "$@" -e "$pat"; fi
  done <<< "$SDLC_DEPLOY_PATTERNS"
  [ $# -gt 0 ] || return 1
  printf '%s\n' "$cmd" | grep -E -i -q "$@" 2>/dev/null; rc=$?
  [ "$rc" = 1 ] && return 1                      # clean miss — the common case costs one grep
  while IFS= read -r pat; do                     # a hit, or a pattern grep cannot compile: one by one, so a bad one never disables the rest
    [ -z "$pat" ] && continue
    if printf '%s\n' "$cmd" | grep -E -i -q -e "$pat" 2>/dev/null; then printf '%s' "$pat"; return 0; fi
  done <<< "$SDLC_DEPLOY_PATTERNS"
  return 1
}

# ---------- tool payload ----------
# edit_new_text <hook-json> → the text an Edit/Write/MultiEdit/NotebookEdit call would put into the file
edit_new_text() {
  local json="$1"
  if has_cmd jq; then
    printf '%s' "$json" | jq -r '[.tool_input.new_string?, .tool_input.content?, .tool_input.new_source?] + [(.tool_input.edits // [])[]? | .new_string?] | map(select(type == "string")) | .[]' 2>/dev/null
  else
    SDLC_JSON="$json" python3 - <<'PY' 2>/dev/null
import json,os
try: d=json.loads(os.environ.get('SDLC_JSON') or 'null')
except Exception: d=None
ti=((d or {}).get('tool_input') or {}) if isinstance(d,dict) else {}
out=[ti.get(k) for k in ('new_string','content','new_source')]
for e in (ti.get('edits') or []):
    if isinstance(e,dict): out.append(e.get('new_string'))
for s in out:
    if isinstance(s,str): print(s)
PY
  fi
}

# ---------- strings & time ----------
trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }
one_line() { printf '%s' "$1" | tr '\n\r\t' '   '; }
# trunc <string> [max=120]
trunc() { local s="$1" n="${2:-120}"; if [ "${#s}" -gt "$n" ]; then printf '%s...' "${s:0:$n}"; else printf '%s' "$s"; fi; }
epoch_now() { date +%s; }
is_int() { case "$1" in ''|*[!0-9-]*) return 1;; *) return 0;; esac; }

# ---------- session state (batch) ----------
# state_set_many <root> <key> <value> [<key> <value> ...] — one rewrite of session-<id>.json
state_set_many() {
  local root="$1"; shift
  [ $# -ge 2 ] || return 0
  local f; f=$(session_state_file "$root")
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  local cur='{}'; [ -f "$f" ] && cur=$(cat "$f")
  if has_cmd jq; then
    local args=() filter="." i=0
    while [ $# -ge 2 ]; do
      args+=(--arg "k$i" "$1" --arg "v$i" "$2"); filter="$filter | .[\$k$i]=\$v$i"; i=$((i+1)); shift 2
    done
    printf '%s' "$cur" | jq "${args[@]}" "$filter" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
  else
    python3 - "$f" "$@" <<'PY' 2>/dev/null
import json,sys
f=sys.argv[1]; kv=sys.argv[2:]
try: d=json.load(open(f))
except Exception: d={}
for i in range(0,len(kv)-1,2): d[kv[i]]=kv[i+1]
json.dump(d,open(f,'w'))
PY
  fi
  return 0
}

# ---------- bounded execution (macOS has no `timeout`) ----------
# run_capped <seconds> <cmd> [args...] — returns the command's exit code, 124 when killed on timeout
run_capped() {
  local secs="$1"; shift
  "$@" &
  local pid=$! ticks=0 max=$((secs * 10))
  while kill -0 "$pid" 2>/dev/null; do
    ticks=$((ticks + 1))
    if [ "$ticks" -ge "$max" ]; then
      kill "$pid" 2>/dev/null; sleep 0.2; kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 0.1
  done
  wait "$pid"
}
