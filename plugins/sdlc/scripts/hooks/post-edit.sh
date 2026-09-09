#!/bin/bash
# PostToolUse Edit|Write|MultiEdit — record the edit in session state, then run the formatter (design §3).
# Never fails the hook: formatter problems are a one-line stderr note, exit 0. Cap per run: SDLC_FORMAT_TIMEOUT seconds (default 20).
# Reads: hooks.format_on_edit, commands.format ("auto" = detect by extension + tool presence), commands.lint_file (optional per-file linter, {file} substituted)
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
sdlc_initialized "$root" || exit 0

path=$(json_get "$input" .tool_input.file_path)
[ -z "$path" ] && path=$(json_get "$input" .tool_input.notebook_path)
case "$path" in ''|/*) ;; *) path="$root/$path";; esac

state_set_many "$root" last_edit_ts "$(now_iso)" last_edit_epoch "$(epoch_now)" edited true
# editing the active plan itself keeps plan and implementation in sync → no "departures" nag this session
plan_rel=$(active_plan_file "$root")
if [ -n "$plan_rel" ] && [ -n "$path" ] && [ "$(rel_path "$root" "$path")" = "$plan_rel" ]; then
  state_set "$root" plan_sync_checked true
fi

# --- formatter
[ "$(cfg_bool "$root" .hooks.format_on_edit true)" = "true" ] || exit 0
[ -n "$path" ] && [ -f "$path" ] || exit 0
fmt=$(cfg "$root" .commands.format auto)
cap="${SDLC_FORMAT_TIMEOUT:-20}"; is_int "$cap" || cap=20
out="$(state_dir "$root")/format.out"; mkdir -p "$(dirname "$out")" 2>/dev/null

run_fmt() { # run_fmt <label> <cmd...> — bounded, quiet; note on failure
  local label="$1"; shift
  ( cd "$root" 2>/dev/null || exit 0; run_capped "$cap" "$@" >"$out" 2>&1 ); rc=$?
  if [ "$rc" != 0 ]; then
    if [ "$rc" = 124 ]; then note="timed out after ${cap}s"; else note="exit $rc: $(head -1 "$out" 2>/dev/null | cut -c1-160)"; fi
    printf '[sdlc] formatter %s on %s — %s (edit kept as written)\n' "$label" "$(rel_path "$root" "$path")" "$note" >&2
  fi
  rm -f "$out" 2>/dev/null
}

case "$fmt" in
  '') ;;
  auto)
    case "$path" in
      *.ts|*.tsx|*.js|*.jsx|*.json|*.css|*.md)
        if [ -f "$root/biome.json" ] && [ -x "$root/node_modules/.bin/biome" ]; then run_fmt biome "$root/node_modules/.bin/biome" format --write "$path"
        elif [ -x "$root/node_modules/.bin/prettier" ]; then run_fmt prettier "$root/node_modules/.bin/prettier" --log-level warn --write "$path"; fi ;;
      *.py)
        if has_cmd ruff; then run_fmt ruff ruff format -q "$path"
        elif has_cmd black; then run_fmt black black -q "$path"; fi ;;
      *.go)   has_cmd gofmt && run_fmt gofmt gofmt -w "$path" ;;
      *.rs)   has_cmd rustfmt && run_fmt rustfmt rustfmt "$path" ;;
      *.java) has_cmd google-java-format && run_fmt google-java-format google-java-format -r "$path" ;;
    esac ;;
  *)
    q=$(printf '%q' "$path")
    case "$fmt" in *'{file}'*) cmdline="${fmt//\{file\}/$q}";; *) cmdline="$fmt $q";; esac
    run_fmt "'$(trunc "$fmt" 40)'" bash -c "$cmdline" ;;
esac
# --- per-file linter (optional): "run the formatter and linter after file edits so drift never accumulates"
lint=$(cfg "$root" .commands.lint_file "")
if [ -n "$lint" ]; then
  q=$(printf '%q' "$path")
  case "$lint" in *'{file}'*) cmdline="${lint//\{file\}/$q}";; *) cmdline="$lint $q";; esac
  run_fmt "lint '$(trunc "$lint" 40)'" bash -c "$cmdline"
fi
exit 0
