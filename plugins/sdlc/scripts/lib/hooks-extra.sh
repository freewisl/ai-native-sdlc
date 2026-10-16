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
    printf '%s' "$json" | python3 -c '
import json,sys
def walk(cur,path):
    for part in [p for p in path.strip(".").replace("[",".").replace("]","").split(".") if p]:
        if isinstance(cur,list):
            try: cur=cur[int(part)]
            except Exception: return None
        elif isinstance(cur,dict): cur=cur.get(part)
        else: return None
        if cur is None: return None
    return cur
try: cur=walk(json.loads(sys.stdin.read() or "null"),sys.argv[1])
except Exception: cur=None
items=cur if isinstance(cur,list) else ([] if cur is None else [cur])
for x in items:
    print(x if isinstance(x,str) else json.dumps(x))
' "$path" 2>/dev/null
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
# for character (TEST_DIR_RE and TEST_FILE_RE in scripts/sdlclib/gate.py); the third is its inline spec/ rule; the fourth is
# TEST_CONFIG_GLOBS of the gate written as a regular expression. tests/hooks.sh compares the first two with the gate's, then compares
# the verdicts over a corpus of paths. Change the gate and these together.
# A test runner's configuration file counts as a test file: conftest.py (any depth), pytest.ini, jest/vitest/playwright/cypress.config.*,
# vitest.workspace.*, karma.conf.*, .mocharc[.*], phpunit.xml[.dist] — changing one changes what the tests check.
# The gate never counts anything under the work dir (SDLC_TEST_WORK_DIR).
SDLC_TEST_DIR_RE='(^|/)(test|tests|__tests__|androidTest|testFixtures)/'
SDLC_TEST_FILE_RE='(_test\.go|\.test\.[A-Za-z]+|\.spec\.[A-Za-z]+|(^|/)test_[^/]*\.py|_test\.py|Tests?\.(java|kt|swift)|Spec\.(scala|kt)|_spec\.rb)$'
SDLC_TEST_CONFIG_RE='(^|/)(conftest\.py|pytest\.ini|(jest|vitest|playwright|cypress)\.config\.[^/]+|vitest\.workspace\.[^/]+|karma\.conf\.[^/]+|\.mocharc(\.[^/]+)?|phpunit\.xml(\.dist)?)$'
SDLC_TEST_SPEC_RE='(^|/)spec/'

