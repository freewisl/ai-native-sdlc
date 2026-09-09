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
# default config shipped with the plugin (used when the project has no .sdlc/config.json)
sdlc_default_config() { printf '%s/templates/config/config.json' "$(sdlc_plugin_root)"; }

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

# json_keys <json> <path>: object keys, one per line, in file order
json_keys() {
  local json="$1" path="$2"
  if has_cmd jq; then
    printf '%s' "$json" | jq -r "$path // {} | if type == \"object\" then keys_unsorted[] else empty end" 2>/dev/null
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
if isinstance(cur,dict):
    for k in cur: print(k)
PY
  fi
}

# ---------- config readers (same file as cfg(), but false-safe and array-aware) ----------
_cfg_json() { [ -f "$1/$SDLC_CONFIG_REL" ] && cat "$1/$SDLC_CONFIG_REL"; return 0; }
# cfg_raw <root> <path> [default]
cfg_raw()  { local v=""; v=$(json_get_raw "$(_cfg_json "$1")" "$2"); printf '%s' "${v:-${3:-}}"; }
# cfg_bool <root> <path> [default]  → prints true|false
cfg_bool() {
  local v; v=$(cfg_raw "$1" "$2" "${3:-false}")
  case "$v" in true|1|yes|on) printf 'true';; *) printf 'false';; esac
}
# cfg_lines <root> <path>  → array elements one per line
cfg_lines() { json_lines "$(_cfg_json "$1")" "$2"; }
cfg_keys()  { json_keys  "$(_cfg_json "$1")" "$2"; }

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
# file_mtime <file> → epoch seconds (0 when unknown)
file_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || printf '0'; }
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

# ---------- gate log (human readable: ts \t decision \t env \t cmd) ----------
gate_log() { # <root> <decision> <env> <cmd>
  local root="$1" f
  sdlc_initialized "$root" || return 0
  f=$(cfg "$root" .gate.log ".sdlc/logs/gate.log")
  case "$f" in /*) ;; *) f="$root/$f";; esac
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  printf '%s\t%s\t%s\t%s\n' "$(now_iso)" "$2" "$3" "$(one_line "$4")" >> "$f" 2>/dev/null || true
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
