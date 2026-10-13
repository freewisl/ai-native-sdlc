#!/bin/bash
# sdlc plugin — structural tests: manifests, skills, agents, hooks.json, templates, stale 0.x references, shell syntax, secret-scan.sh.
# Run from anywhere: bash tests/scripts.sh [-v]. Prints PASS/FAIL per check — a FAIL line carries its reason, -v adds a detail line to
# every PASS — then "== N passed, M failed", and exits 1 when any check failed. Needs bash 3.2+, git, and jq or python3
# (SDLC_TEST_NO_JQ=1 forces the python3 path). Not covered here: the engine, init and github-setup (tests/py) and the hooks
# (tests/hooks.sh); tests/run.sh runs all of them plus `claude plugin validate`.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; PLUGIN="$(cd "$HERE/.." && pwd)"; SCRIPTS="$PLUGIN/scripts"
REPO="$(cd "$PLUGIN/../.." && pwd)"                        # the marketplace root: plugins/<name>/ sits two levels below it
export CLAUDE_PLUGIN_ROOT="$PLUGIN"
unset CLAUDE_PROJECT_DIR
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null   # the temporary repositories see no user or system git config
WORK=$(mktemp -d "${TMPDIR:-/tmp}/sdlc-scripts.XXXXXX"); trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0; VERBOSE=false; for a in "$@"; do [ "$a" = "-v" ] && VERBOSE=true; done
TAB=$(printf '\t'); US=$(printf '\037')                    # US (unit separator) is not IFS whitespace, so empty fields survive `read`

HAVE_JQ=false; command -v jq >/dev/null 2>&1 && [ -z "${SDLC_TEST_NO_JQ:-}" ] && HAVE_JQ=true
HAVE_PY=false; command -v python3 >/dev/null 2>&1 && HAVE_PY=true
$HAVE_JQ || $HAVE_PY || { echo "tests/scripts.sh needs jq or python3" >&2; exit 2; }

# ---------------------------------------------------------------- helpers
ok()  { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; if $VERBOSE && [ -n "${2:-}" ]; then printf '      %s\n' "$2"; fi; return 0; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n      %s\n' "$1" "$(printf '%s' "${2:-}" | head -c 600 | tr '\n' ' ')"; return 0; }
eq()       { if [ "$2" = "$3" ]; then ok "$1" "${4:-}"; else bad "$1" "got '$2', want '$3'"; fi; }
nonempty() { case "$2" in *[![:space:]]*) ok "$1" "$(printf '%s' "$2" | head -c 100)";; *) bad "$1" "empty or missing";; esac; }
has()      { case "$2" in *"$3"*) ok "$1";; *) bad "$1" "missing '$3' in: $2";; esac; }
rel()      { printf '%s' "${1#"$PLUGIN"/}"; }                                  # a path relative to the plugin
names()    { ls -1 "$1" 2>/dev/null | LC_ALL=C sort; }                         # the entries of a directory, one per line (no dotfiles)
oneline()  { tr '\n' ' ' | sed 's/ *$//'; }
# holds_exactly <dir> <entry…> → PASS when the directory holds those entries and no others (dotfiles ignored)
holds_exactly() {
  local dir="$1" missing extra; shift
  printf '%s\n' "$@" | LC_ALL=C sort > "$WORK/want"; names "$dir" > "$WORK/have"
  missing=$(LC_ALL=C comm -23 "$WORK/want" "$WORK/have" | oneline); extra=$(LC_ALL=C comm -13 "$WORK/want" "$WORK/have" | oneline)
  if [ -z "$missing$extra" ]; then ok "${dir#"$PLUGIN"/} holds exactly: $*"
  else bad "${dir#"$PLUGIN"/} holds exactly: $*" "missing: ${missing:-none}; unexpected: ${extra:-none}"; fi
}

