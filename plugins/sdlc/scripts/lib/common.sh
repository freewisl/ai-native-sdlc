#!/bin/bash
# sdlc plugin — shared shell library. Source from hooks and scripts:
#   . "$(dirname "$0")/../lib/common.sh"      (from scripts/hooks/*.sh)
#   . "$(dirname "$0")/lib/common.sh"         (from scripts/*.sh)
# No dependency beyond bash 3.2+, and jq OR python3.

SDLC_CONFIG_REL=".sdlc/config.json"

# ---------- basics ----------
has_cmd() { command -v "$1" >/dev/null 2>&1; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# json_get <json-string> <path>   path like .tool_input.command  (jq syntax; python fallback supports .a.b.c and .a[0])
# The python fallback reads the JSON from its standard input: an environment variable is capped (about 1 MB in all on macOS, 128 KiB per
# string on Linux — E2BIG), and a hook payload that carries a whole file is bigger than that.
json_get() {
  local json="$1" path="$2"
  if has_cmd jq; then
    printf '%s' "$json" | jq -r "$path | if . == null then empty else . end" 2>/dev/null
  else
    printf '%s' "$json" | python3 -c '
import json,sys
path=sys.argv[1]
try:
    data=json.loads(sys.stdin.read() or "null")
except Exception:
    sys.exit(3)
cur=data
for part in [p for p in path.strip(".").replace("[",".").replace("]","").split(".") if p]:
    if isinstance(cur,list):
        try: cur=cur[int(part)]
        except Exception: sys.exit(0)
    elif isinstance(cur,dict):
        cur=cur.get(part)
    else:
        sys.exit(0)
    if cur is None: sys.exit(0)
if isinstance(cur,(dict,list)):
    print(json.dumps(cur))
elif isinstance(cur,bool):
    print("true" if cur else "false")
else:
    print(cur)
' "$path" 2>/dev/null
  fi
}

# json_ok <json-string> → 0 when it is one JSON object (what a hook gets on stdin); 1 when it is not; 2 when there is neither jq nor python3
# to tell. The guards (guard-edit, guard-bash) use it to fail closed: a payload they cannot read is a blocked call, not a passed one.
json_ok() {
  if has_cmd jq; then printf '%s' "$1" | jq -e 'type == "object"' >/dev/null 2>&1; return $?; fi
  if has_cmd python3; then
    printf '%s' "$1" | python3 -c 'import json,sys; sys.exit(0 if isinstance(json.loads(sys.stdin.read()), dict) else 1)' >/dev/null 2>&1; return $?
  fi
  return 2
}

# json_file_get <file> <path>
json_file_get() { [ -f "$1" ] || return 0; json_get "$(cat "$1")" "$2"; }

# json_escape <string>  -> JSON string literal (with quotes)
json_escape() {
  if has_cmd jq; then printf '%s' "$1" | jq -Rs . ; else printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; fi
}

# ---------- project root & config ----------
# project_root [hook-stdin-json]  → $CLAUDE_PROJECT_DIR, else stdin cwd, else git toplevel, else pwd
project_root() {
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then printf '%s' "$CLAUDE_PROJECT_DIR"; return; fi
  local cwd=""
  [ -n "${1:-}" ] && cwd=$(json_get "$1" .cwd)
  [ -z "$cwd" ] && cwd=$(pwd)
  local top
  top=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || true)
  printf '%s' "${top:-$cwd}"
}

# _cfg_text <root> → the config file's text (empty when there is none), read without starting another program
_cfg_text() { local f="$1/$SDLC_CONFIG_REL"; [ -f "$f" ] && printf '%s' "$(<"$f")"; return 0; }
# cfg_mentions <config-text> <json-path> → 1 when the path is a plain .a.b.c whose last key does not occur in the text at all:
# a key the file never names cannot be set, so the caller keeps its default and jq (or python) is never started
cfg_mentions() {
  case "$2" in *[!A-Za-z0-9_.]*) return 0;; esac
  case "$1" in *"${2##*.}"*) return 0;; esac
  return 1
}
# cfg <root> <json-path> [default]   reads .sdlc/config.json (path like .protect.protect_tests)
cfg() {
  local root="$1" path="$2" def="${3:-}" v="" t
  t=$(_cfg_text "$root")
  if cfg_mentions "$t" "$path"; then v=$(json_get "$t" "$path"); fi
  printf '%s' "${v:-$def}"
}
sdlc_initialized() { [ -f "$1/$SDLC_CONFIG_REL" ]; }

