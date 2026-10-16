#!/bin/bash
# PostToolUse and PostToolUseFailure Bash — when a simple command of the command line STARTS WITH a verify command (commands.test/
# build/lint or verify.commands; after `cd x &&`, env assignments, time … — a heredoc body or a file name that merely mentions it is
# not a run), record last_verify_{ts,epoch,cmd,ok,name} in session state and log verify.run (stop-verify reads it).
# Result: PostToolUseFailure is a failed run (Claude Code sends that event when the command exits non-zero); PostToolUse is a pass —
# unless an exit-code field says otherwise, the call was interrupted, or the command has a pipe (`npm test | tail`: the pipe's exit
# code is the last command's) and its output holds a failing-test summary. Output that merely says "Error:" or "FAIL" is not enough.
# A background run (run_in_background, or a background task id in the response) has no result yet: nothing is recorded.
# Reads: commands.test, commands.build, commands.lint, verify.commands
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
sdlc_initialized "$root" || exit 0
cmd=$(json_get "$input" .tool_input.command)
[ -z "$cmd" ] && exit 0
[ "$(json_get_raw "$input" .tool_input.run_in_background)" = "true" ] && exit 0
[ -n "$(json_get "$input" .tool_response.backgroundTaskId)" ] && exit 0
event=$(json_get "$input" .hook_event_name)

# candidates: commands.test|build|lint (always) + verify.commands entries (names resolve, others are literal)
candidates=""
for name in test build lint; do
  c=$(cfg "$root" ".commands.$name" ""); [ -n "$c" ] && candidates="${candidates}${name}"$'\t'"${c}"$'\n'
done
candidates="${candidates}$(verify_commands "$root")"

# norm_clauses <command line> → NCL: its simple commands as words joined by single spaces, past env assignments and wrappers
NCL=()
norm_clause() {
  local nw=${#W[@]} k i words=""
  tl_cmdword; k=$TL_K
  [ "$k" -lt "$nw" ] || return 1
  i=$k; while [ "$i" -lt "$nw" ]; do words="$words${words:+ }${W[$i]}"; i=$((i + 1)); done
  NCL[${#NCL[@]}]="$words"
  return 1                                          # not found: the walk goes on through every simple command
}
# runs_verify <verify command> → 0 when the command line (LINE_CL) has, in this order, a simple command that is each clause of it —
# equal, or followed by more words (`npm test -- --watch`)
runs_verify() {
  local v="$1" i=0 j=0 nl=${#LINE_CL[@]} nv ok
  NCL=(); WALK_NESTED=0; walk_scan "$v" 0 norm_clause; VERIFY_CL=(${NCL[@]+"${NCL[@]}"}); nv=${#VERIFY_CL[@]}
  [ "$nv" -gt 0 ] || return 1
  while [ "$i" -lt "$nv" ]; do
    ok=1
    while [ "$j" -lt "$nl" ]; do
      case "${LINE_CL[$j]}" in "${VERIFY_CL[$i]}"|"${VERIFY_CL[$i]} "*) ok=0; j=$((j + 1)); break;; esac
      j=$((j + 1))
    done
    [ "$ok" = 0 ] || return 1
    i=$((i + 1))
  done
  return 0
}

matched=""; LINE_CL=(); VERIFY_CL=(); line_done=0
while IFS=$'\t' read -r name vcmd; do
  [ -z "$vcmd" ] && continue
  case "$cmd" in *"${vcmd%% *}"*) ;; *) continue;; esac         # cheap: its first word is not even in the line (quotes may differ)
  if [ "$line_done" = 0 ]; then NCL=(); WALK_NESTED=0; walk_scan "$cmd" 0 norm_clause; LINE_CL=(${NCL[@]+"${NCL[@]}"}); line_done=1; fi
  if runs_verify "$vcmd"; then matched="$name"; break; fi
done <<< "$candidates"
[ -z "$matched" ] && exit 0

# --- did it pass? the event; an exit code field; interrupted/is_error; a failing-test summary of a piped command
ok=""
if [ "$event" = "PostToolUseFailure" ]; then ok=false; fi
if [ -z "$ok" ]; then
  for key in exit_code exitCode code returncode status; do
    v=$(json_get "$input" ".tool_response.$key")
    if is_int "$v"; then [ "$v" = 0 ] && ok=true || ok=false; break; fi
  done
fi
if [ -z "$ok" ]; then
  [ "$(json_get_raw "$input" .tool_response.interrupted)" = "true" ] && ok=false
  [ "$(json_get_raw "$input" .tool_response.is_error)" = "true" ] && ok=false
fi
if [ -z "$ok" ]; then
  ok=true
  unpiped="${cmd//||/}"
  case "$unpiped" in *'|'*)
    text="$(json_get "$input" .tool_response.stderr)
$(json_get "$input" .tool_response.stdout)"
    [ -z "$(printf '%s' "$text" | tr -d '[:space:]')" ] && text=$(json_get "$input" .tool_response)   # plain-string response
    if printf '%s\n' "$text" | grep -E -q \
        -e '(^|[^[:alnum:]_])FAIL(ED|URE|S)?([^[:alnum:]_]|$)' \
        -e '(^|[^[:alnum:]_])[1-9][0-9]* (failed|failing|failures?)([^[:alnum:]_]|$)' \
        -e 'Tests? failed' -e 'npm ERR!' -e '(Failures|Errors): [1-9]'; then ok=false; fi;;
  esac
fi

state_set_many "$root" last_verify_ts "$(now_iso)" last_verify_epoch "$(epoch_now)" \
  last_verify_cmd "$(trunc "$(one_line "$cmd")" 200)" last_verify_ok "$ok" last_verify_name "$matched"
decision=pass; [ "$ok" = true ] || decision=fail
log_event "$root" verify.run "$decision" "$matched" "{\"cmd\":$(json_escape "$(trunc "$(one_line "$cmd")" 200)"),\"ok\":$ok}"
exit 0