# json_ok <file> → 0 when the file is one valid JSON document
json_ok() {
  if $HAVE_JQ; then jq empty "$1" >/dev/null 2>&1
  else python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" >/dev/null 2>&1; fi
}
# json_field <file> <.a.b> → the string at that object path ("" when it is absent or not a string)
json_field() {
  if $HAVE_JQ; then jq -r "$2 | if type == \"string\" then . else empty end" "$1" 2>/dev/null
  else python3 - "$1" "$2" <<'PY' 2>/dev/null
import json, sys
cur = json.load(open(sys.argv[1]))
for k in [p for p in sys.argv[2].split('.') if p]:
    cur = cur.get(k) if isinstance(cur, dict) else None
print(cur if isinstance(cur, str) else '')
PY
  fi
}
# mp_entry <marketplace.json> <plugin name> <field> → that field of the plugin's entry ("" when there is none; a non-string comes back as JSON)
mp_entry() {
  if $HAVE_JQ; then
    jq -r --arg n "$2" --arg f "$3" '[.plugins[]? | select(.name == $n)][0] | .[$f] // empty | if type == "string" then . else tojson end' "$1" 2>/dev/null
  else
    python3 - "$1" "$2" "$3" <<'PY' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1]))
for p in d.get('plugins') or []:
    if isinstance(p, dict) and p.get('name') == sys.argv[2]:
        v = p.get(sys.argv[3])
        print(v if isinstance(v, str) else ('' if v is None else json.dumps(v)))
        break
PY
  fi
}
# hook_rows <hooks.json> → one line per hook command: event US matcher US onFailure US command
hook_rows() {
  if $HAVE_JQ; then
    jq -r '(.hooks // {}) | to_entries[] | .key as $e | (.value // [])[] | (.matcher // "") as $m | (.hooks // [])[] | [$e, $m, (.onFailure // ""), (.command // "")] | join("\u001f")' "$1" 2>/dev/null
  else
    python3 - "$1" <<'PY' 2>/dev/null
import json, sys
for ev, entries in (json.load(open(sys.argv[1])).get('hooks') or {}).items():
    for ent in entries or []:
        for h in ent.get('hooks') or []:
            print('\x1f'.join([ev, ent.get('matcher') or '', str(h.get('onFailure') or ''), str(h.get('command') or '')]))
PY
  fi
}
# fm_get <file> <key> → the value of a top-level `key:` in the leading --- block: quotes and a trailing # comment removed,
# a block scalar (| >) or a list joined with spaces; empty when the file has no frontmatter or the key is absent
fm_get() {
  [ -f "$1" ] || return 0
  awk -v key="$2" '
    function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t\r]+$/, "", s); return s }
    NR == 1 { if ($0 !~ /^---[ \t\r]*$/) exit; next }
    /^---[ \t\r]*$/ { exit }
    cap { if ($0 ~ /^[ \t]/ || $0 ~ /^-[ \t]/ || $0 ~ /^[ \t\r]*$/) { t = trim($0); sub(/^-[ \t]+/, "", t); if (t != "") val = (val == "" ? t : val " " t); next } exit }
    {
      line = $0; sub(/\r$/, "", line); n = length(key) + 1
      if (substr(line, 1, n) != key ":") next
      v = trim(substr(line, n + 1))
      if (v == "" || v ~ /^[|>][+-]?[ \t]*(#.*)?$/) { cap = 1; next }
      q = substr(v, 1, 1)
      if (q == "\"" || q == "\047") { v = substr(v, 2); j = index(v, q); if (j > 0) v = substr(v, 1, j - 1) }
      else sub(/[ \t]+#.*$/, "", v)
      val = v; exit
    }
    END { print val }' "$1"
}
# group_hits  (stdin: grep -n -o lines "file:line:match") → file TAB distinct matches TAB line numbers, one row per file
group_hits() {
  awk '{ i = index($0, ":"); f = substr($0, 1, i - 1); r = substr($0, i + 1); j = index(r, ":"); l = substr(r, 1, j - 1); m = substr(r, j + 1)
         if (!(f in seen)) { seen[f] = 1; order[++n] = f }
         if (index(" " ml[f] " ", " " m " ") == 0) ml[f] = ml[f] (ml[f] == "" ? "" : " ") m
         if (!((f, l) in lseen)) { lseen[f, l] = 1; ll[f] = ll[f] (ll[f] == "" ? "" : ",") l } }
       END { for (k = 1; k <= n; k++) { f = order[k]; printf "%s\t%s\t%s\n", f, ml[f], ll[f] } }'
}

echo "== sdlc structural tests  (plugin: $PLUGIN, work: $WORK, jq: $($HAVE_JQ && echo yes || echo no), python3: $($HAVE_PY && echo yes || echo no))"

# ---------------------------------------------------------------- manifests
echo "-- manifests"
PJ="$PLUGIN/.claude-plugin/plugin.json"; MJ="$REPO/.claude-plugin/marketplace.json"
for f in "$PJ" "$MJ"; do
  if json_ok "$f"; then ok "${f#"$REPO"/} parses as JSON"; else bad "${f#"$REPO"/} parses as JSON" "missing or not valid JSON: $f"; fi
done
pver=$(json_field "$PJ" .version)
eq "plugin.json: name is sdlc" "$(json_field "$PJ" .name)" sdlc
case "$pver" in [0-9]*.[0-9]*.[0-9]*) ok "plugin.json: version is x.y.z" "$pver";; *) bad "plugin.json: version is x.y.z" "got '$pver'";; esac
if grep -q 'hooks/hooks\.json' "$PJ" 2>/dev/null; then bad "plugin.json does not name hooks/hooks.json" "hooks/hooks.json loads by itself; naming it makes the plugin fail with 'duplicate hooks file'"
else ok "plugin.json does not name hooks/hooks.json (it loads by itself)"; fi
eq "marketplace.json lists the plugin sdlc" "$(mp_entry "$MJ" sdlc name)" sdlc
msrc=$(mp_entry "$MJ" sdlc source)
case "$msrc" in
  ./*|../*) eq "marketplace entry sdlc: source is this plugin's directory" "$(cd "$REPO/$msrc" 2>/dev/null && pwd)" "$PLUGIN" "$msrc";;
  "")       bad "marketplace entry sdlc: has a source" "the entry names none";;
  *)        ok "marketplace entry sdlc: source is not a relative path, not checked" "$msrc";;
esac
mver=$(mp_entry "$MJ" sdlc version)
if [ -z "$mver" ]; then ok "marketplace entry sdlc: carries no version, nothing to compare"
else eq "marketplace entry sdlc: version equals plugin.json's" "$mver" "$pver"; fi

# ---------------------------------------------------------------- skills
echo "-- skills"
SKILLS="go init run status"
holds_exactly "$PLUGIN/skills" $SKILLS
for d in "$PLUGIN"/skills/*/; do
  [ -d "$d" ] || continue
  s=$(basename "$d"); f="$d/SKILL.md"
  if [ ! -f "$f" ]; then bad "skill $s: has a SKILL.md" "no $(rel "$f")"; continue; fi
  eq "skill $s: frontmatter name equals its directory" "$(fm_get "$f" name)" "$s"
  nonempty "skill $s: description is non-empty" "$(fm_get "$f" description)"