# auto_test_path <rel> → 0 when <rel> is a test path of the built-in set; mirrors is_test_path() in gate.py for the "auto" entry
auto_test_path() {
  local LC_ALL=C rel="$1"                        # bytes, not characters: a legacy-encoded file name is a name like any other
  if [ -n "$SDLC_TEST_WORK_DIR" ]; then case "$rel" in "$SDLC_TEST_WORK_DIR"/*) return 1;; esac; fi
  [[ "$rel" =~ $SDLC_TEST_DIR_RE ]] && return 0
  [[ "$rel" =~ $SDLC_TEST_FILE_RE ]] && return 0
  [[ "$rel" =~ $SDLC_TEST_CONFIG_RE ]] && return 0
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
# branched off the default branch, which is origin/<protect.default_branch> when that exists, else the local branch; with
# loop.forge "local" only the local branch (the merge gate measures the same way — gitops.make_forge sets prefer_local: a local
# forge merges there, whatever origin has). "-" when there is no such commit (not a repository, no commits, no default branch).
test_base_rev() {
  [ -n "$SDLC_TEST_BASE" ] && return 0
  local def ref b forge
  SDLC_TEST_BASE="-"
  def=$(cfg "$1" .protect.default_branch main)
  case "$def" in -*) def=main;; esac                       # never hand git something that reads as an option
  forge=$(cfg "$1" .loop.forge auto)
  for ref in "origin/$def" "$def"; do
    [ "$forge" = local ] && [ "$ref" != "$def" ] && continue   # a local forge: origin's copy of the branch is not the base
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
# What the gate matches while the patterns are the defaults: the same five, with prod, production and live as whole words (`kubectl get deploy
# -n delivery` has no `live` in it, `release the products` no `prod`) — a digit, a dash, a slash or a colon around them still counts, and so does
# the trigger word directly in front of them (`deployProd`) — plus the one deploy that carries neither word, `vercel ... --prod`.
SDLC_WORD_DEPLOY_PATTERNS='(deploy|release|rollout|promote)([^|;&]*[^[:alpha:]])?(prod|production|live)([^[:alpha:]]|$)
(^|[^[:alpha:]])(prod|production|live)([^[:alpha:]][^|;&]*)?(deploy|release|rollout|promote)
kubectl [^|;&]*(apply|rollout|set image)[^|;&]*[^[:alpha:]](prod|production)([^[:alpha:]]|$)
helm (upgrade|install)[^|;&]*[^[:alpha:]](prod|production)([^[:alpha:]]|$)
terraform apply[^|;&]*[^[:alpha:]](prod|production)([^[:alpha:]]|$)
vercel[^|;&]*--prod(uction)?([^[:alpha:]]|$)'
SDLC_DEPLOY_PATTERNS="$SDLC_WORD_DEPLOY_PATTERNS"; SDLC_DEPLOY_ENV=RELEASE_APPROVAL

# valid_ident <name> → 0 when it is usable as a shell variable name (indirect expansion of anything else is an error)
valid_ident() { case "$1" in ''|[0-9]*|*[!A-Za-z0-9_]*) return 1;; esac; return 0; }

# deploy_gate_config <root> → SDLC_DEPLOY_PATTERNS (one per line) and SDLC_DEPLOY_ENV. Source order: protect.deploy_patterns,
# a version-1 config's environments.production.patterns, the default list; an explicit empty list switches the gate off. The default list is
# matched as SDLC_WORD_DEPLOY_PATTERNS; a list the config names is matched as written.
deploy_gate_config() {
  local cj raw name
  SDLC_DEPLOY_PATTERNS="$SDLC_WORD_DEPLOY_PATTERNS"; SDLC_DEPLOY_ENV=RELEASE_APPROVAL
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
  [ "$SDLC_DEPLOY_PATTERNS" != "$SDLC_DEFAULT_DEPLOY_PATTERNS" ] || SDLC_DEPLOY_PATTERNS="$SDLC_WORD_DEPLOY_PATTERNS"   # the defaults, written out by hand
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

# ---------- the command line as simple commands (guard-bash, post-bash) ----------
# The command line as simple commands — (1) and (3) both read it through one tokenizer (awk) that turns it into records: W<word>
# (quotes and escapes resolved), O<path> (the target of an output redirection) and C (end of a simple command: ; & | && || newline
# ( ) `) — heredoc bodies and comments skipped. Only (1) asks for more (awk -v bodies=1 -v cap=<n>): P (the command's output is
# piped on), R<file> (its input is a here-string — no file — or the file), N<kind> followed by one record of text, newlines as
# \001 — kind X: a command substitution inside double quotes or inside an unquoted heredoc body (a command of its own), kind B<n>:
# the body of a heredoc whose command, simple command number n, names a shell — and ! (more than <n> records: stopped there).
# Plain text, white space and quoted text are read a chunk at a time: substr in macOS awk measures the whole command line on every
# call, so a character-by-character scan is quadratic in its length. awk runs as LC_ALL=C — it reads bytes, whatever the locale: in a
# UTF-8 locale macOS awk stops dead on a regular expression over half a character (a chunk that ends inside one, the text after a
# backslash that escapes its first byte), and everything after that point is never seen.
TL_AWK='
BEGIN {
  RS = "\001"; SQ = "\047"; DQ = "\042"; BQ = "\140"; DOL = "\044"; cl = 0
  PLAINRE = "^[^ \t\r\n;|&()<>" BQ DQ SQ "\\\\]+"                 # a run of characters that need no attention (not stop, quote or backslash)
  DQRE = bodies ? "[\"\\\\$`]" : "[\"\\\\]"
  if (bodies) { m = split(shells, a, " "); for (k = 1; k <= m; k++) SH[a[k]] = 1 }
}
function stop(c) {
  return c == " " || c == "\t" || c == "\r" || c == "\n" || c == ";" || c == "|" || c == "&" || c == "(" || c == ")" || c == "<" || c == ">" || c == "`"
}
function tick() { if (cap && ++nrec > cap) { print "!"; exit } }                  # (bodies only) too many records: the caller matches the text instead
function cend() { tick(); print "C"; cl++ }
function nest(kind, x) { tick(); gsub(/\n/, "\001", x); print "N" kind; print x }
function pclose(t, p,   nt, dep, rest, j, c) {              # the ) that closes a $( — p is just past the $(
  nt = length(t); dep = 1
  while (p <= nt) {
    rest = substr(t, p, 256)
    j = match(rest, /[()\\]/)
    if (j == 0) { p += length(rest); continue }
    p += j - 1
    c = substr(t, p, 1)
    if (c == "\\") { p += 2; continue }
    if (c == "(") dep++
    else if (--dep == 0) return p
    p++
  }
  return nt + 1
}
function bclose(t, p,   nt, rest, j) {                       # the backtick that closes one — p is just past the opening one
  nt = length(t)
  while (p <= nt) {
    rest = substr(t, p, 256)
    j = match(rest, /[`\\]/)
    if (j == 0) { p += length(rest); continue }
    p += j - 1
    if (substr(t, p, 1) == "\\") { p += 2; continue }
    return p
  }
  return nt + 1
}
function substs(t,   p, nt, rest, j, c, q) {                 # the command substitutions an unquoted heredoc body expands
  if (index(t, "$(") == 0 && index(t, BQ) == 0) return
  nt = length(t); p = 1
  while (p <= nt) {
    rest = substr(t, p, 256)
    j = match(rest, /[\\$`]/)
    if (j == 0) { p += length(rest); continue }
    p += j - 1
    c = substr(t, p, 1)
    if (c == "\\") { p += 2; continue }
    if (c == DOL) {
      if (substr(t, p + 1, 1) != "(") { p++; continue }
      q = pclose(t, p + 2); nest("X", substr(t, p + 2, q - p - 2)); p = q + 1
    } else {
      q = bclose(t, p + 1); nest("X", substr(t, p + 1, q - p - 1)); p = q + 1
    }
  }
}
function readword(   w, c, d, e, rest, j, q, k0) {
  w = ""
  while (i <= n) {
    rest = substr(s, i, 256)
    if (match(rest, PLAINRE)) { w = w substr(rest, 1, RLENGTH); i += RLENGTH; continue }
    c = substr(rest, 1, 1)
    if (stop(c)) break
    if (c == "\\") {
      d = substr(s, i + 1, 1)
      if (d == "\n") { i += 2; continue }
      w = w d; i += 2; continue
    }
    if (c == SQ) {
      i++
      rest = substr(s, i); j = index(rest, SQ)
      if (j == 0) { w = w rest; i = n + 1 } else { w = w substr(rest, 1, j - 1); i += j }
      continue
    }
    if (c == DQ) {
      i++
      while (i <= n) {
        rest = substr(s, i, 256)                        # plain text is taken a chunk at a time
        j = match(rest, DQRE)
        if (j == 0) { w = w rest; i += length(rest); continue }
        w = w substr(rest, 1, j - 1); i += j - 1
        d = substr(s, i, 1)
        if (d == DQ) break
        if (d == DOL || d == BQ) {                      # (bodies only) a command substitution inside the quotes is a command of its own
          if (d == BQ) { k0 = i + 1; q = bclose(s, k0) }
          else if (substr(s, i + 1, 1) == "(") { k0 = i + 2; q = pclose(s, k0) }
          else { w = w d; i++; continue }
          nest("X", substr(s, k0, q - k0))
          w = w substr(s, i, q - i + 1); i = q + 1; continue
        }
        e = substr(s, i + 1, 1)
        if (e == "\n") { i += 2; continue }
        if (e == "$" || e == "`" || e == DQ || e == "\\") { w = w e; i += 2; continue }
        w = w d; i++
      }
      i++; continue
    }
    w = w c; i++
  }
  gsub(/\n/, " ", w)
  return w
}
function skipws(   rest) {                                  # a run of white space, a chunk at a time
  while (i <= n) {
    rest = substr(s, i, 256)
    if (!match(rest, /^[ \t\r]+/)) return
    i += RLENGTH
  }
}
function emit(k, b) {                                       # (bodies only) a heredoc body: a script when its command is a shell, plus the $( ) of an unquoted one
  if (!bodies) return
  if (shw[hown[k]]) nest("B" hown[k], b)
  if (!hq[k]) substs(b)
}
function skipbodies(   k, d, wrapped, j, b, from, rs, rl) {
  for (k = 1; k <= nh; k++) {
    if (i > n) break
    d = hd[k]; from = i
    wrapped = "\n" substr(s, i) "\n"
    if (!hdash[k]) {
      j = index(wrapped, "\n" d "\n")
      if (j > 0) { emit(k, substr(s, from, j - 1)); i = i + j + length(d); continue }
    } else if (d ~ /^[A-Za-z0-9_]+$/) {
      if (match(wrapped, "\n\t*" d "\n")) { rs = RSTART; rl = RLENGTH; emit(k, substr(s, from, rs - 1)); i = i + rs + rl - 2; continue }
    } else {
      b = ""
      while (i <= n) {
        j = index(substr(s, i), "\n")
        if (j == 0) { wrapped = substr(s, i); i = n + 1 } else { wrapped = substr(s, i, j - 1); i += j }
        sub(/^\t+/, "", wrapped)
        if (wrapped == d) break
        b = b wrapped "\n"
      }
      emit(k, b)
      continue
    }
    emit(k, substr(s, from)); i = n + 1
  }
  nh = 0
}
{
  s = $0; n = length(s); i = 1; nh = 0
  while (i <= n) {
    rest = substr(s, i, 256)
    if (match(rest, /^[ \t\r]+/)) { i += RLENGTH; continue }
    c = substr(rest, 1, 1)
    if (c == "\n") { i++; cend(); if (nh > 0) skipbodies(); continue }
    if (c == "#") { j = index(substr(s, i), "\n"); if (j == 0) i = n + 1; else i += j - 1; continue }
    if (c == ";" || c == "(" || c == ")" || c == "`") { i++; cend(); continue }
    if (c == "|") {
      i++; d = substr(s, i, 1)
      if (d == "|") i++
      else { if (d == "&") i++; if (bodies) print "P" }
      cend(); continue
    }
    if (c == "&") {
      d = substr(s, i + 1, 1)
      if (d == ">") {
        i += 2; if (substr(s, i, 1) == ">") i++
        skipws(); t = readword(); print "O" t; continue
      }
      i++; if (d == "&") i++
      cend(); continue
    }
    if (c == ">") {
      i++; d = substr(s, i, 1)
      if (d == ">" || d == "|") i++
      else if (d == "&") {
        e = substr(s, i + 1, 1)
        if (e ~ /[0-9-]/) { i++; while (i <= n && substr(s, i, 1) ~ /[0-9-]/) i++; continue }
        i++
      }
      else if (d == "(") continue
      skipws(); t = readword(); print "O" t; continue
    }
    if (c == "<") {
      i++; d = substr(s, i, 1)
      if (d == "<") {
        i++
        if (substr(s, i, 1) == "<") { i++; skipws(); t = readword(); if (bodies) print "R"; continue }
        dash = 0; if (substr(s, i, 1) == "-") { dash = 1; i++ }
        skipws(); hp = i; t = readword()
        nh++; hd[nh] = t; hdash[nh] = dash; hown[nh] = cl
        q = substr(s, hp, i - hp); hq[nh] = (index(q, DQ) > 0 || index(q, SQ) > 0 || index(q, "\\") > 0)
        continue
      }
      if (d == "(") continue
      if (d == "&") i++
      skipws(); t = readword(); if (bodies && d != "&") print "R" t; continue
    }
    p = i
    w = readword()
    if (i == p) { i++; continue }
    d = substr(s, i, 1)
    if (w ~ /^[0-9]+$/ && (d == ">" || d == "<")) continue
    if (bodies) { b = w; sub(/^.*\//, "", b); if (b in SH) shw[cl] = 1 }
    tick(); print "W" w
  }
}
'

# tl_cmdword → TL_K: the index in W[] of the command word — past VAR=value prefixes, shell keywords and wrappers (sudo, env, time,
# nice … — also with a path) and their options; ${#W[@]} when the simple command has none
tl_cmdword() {
  local nw=${#W[@]} w b wrap=0 tool=""
  TL_K=0
  while [ "$TL_K" -lt "$nw" ]; do
    w="${W[$TL_K]}"
    case "$w" in
      -*) [ "$wrap" -ge 1 ] || break
          case "$tool" in                        # an option that takes the next word as its argument (-u root, -n 5 …)
            sudo) case "$w" in -[ughpCrtTUDR]|--user|--group|--host|--prompt|--close-from|--role|--type|--command-timeout|--other-user|--chdir|--chroot) TL_K=$((TL_K + 1));; esac;;
            doas) case "$w" in -[uC]) TL_K=$((TL_K + 1));; esac;;
            env) case "$w" in -[uCS]|--unset|--chdir|--split-string) TL_K=$((TL_K + 1));; esac;;
            nice) case "$w" in -n|--adjustment) TL_K=$((TL_K + 1));; esac;;
            stdbuf) case "$w" in -[ioe]|--input|--output|--error) TL_K=$((TL_K + 1));; esac;;
            timeout) case "$w" in -[sk]|--signal|--kill-after) TL_K=$((TL_K + 1));; esac;;
            time) case "$w" in -[fo]|--format|--output) TL_K=$((TL_K + 1));; esac;;
            exec) case "$w" in -a) TL_K=$((TL_K + 1));; esac;;
          esac;;
      *=*) valid_ident "${w%%=*}" || break;;
      '!'|'{'|'}'|if|then|else|elif|fi|do|done|while|until) wrap=0;;
      *) b="${w##*/}"
         case "$b" in
           time|command|builtin|exec|nohup|sudo|doas|env|nice|setsid|stdbuf) wrap=1; tool="$b";;
           timeout) wrap=2; tool="$b";;
           *) if [ "$wrap" = 2 ]; then wrap=1; else break; fi;;
         esac;;
    esac
    TL_K=$((TL_K + 1))
  done
}