# rel_path <root> <abs-or-rel path> → path relative to root (no leading ./). A path that lies under the root only through
# a symlink (/var vs /private/var) is matched on the physical root too; one outside the root stays absolute.
rel_path() {
  local root="${1%/}" p="$2" phys
  case "$p" in
    "$root"/*) p="${p#"$root"/}" ;;
    /*) phys=$(cd "$root" 2>/dev/null && pwd -P)
        if [ -n "$phys" ]; then case "$p" in "$phys"/*) p="${p#"$phys"/}" ;; esac; fi ;;
  esac
  printf '%s' "${p#./}"
}

# glob_match <pattern> <path> → 0 when the path matches. The glob rules of every list in the config (frozen paths, generated paths,
# test paths), the same as glob_match() in scripts/sdlclib/util.py, which the merge gate uses; tests/hooks.sh compares the two.
# The pattern is trimmed (an empty one matches nothing), a leading ./ of the path is dropped. A leading / or ./ of the pattern says
# "from the repository root": it is dropped, and such a pattern is never matched against the file name alone. `dir/` is a
# directory: the directory itself or anything below it; what comes before the slash is a glob like any other (`**/migrations/`,
# `src/*/secrets/`). Any other pattern must match the whole path: `*` is any run of characters but /, `?` one character but /,
# `**/` any number of directories (also none), `**` anything, / included; every other character stands for itself (also [ ] { }).
# A pattern without / (and without a leading / or ./) also matches the file name alone. A pattern that is nothing but slashes
# matches nothing. Bytes, not characters: one `?` is one byte of a multi-byte one.
# A pattern is translated into an anchored extended regular expression, and the first 16 of a process are kept (SDLC_GLOB_KEY →
# SDLC_GLOB_ERE): a command that names many paths asks about the same few patterns again and again.
SDLC_GLOB_KEY=(); SDLC_GLOB_ERE=()
glob_match() {
  local LC_ALL=C pat="$1" p="$2" re="" k=0 nk=0 i=0 len=0 c="" rooted=0 dir=0 key=""
  pat="${pat#"${pat%%[![:space:]]*}"}"; pat="${pat%"${pat##*[![:space:]]}"}"
  [ -n "$pat" ] || return 1
  case "$p" in ./*) p="${p#./}";; esac
  while :; do
    case "$pat" in ./*) pat="${pat#./}"; rooted=1;; /*) pat="${pat#/}"; rooted=1;; *) break;; esac
  done
  [ -n "${pat//\//}" ] || return 1
  case "$pat" in */) dir=1; while [ "${pat%/}" != "$pat" ]; do pat="${pat%/}"; done ;; esac
  key="$dir:$pat"
  nk=${#SDLC_GLOB_KEY[@]}
  while [ "$k" -lt "$nk" ]; do [[ "${SDLC_GLOB_KEY[$k]}" == "$key" ]] && break; k=$((k + 1)); done
  if [ "$k" -lt "$nk" ]; then re="${SDLC_GLOB_ERE[$k]}"
  else
    len=${#pat}
    while [ "$i" -lt "$len" ]; do
      case "${pat:$i:3}" in '**/') re="$re(.*/)?"; i=$((i + 3)); continue ;; esac
      case "${pat:$i:2}" in '**') re="$re.*"; i=$((i + 2)); continue ;; esac
      c="${pat:$i:1}"; i=$((i + 1))
      case "$c" in
        '*') re="$re[^/]*" ;;
        '?') re="$re[^/]" ;;
        '\'|'^'|'$'|'.'|'['|'('|')'|'|'|'+'|'{') re="$re\\$c" ;;
        ']'|'}') re="$re[$c]" ;;
        *) re="$re$c" ;;
      esac
    done
    if [ "$dir" = 1 ]; then re="^$re(/.*)?\$"; else re="^$re\$"; fi
    if [ "$nk" -lt 16 ]; then SDLC_GLOB_KEY[$nk]="$key"; SDLC_GLOB_ERE[$nk]="$re"; fi
  fi
  [[ "$p" =~ $re ]] && return 0
  [ "$dir" = 1 ] || [ "$rooted" = 1 ] && return 1
  case "$pat" in */*) return 1;; esac
  [[ "${p##*/}" =~ $re ]]
}

# ---------- logging (audit trail) ----------
# log_event <root> <event> <decision> <reason> [detail-json]
log_event() {
  local root="$1" event="$2" decision="$3" reason="$4" detail="${5:-null}"
  [ "$(cfg "$root" .hooks.log_events true)" = "false" ] && return 0
  sdlc_initialized "$root" || return 0
  local dir="$root/.sdlc/logs"; mkdir -p "$dir" 2>/dev/null || return 0
  printf '{"ts":"%s","event":"%s","session_id":"%s","decision":"%s","reason":%s,"detail":%s}\n' \
    "$(now_iso)" "$event" "${SDLC_SESSION_ID:-}" "$decision" "$(json_escape "$reason")" "$detail" >> "$dir/events.jsonl" 2>/dev/null || true
}

# ---------- hook responses ----------
# block <message>  → exit 2, message to Claude via stderr: "<what>. Why: <rule>. To proceed: <route>."
block() { printf '[sdlc] BLOCKED: %s\n' "$1" >&2; exit 2; }

# ---------- session state ----------
state_dir() { printf '%s' "$1/.sdlc/state"; }
session_state_file() { printf '%s/session-%s.json' "$(state_dir "$1")" "${SDLC_SESSION_ID:-default}"; }
# state_set <root> <key> <value>
state_set() {
  local root="$1" key="$2" val="$3" f; f=$(session_state_file "$root")
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  local cur='{}'; [ -f "$f" ] && cur=$(cat "$f")
  if has_cmd jq; then
    printf '%s' "$cur" | jq --arg k "$key" --arg v "$val" '.[$k]=$v' > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
  else
    python3 - "$f" "$key" "$val" <<'PY'
import json,sys
f,k,v=sys.argv[1:4]
try: d=json.load(open(f))
except Exception: d={}
d[k]=v
json.dump(d,open(f,'w'))
PY
  fi
}
state_get() { local f; f=$(session_state_file "$1"); [ -f "$f" ] && json_file_get "$f" ".$2"; }

# frontmatter_get <file> <key> → the value of a top-level `key:` in the leading --- block, quotes and a trailing
# `# comment` removed (empty when the file has no frontmatter or the key is absent)
frontmatter_get() {
  [ -f "$1" ] || return 0
  awk -v key="$2" '
    NR == 1 { if ($0 !~ /^---[ \t\r]*$/) exit; next }
    /^---[ \t\r]*$/ { exit }
    {
      line = $0; sub(/\r$/, "", line); n = length(key) + 1
      if (substr(line, 1, n) == key ":") {
        v = substr(line, n + 1); sub(/^[ \t]+/, "", v); q = substr(v, 1, 1)
        if (q == "\"" || q == "\047") { v = substr(v, 2); j = index(v, q); if (j > 0) v = substr(v, 1, j - 1) }
        else { sub(/[ \t]+#.*$/, "", v); sub(/^#.*$/, "", v); sub(/[ \t]+$/, "", v) }
        print v; exit
      }
    }' "$1" 2>/dev/null
}

# ---------- active work doc ----------
# .sdlc/state/active-work holds one slug (written by the engine for the session it runs, or by /sdlc:go). The doc is
# <paths.work>/<slug>.md. It applies ONLY while the checkout is on branch sdlc/<slug>: a stale file left by an abandoned
# session must never block anything.
ACTIVE_WORK_REL=".sdlc/state/active-work"
current_branch() { git -C "$1" symbolic-ref --quiet --short HEAD 2>/dev/null; }
work_dir() { local d; d=$(cfg "$1" .paths.work work); d="${d%/}"; printf '%s' "${d:-work}"; }
# active_work_slug <root> → the slug, when its branch is the one checked out
active_work_slug() {
  local f="$1/$ACTIVE_WORK_REL" slug
  [ -f "$f" ] || return 0
  slug=$(head -n 1 "$f" 2>/dev/null | tr -d '[:space:]')
  case "$slug" in ''|[!a-z0-9]*|*[!a-z0-9._-]*) return 0;; esac
  [ "$(current_branch "$1")" = "sdlc/$slug" ] && printf '%s' "$slug"
  return 0
}
# active_work_file <root> → repo-relative path of the active work doc, when it applies and the file exists
active_work_file() {
  local slug rel; slug=$(active_work_slug "$1"); [ -n "$slug" ] || return 0
  rel="$(work_dir "$1")/$slug.md"
  [ -f "$1/$rel" ] && printf '%s' "$rel"
  return 0
}