done

# every references/<file>.md a skill mentions is in a references/ directory: its own, or the one of the skill whose path it spells out
n=0; miss=0
for d in "$PLUGIN"/skills/*/; do
  [ -d "$d" ] || continue
  s=$(basename "$d")
  hits=$(cd "$d" && grep -rIoHE '([A-Za-z0-9_${}./-]*/)?references/[A-Za-z0-9_.-]+\.md' . 2>/dev/null | LC_ALL=C sort -u)
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    from="${line%%:*}"; tok="${line#*:}"
    case "$tok" in
      '${CLAUDE_PLUGIN_ROOT}/'*) continue ;;                  # the path check below resolves these
      *skills/*) target="$PLUGIN/skills/${tok#*skills/}" ;;   # another skill's reference, spelled with its path
      *)         target="$d/$tok" ;;
    esac
    n=$((n+1))
    [ -f "$target" ] || { miss=$((miss+1)); bad "skill $s: $tok (mentioned in ${from#./}) exists" "no file $(rel "$target")"; }
  done <<< "$hits"
done
[ "$miss" -eq 0 ] && ok "every references/<file>.md a skill mentions exists ($n mentions)"

# every ${CLAUDE_PLUGIN_ROOT}/<path> named in skills, agents, hooks or templates exists; <placeholder> segments and * match any name
refs=$(cd "$PLUGIN" && grep -rIoHE '\$\{CLAUDE_PLUGIN_ROOT\}/[A-Za-z0-9_./<>*-]+' skills agents hooks templates 2>/dev/null \
  | awk '{ m = ":${CLAUDE_PLUGIN_ROOT}/"; i = index($0, m); printf "%s\t%s\n", substr($0, 1, i - 1), substr($0, i + length(m)) }' | LC_ALL=C sort -u)
n=0; miss=0
while IFS="$TAB" read -r f p; do
  [ -n "$p" ] || continue
  while [ "${p%.}" != "$p" ]; do p="${p%.}"; done            # the full stop that ends a sentence is not part of the path
  p=$(printf '%s' "$p" | sed 's/<[^>]*>/*/g')
  n=$((n+1)); found=false
  case "$p" in
    *'*'*) ( cd "$PLUGIN" && for m in $p; do [ -e "$m" ] && exit 0; done; exit 1 ) && found=true ;;
    *)     [ -e "$PLUGIN/$p" ] && found=true ;;
  esac
  $found || { miss=$((miss+1)); bad "\${CLAUDE_PLUGIN_ROOT}/$p (named in $f) exists" "nothing at $PLUGIN/$p"; }