# shell_reads_stdin → 0 when the shell in W[] (its name at TL_K: sh bash zsh dash ksh, or ssh) takes its commands from standard
# input: no script operand and no -c (`… | bash`), -s, or - as an operand; for ssh a host and no remote command. `bash script.sh`,
# `bash -c 'cmd'` read the script or the command they were given — what is piped in is that program's input, not a command.
shell_reads_stdin() {
  local k=$((TL_K + 1)) nw=${#W[@]} a ops=0 sflag=0 cflag=0 ssh=0
  case "${W[$TL_K]##*/}" in ssh) ssh=1;; esac
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    if [ "$ssh" = 1 ]; then
      case "$a" in
        -[pilofFJLRDbcEeImOSWwBQ]) k=$((k + 1));;
        -*) ;;
        *) ops=$((ops + 1));;
      esac
      continue
    fi
    case "$a" in
      --) ops=$((ops + nw - k)); break;;
      -) sflag=1;;
      -o|+o|-O|+O|--rcfile|--init-file) k=$((k + 1));;
      --*) ;;
      -*|+*) case "$a" in *c*) cflag=1;; esac; case "$a" in *s*) sflag=1;; esac;;
      *) ops=$((ops + 1));;
    esac
  done
  if [ "$ssh" = 1 ]; then [ "$ops" -le 1 ]; return; fi
  [ "$cflag" = 1 ] && return 1
  [ "$sflag" = 1 ] && return 0
  [ "$ops" = 0 ]
}

