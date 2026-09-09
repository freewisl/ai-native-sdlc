#!/bin/bash
# sdlc plugin — shared shell library. Source from hooks and scripts:
#   . "$(dirname "$0")/../lib/common.sh"      (from scripts/hooks/*.sh)
#   . "$(dirname "$0")/lib/common.sh"         (from scripts/*.sh)
# Design contract: _workspace/02_design.md §6. No dependency beyond bash 3.2+, and jq OR python3.

SDLC_CONFIG_REL=".sdlc/config.json"

# ---------- basics ----------
has_cmd() { command -v "$1" >/dev/null 2>&1; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# json_get <json-string> <path>   path like .tool_input.command  (jq syntax; python fallback supports .a.b.c and .a[0])
json_get() {
  local json="$1" path="$2"
  if has_cmd jq; then
    printf '%s' "$json" | jq -r "$path | if . == null then empty else . end" 2>/dev/null
  else
    SDLC_JSON="$json" python3 - "$path" <<'PY' 2>/dev/null
import json,os,sys
path=sys.argv[1]
try:
    data=json.loads(os.environ.get('SDLC_JSON') or 'null')
except Exception:
    sys.exit(0)
cur=data
for part in [p for p in path.strip('.').replace('[','.').replace(']','').split('.') if p]:
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
    print('true' if cur else 'false')
else:
    print(cur)
PY
  fi
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

# cfg <root> <json-path> [default]   reads .sdlc/config.json (path like .protect.protect_tests)
cfg() {
  local root="$1" path="$2" def="${3:-}" v=""
  [ -f "$root/$SDLC_CONFIG_REL" ] && v=$(json_file_get "$root/$SDLC_CONFIG_REL" "$path")
  printf '%s' "${v:-$def}"
}
sdlc_initialized() { [ -f "$1/$SDLC_CONFIG_REL" ]; }

# rel_path <root> <abs-or-rel path> → path relative to root (no leading ./)
rel_path() {
  local root="${1%/}" p="$2"
  case "$p" in
    "$root"/*) p="${p#"$root"/}" ;;
    /*) : ;;
  esac
  printf '%s' "${p#./}"
}

# glob_match <pattern> <path>  — bash pattern match; pattern ending with '/' means prefix; '**/' handled as any depth
glob_match() {
  local pat="$1" p="$2"
  case "$pat" in
    */) case "$p" in "$pat"*|"${pat%/}") return 0;; esac; return 1 ;;
  esac
  # translate ** to * for bash extglob-free matching, also try basename match for patterns without '/'
  local simple="${pat//\*\*\//}"
  case "$p" in $pat) return 0;; $simple) return 0;; */$pat) return 0;; esac
  case "$pat" in */*) return 1;; esac
  case "$(basename "$p")" in $pat) return 0;; esac
  return 1
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
# block <message>  → exit 2, message to Claude via stderr (format per design §3)
block() { printf '[sdlc] BLOCKED: %s\n' "$1" >&2; exit 2; }
# ask_json <reason> → PreToolUse "ask" decision (human confirms in interactive session)
ask_json() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":%s}}\n' "$(json_escape "$1")"
  exit 0
}
# context_json <text> → SessionStart/UserPromptSubmit additional context (stdout is added to context on exit 0)
context_out() { printf '%s\n' "$1"; }

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

# active plan file (set by /sdlc:plan): .sdlc/state/active-plan contains repo-relative path
active_plan_file() { local f="$(state_dir "$1")/active-plan"; [ -f "$f" ] && cat "$f"; }
# frontmatter_get <file> <key>
frontmatter_get() {
  [ -f "$1" ] || return 0
  sed -n '2,/^---$/p' "$1" | grep -E "^$2:" | head -1 | sed -E "s/^$2:[[:space:]]*//; s/^\"(.*)\"$/\1/"
}