done <<< "$refs"
[ "$miss" -eq 0 ] && ok "every \${CLAUDE_PLUGIN_ROOT}/<path> in skills, agents, hooks and templates exists ($n paths)"

# ---------------------------------------------------------------- agents
echo "-- agents"
AGENTS="sdlc-builder sdlc-researcher sdlc-reviewer sdlc-simplifier sdlc-verifier"
holds_exactly "$PLUGIN/agents" $(for a in $AGENTS; do echo "$a.md"; done)
for f in "$PLUGIN"/agents/*.md; do
  [ -f "$f" ] || continue
  a=$(basename "$f" .md)
  eq "agent $a: frontmatter name equals the file name" "$(fm_get "$f" name)" "$a"
  nonempty "agent $a: description is non-empty" "$(fm_get "$f" description)"
  nonempty "agent $a: tools is non-empty" "$(fm_get "$f" tools)"
  nonempty "agent $a: effort is non-empty" "$(fm_get "$f" effort)"
  model=$(fm_get "$f" model)
  case "$model" in
    haiku|sonnet|opus) ok "agent $a: model is an alias" "$model" ;;
    *) bad "agent $a: model is an alias (haiku, sonnet or opus)" "got '$model' — a full model id pins the plugin to one model generation" ;;
  esac
done

# ---------------------------------------------------------------- hooks
echo "-- hooks"
HJ="$PLUGIN/hooks/hooks.json"
if json_ok "$HJ"; then ok "hooks/hooks.json parses as JSON"; else bad "hooks/hooks.json parses as JSON" "missing or not valid JSON: $HJ"; fi
rows=$(hook_rows "$HJ")
nrows=0; nscripts=0; miss=0
while IFS="$US" read -r ev matcher onfail cmd; do
  [ -n "$cmd" ] || continue
  nrows=$((nrows+1))
  p=$(printf '%s' "$cmd" | grep -oE '\$\{CLAUDE_PLUGIN_ROOT\}/[A-Za-z0-9_./-]+' | head -1); p="${p#\$\{CLAUDE_PLUGIN_ROOT\}/}"
  [ -n "$p" ] || continue
  nscripts=$((nscripts+1))
  [ -f "$PLUGIN/$p" ] || { miss=$((miss+1)); bad "hooks.json $ev: the script $p exists" "no file $PLUGIN/$p (command: $cmd)"; }
done <<< "$rows"
if [ "$nrows" -eq 0 ]; then bad "hooks.json registers hook commands" "no hook command found in $HJ"
elif [ "$miss" -eq 0 ]; then ok "every script hooks.json runs exists ($nscripts scripts in $nrows hook commands)"; fi
# the two guards must run before the tool call (PreToolUse), cover every tool they protect, and fail closed
for g in guard-edit guard-bash; do
  case "$g" in guard-edit) need="Edit Write MultiEdit NotebookEdit";; *) need="Bash";; esac
  gn=0; gblock=0; gpre=0; gmatchers=""
  while IFS="$US" read -r ev matcher onfail cmd; do
    case "$cmd" in
      *"/scripts/hooks/$g.sh"*)
        gn=$((gn+1)); [ "$onfail" = block ] && gblock=$((gblock+1))
        if [ "$ev" = PreToolUse ]; then gpre=$((gpre+1)); gmatchers="$gmatchers|$matcher"; fi ;;
    esac
  done <<< "$rows"
  gmiss=""; for t in $need; do case "$gmatchers|" in *"|$t|"*) ;; *) gmiss="$gmiss $t";; esac; done   # gmatchers is |m1|m2…: a matcher is a|b|c
  if [ "$gn" -eq 0 ]; then bad "hooks.json registers $g.sh" "no hook command runs scripts/hooks/$g.sh"
  elif [ "$gpre" -ne "$gn" ]; then bad "$g.sh runs on PreToolUse" "$gn registration(s), $gpre under PreToolUse"
  elif [ -n "$gmiss" ]; then bad "$g.sh's matcher covers $need" "not matched:$gmiss (matchers: $gmatchers)"
  else ok "$g.sh runs on PreToolUse and matches $need" "matcher: ${gmatchers#|}"; fi
  if [ "$gn" -gt 0 ]; then
    if [ "$gblock" -eq "$gn" ]; then ok "$g.sh carries \"onFailure\": \"block\" (fails closed)" "$gn registration(s)"
    else bad "$g.sh carries \"onFailure\": \"block\" (fails closed)" "$gblock of $gn registration(s) carry it; without it a guard that crashes or times out lets the tool call through"; fi
  fi
done

# ---------------------------------------------------------------- templates
echo "-- templates"
T="$PLUGIN/templates"
LANG_TEMPLATES="CLAUDE.sections.md REVIEW.md work-README.md work.md"            # in templates/en and in templates/ko
CONFIG_TEMPLATES="frozen-paths.txt settings.project.json"   # in templates/config (init builds config.json and the .gitignore lines in code)
holds_exactly "$T" config en ko
holds_exactly "$T/en" $LANG_TEMPLATES
holds_exactly "$T/ko" $LANG_TEMPLATES
holds_exactly "$T/config" $CONFIG_TEMPLATES
for f in settings.project.json; do
  if json_ok "$T/config/$f"; then ok "templates/config/$f parses as JSON"; else bad "templates/config/$f parses as JSON" "missing or not valid JSON"; fi
done
eq "templates/en and templates/ko hold the same file names" "$(names "$T/en" | oneline)" "$(names "$T/ko" | oneline)"
# a template is read when some script, skill, agent or hook names the file. The repository being adopted has its own .sdlc/<name>
# (config.json, frozen-paths.txt); mentions of that one are not references to the template.
template_read() {
  local base="$1" esc
  esc="${base//./\\.}"
  ( cd "$PLUGIN" && grep -rIhF --exclude-dir=__pycache__ -e "$base" scripts skills agents hooks 2>/dev/null ) \
    | sed -e "s|\\.sdlc/$esc||g" -e "s|\"\\.sdlc\", \"$esc\"||g" | grep -qF -e "$base"
}
while IFS= read -r base; do
  [ -n "$base" ] || continue
  if template_read "$base"; then ok "template $base is read by code or a skill"
  else bad "template $base is read by code or a skill" "no file under scripts/, skills/, agents/ or hooks/ names '$base' — delete the template, or make the code read it"; fi
done <<< "$(find "$T" -type f -not -name '.*' 2>/dev/null | sed 's|.*/||' | LC_ALL=C sort -u)"