# ---------- walking a command line (guard-bash, post-bash) ----------
# walk_scan <command line> <depth> <callback> → 0 as soon as <callback> returns 0 for a simple command of the line, 1 when none does.
# The callback is called as `<callback> <depth>` with the simple command's words in W[] and its output-redirection targets in R[]
# (the variables of walk_scan, seen through bash's dynamic scope). What runs inside the command line is walked too: a command
# substitution inside double quotes, the body of a heredoc that a shell reads (`bash <<EOF`, `bash -s <<EOF`), `bash -c '<cmd>'` and
# `eval <words>` — to a depth of 3 and 16 bodies a command line (one awk each). A nested body that changes the working directory
# (TL_BASE, TL_STACK) leaves it as it was.
WALK_NESTED=0; WALK_MAX_NESTED=16; TL_BASE=""; TL_STACK=""
walk_scan() {
  local text="$1" depth="$2" cb="$3" out rec kind="" cl=0 SHELL_SER=" "
  local -a W=() R=()
  out=$(printf '%s' "$text" | LC_ALL=C awk -v bodies=1 -v shells="sh bash zsh dash ksh" "$TL_AWK" 2>/dev/null)
  while IFS= read -r rec; do
    if [ -n "$kind" ]; then                              # the text record after an N header
      case "$kind" in B*) case "$SHELL_SER" in *" ${kind#B} "*) ;; *) kind=""; continue;; esac;; esac
      kind=""
      walk_nested "$rec" "$depth" "$cb" && return 0
      continue
    fi
    case "$rec" in
      W*) W[${#W[@]}]="${rec#W}";;
      O*) R[${#R[@]}]="${rec#O}";;
      N*) kind="${rec#N}";;
      C) walk_clause "$depth" "$cb" && return 0; W=(); R=(); cl=$((cl + 1));;
    esac
  done <<< "$out"
  walk_clause "$depth" "$cb"
}
# walk_nested <text, newlines as \001> <depth> <callback> — walk_scan of a body, one level deeper
walk_nested() {
  local body="$1" depth="$2" cb="$3" saved="$TL_BASE" saved_stack="$TL_STACK" rc=1
  [ "$depth" -lt 3 ] && [ "$WALK_NESTED" -lt "$WALK_MAX_NESTED" ] || return 1
  WALK_NESTED=$((WALK_NESTED + 1))
  case "$body" in *$'\001'*) body=$(printf '%s' "$body" | tr '\001' '\n');; esac
  if walk_scan "$body" $((depth + 1)) "$cb"; then rc=0; fi
  TL_BASE="$saved"; TL_STACK="$saved_stack"
  return "$rc"
}
# walk_clause <depth> <callback> — the simple command in W[]/R[]: the callback, then what the command runs (a shell's -c argument, eval)
walk_clause() {
  local depth="$1" cb="$2" nw=${#W[@]} k i base str
  [ "$nw" -gt 0 ] || [ "${#R[@]}" -gt 0 ] || return 1
  "$cb" "$depth" && return 0
  [ "$nw" -gt 0 ] || return 1
  tl_cmdword; k=$TL_K
  [ "$k" -lt "$nw" ] || return 1
  base="${W[$k]##*/}"; k=$((k + 1))
  case "$base" in
    bash|sh|zsh|dash|ksh)
      if shell_reads_stdin; then SHELL_SER="$SHELL_SER$cl "; fi       # a heredoc body that follows is its script
      i=$k
      while [ "$i" -lt "$nw" ]; do
        case "${W[$i]}" in
          -c|-[a-z]c|-[a-z][a-z]c)
            i=$((i + 1))
            if [ "$i" -lt "$nw" ]; then walk_nested "${W[$i]}" "$depth" "$cb" && return 0; fi
            break;;
        esac
        i=$((i + 1))
      done;;
    eval) str=""; i=$k
          while [ "$i" -lt "$nw" ]; do str="$str ${W[$i]}"; i=$((i + 1)); done
          if [ -n "$str" ]; then walk_nested "$str" "$depth" "$cb" && return 0; fi;;
  esac
  return 1
}