# ---------------------------------------------------------------- stale 0.x references
echo "-- no dangling 0.x references"
# scripts/sdlclib/init.py is exempt: it recognises old command mentions in a user's CLAUDE.md on purpose; tests/ are not scanned
SCOPE="skills agents hooks templates scripts"
REMOVED_CMDS="intent spec plan verify review release monitor triage postmortem metrics evals lesson policy scan doctor"
DELETED_SCRIPTS="run-loop.sh init.sh status.sh doctor.sh metrics.py monitor.py run-evals.sh review-gate.sh"
found=$(cd "$PLUGIN" && grep -rInoE '/sdlc:[a-z][a-z0-9_-]*' $SCOPE --exclude-dir=__pycache__ 2>/dev/null \
  | grep -v '^scripts/sdlclib/init\.py:' \
  | awk -v names="$REMOVED_CMDS" 'BEGIN { n = split(names, a, " "); for (i = 1; i <= n; i++) del["/sdlc:" a[i]] = 1 }
      { i = index($0, ":"); r = substr($0, i + 1); j = index(r, ":"); if (substr(r, j + 1) in del) print }' | group_hits)
if [ -z "$found" ]; then ok "no removed command (/sdlc:$(printf '%s' "$REMOVED_CMDS" | tr ' ' '|')) is mentioned"
else while IFS="$TAB" read -r f m l; do bad "$f mentions removed commands" "$m (lines $l)"; done <<< "$found"; fi
# a file name counts whole: run-loop.sh, but not my-run-loop.sh or run-loop.shx (the match carries its closing character; awk cuts it off)
found=$(cd "$PLUGIN" && grep -rInoE '[A-Za-z0-9_./-]+\.(sh|py)([^A-Za-z0-9_.-]|\.([^A-Za-z0-9_-]|$)|$)' $SCOPE --exclude-dir=__pycache__ 2>/dev/null \
  | grep -v '^scripts/sdlclib/init\.py:' \
  | awk -v names="$DELETED_SCRIPTS" 'BEGIN { n = split(names, a, " "); for (i = 1; i <= n; i++) del[a[i]] = 1 }
      { i = index($0, ":"); f = substr($0, 1, i - 1); r = substr($0, i + 1); j = index(r, ":"); l = substr(r, 1, j - 1); m = substr(r, j + 1)
        if (!match(m, "[A-Za-z0-9_./-]+\\.(sh|py)")) next
        k = split(substr(m, RSTART, RLENGTH), parts, "/"); if (parts[k] in del) print f ":" l ":" parts[k] }' | group_hits)
if [ -z "$found" ]; then ok "no deleted script ($DELETED_SCRIPTS) is mentioned"
else while IFS="$TAB" read -r f m l; do bad "$f mentions deleted scripts" "$m (lines $l)"; done <<< "$found"; fi

# ---------------------------------------------------------------- shell syntax
echo "-- shell syntax"
n=0; miss=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  n=$((n+1))
  if ! err=$(bash -n "$f" 2>&1); then miss=$((miss+1)); bad "bash -n $(rel "$f")" "$err"; fi
done <<< "$(find "$PLUGIN" -name '*.sh' -type f -not -path '*/.git/*' 2>/dev/null | LC_ALL=C sort)"
[ "$miss" -eq 0 ] && ok "bash -n passes for every *.sh in the plugin ($n files)"

# ---------------------------------------------------------------- secret-scan.sh
echo "-- secret-scan.sh"
SS="$SCRIPTS/secret-scan.sh"
FAKE_KEY="AKIA""$(printf 'Q%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16)"   # AWS-key shaped, assembled at run time: this file holds no secret literal
DOC_KEY="AKIA""IOSFODNN7""EXAMPLE"                                          # AWS's documentation key; the word EXAMPLE puts it on the allowlist
# no_leak <name> <scanner output> → PASS when neither the fake credential nor a run of its filler is in the output (a failure never echoes it)
no_leak() { case "$2" in *"$FAKE_KEY"*|*QQQQQQQQ*) bad "$1" "the scanner printed (part of) the credential";; *) ok "$1";; esac; }

out=$(printf 'aws_access_key_id = %s\n' "$FAKE_KEY" | bash "$SS" --text 2>&1); rc=$?
eq  "--text: a credential-shaped string exits 1" "$rc" 1
has "--text: the hit names the pattern, not the value" "$out" "pattern#"
no_leak "--text: the credential is never printed" "$out"
out=$(printf 'nothing to see here\nconst answer = 42\n' | bash "$SS" --text 2>&1); rc=$?
eq "--text: clean text exits 0" "$rc" 0
eq "--text: clean text prints nothing" "$out" ""
out=$(printf 'aws_access_key_id = %s\n' "$DOC_KEY" | bash "$SS" --text 2>&1); rc=$?
eq "--text: a placeholder on the allowlist (EXAMPLE) exits 0" "$rc" 0
out=$(printf '' | bash "$SS" --text 2>&1); rc=$?
eq "--text: empty input exits 0" "$rc" 0
X="$WORK/extra"; mkdir -p "$X/.sdlc"; printf '{"version": 2, "secrets": {"extra_patterns": ["ACME-[0-9]{8}"]}}\n' > "$X/.sdlc/config.json"
out=$(printf 'ticket token ACME-12345678\n' | bash "$SS" --text "$X" 2>&1); rc=$?
eq "--text <root>: secrets.extra_patterns of .sdlc/config.json apply" "$rc" 1
out=$(printf 'ticket token ACME-12345678\n' | bash "$SS" --text "$WORK" 2>&1); rc=$?
eq "--text <root>: no extra patterns outside an initialized project" "$rc" 0
out=$(bash "$SS" 2>&1); rc=$?;            eq "no mode: usage error, exit 2" "$rc" 2
out=$(bash "$SS" --staged 2>&1); rc=$?;   eq "--staged without a root: usage error, exit 2" "$rc" 2
out=$(bash "$SS" --bogus 2>&1); rc=$?;    eq "an unknown mode: usage error, exit 2" "$rc" 2