# ---------- the guards read their input or block ----------
# guard_require_json <payload> — guard-edit and guard-bash: nothing on stdin is nothing to guard (exit 0); a payload that is not one JSON
# object, or no jq or python3 to read it with, is a blocked call — a guard that cannot read what it guards must not wave it through.
guard_require_json() {
  local rc
  [ -n "$(printf '%s' "$1" | head -c 400 | tr -d '[:space:]')" ] || exit 0
  json_ok "$1"; rc=$?
  case "$rc" in
    0) return 0;;
    2) block "the hook payload cannot be read: neither jq nor python3 is installed. Why: the guards fail closed — a call they cannot read is not let through. To proceed: install jq (or python3), or disable the sdlc plugin.";;
    *) block "the hook payload is not valid JSON (the tool call is not checked). Why: the guards fail closed — a call they cannot read is not let through. To proceed: retry the call; if it keeps happening, check that 'jq --version' or 'python3 --version' runs, or disable the sdlc plugin.";;
  esac
}
# sdlc_work_active <root> → 0 while an sdlc item is active: .sdlc/state/active-work names a slug and the branch is sdlc/<slug> (the condition
# of the test lock and of design first). The merge then belongs to the shell gate — a session never merges or pushes to the default branch.
sdlc_work_active() { [ -n "$(active_work_slug "$1")" ]; }