# --staged runs on throwaway repositories below $WORK, one per case; each starts with a clean README committed
new_repo() {   # <name> → $R, a repository holding one clean commit
  R="$WORK/$1"; mkdir -p "$R"
  git -C "$R" init -q . >/dev/null 2>&1; printf 'hello\n' > "$R/README.md"
  git -C "$R" add README.md >/dev/null 2>&1; g "$R" commit -q -m base >/dev/null 2>&1
}
g()    { local d="$1"; shift; git -C "$d" -c user.name=t -c user.email=t@t -c commit.gpgsign=false -c core.hooksPath=/dev/null "$@"; }
scan() { SCAN_OUT=$(bash "$SS" --staged "$R" "$@" 2>&1); SCAN_RC=$?; }

new_repo staged-secret; mkdir -p "$R/src"; printf 'key = %s\n' "$FAKE_KEY" > "$R/src/app.txt"; g "$R" add src/app.txt >/dev/null 2>&1; scan
eq  "--staged: a credential added to a staged file exits 1" "$SCAN_RC" 1
has "--staged: the hit names the pattern" "$SCAN_OUT" "pattern#"
no_leak "--staged: the credential is never printed" "$SCAN_OUT"

new_repo staged-evals; mkdir -p "$R/evals/fixtures"; printf 'key = %s\n' "$FAKE_KEY" > "$R/evals/fixtures/creds.txt"; g "$R" add evals/fixtures/creds.txt >/dev/null 2>&1; scan
eq  "--staged: a credential staged under evals/ is flagged too (no directory is exempt)" "$SCAN_RC" 1
no_leak "--staged: …and not printed" "$SCAN_OUT"

new_repo staged-lookalike; printf '++ %s\n' "$FAKE_KEY" > "$R/notes.txt"; g "$R" add notes.txt >/dev/null 2>&1; scan
eq "--staged: an added line that starts with '++ ' is scanned, not mistaken for a file header" "$SCAN_RC" 1

new_repo staged-clean; printf 'more text\n' >> "$R/README.md"; printf 'doc key = %s\n' "$DOC_KEY" > "$R/doc.txt"; g "$R" add README.md doc.txt >/dev/null 2>&1; scan
eq "--staged: clean and allowlisted additions exit 0" "$SCAN_RC" 0
eq "--staged: …and print nothing" "$SCAN_OUT" ""

new_repo staged-removed; printf 'key = %s\n' "$FAKE_KEY" > "$R/old.txt"; printf 'key = %s\n' "$FAKE_KEY" > "$R/gone.txt"
g "$R" add old.txt gone.txt >/dev/null 2>&1; g "$R" commit -q -m "add the credential" >/dev/null 2>&1
printf 'key = read from the environment\n' > "$R/old.txt"; g "$R" add old.txt >/dev/null 2>&1; g "$R" rm -q gone.txt >/dev/null 2>&1; scan
eq "--staged: a credential that is only removed (edited out, file deleted) exits 0" "$SCAN_RC" 0
eq "--staged: …and prints nothing" "$SCAN_OUT" ""

new_repo staged-all; printf 'key = %s\n' "$FAKE_KEY" >> "$R/README.md"; scan
eq "--staged: a credential that is not staged is not scanned" "$SCAN_RC" 0
scan --all
eq "--staged --all: …but an unstaged change to a tracked file is (what commit -a would take)" "$SCAN_RC" 1
no_leak "--staged --all: the credential is never printed" "$SCAN_OUT"

echo "== $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