# ---------- tool payload ----------
# edit_new_text <hook-json> → the text an Edit/Write/MultiEdit/NotebookEdit call would put into the file (the payload goes through stdin:
# a Write of a big file is far past what an environment variable can carry)
edit_new_text() {
  local json="$1"
  if has_cmd jq; then
    printf '%s' "$json" | jq -r '[.tool_input.new_string?, .tool_input.content?, .tool_input.new_source?] + [(.tool_input.edits // [])[]? | .new_string?] | map(select(type == "string")) | .[]' 2>/dev/null
  else
    printf '%s' "$json" | python3 -c '
import json,sys
try: d=json.loads(sys.stdin.read() or "null")
except Exception: d=None
ti=((d or {}).get("tool_input") or {}) if isinstance(d,dict) else {}
out=[ti.get(k) for k in ("new_string","content","new_source")]
for e in (ti.get("edits") or []):
    if isinstance(e,dict): out.append(e.get("new_string"))
for s in out:
    if isinstance(s,str): print(s)
' 2>/dev/null
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

# ---------- process identity (session-start) ----------
# A pid number is reused once its process ends; the start time is not. The loop writes `<pid> <start time>` to .sdlc/state/loop.pid
# (sdlclib state.write_pid), and these two mirror sdlclib util.proc_start / util.same_process, one `ps` call each.
# squeeze_ws <text> → the words of <text> joined by single spaces (no globbing, no external command)
squeeze_ws() {
  local w out="" noglob=1
  case $- in *f*) ;; *) noglob=0; set -f;; esac
  for w in $1; do out="${out:+$out }$w"; done
  [ "$noglob" = 1 ] || set +f
  printf '%s' "$out"
}
# proc_start <pid> → the start time as `ps` prints it (LC_ALL=C, runs of white space squeezed to one space); empty when the process is not
# running or `ps` cannot say
proc_start() {
  local s
  s=$(LC_ALL=C ps -o lstart= -p "$1" 2>/dev/null) || return 0
  squeeze_ws "$s"
}
# same_process <pid> <start> → 0 when <pid> is alive and — when <start> (recorded when the process wrote its pid down) is not empty —
# still the process that started then: another start time means the number belongs to a newer process. Without a recorded start, or when
# `ps` cannot say, being alive is all there is to go by.
same_process() {
  local now
  kill -0 "$1" 2>/dev/null || return 1
  [ -n "${2:-}" ] || return 0
  now=$(proc_start "$1")
  [ -z "$now" ] || [ "$now" = "$2" ]
}
