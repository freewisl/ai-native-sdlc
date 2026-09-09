#!/bin/bash
# PostToolUse Bash — when the command was a verify command (commands.test/build/lint or verify.commands), record
# last_verify_{ts,epoch,cmd,ok,name} in session state and log verify.run (design §3). Result heuristics on tool_response.
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

# candidates: commands.test|build|lint (always) + verify.commands entries (names resolve, others are literal)
candidates=""
for name in test build lint; do
  c=$(cfg "$root" ".commands.$name" ""); [ -n "$c" ] && candidates="${candidates}${name}"$'\t'"${c}"$'\n'
done
candidates="${candidates}$(verify_commands "$root")"
matched=""
while IFS=$'\t' read -r name vcmd; do
  [ -z "$vcmd" ] && continue
  case "$cmd" in *"$vcmd"*) matched="$name"; break;; esac
done <<< "$candidates"
[ -z "$matched" ] && exit 0

# --- did it pass? exit code field → interrupted/is_error → output text
ok=""
for key in exit_code exitCode code returncode status; do
  v=$(json_get "$input" ".tool_response.$key")
  if is_int "$v"; then [ "$v" = 0 ] && ok=true || ok=false; break; fi
done
if [ -z "$ok" ]; then
  [ "$(json_get_raw "$input" .tool_response.interrupted)" = "true" ] && ok=false
  [ "$(json_get_raw "$input" .tool_response.is_error)" = "true" ] && ok=false
fi
if [ -z "$ok" ]; then
  text="$(json_get "$input" .tool_response.stderr)
$(json_get "$input" .tool_response.stdout)"
  [ -z "$(printf '%s' "$text" | tr -d '[:space:]')" ] && text=$(json_get "$input" .tool_response)   # plain-string response
  if printf '%s\n' "$text" | grep -E -q \
      -e '(^|[^[:alnum:]_])FAIL(ED|URE|S)?([^[:alnum:]_]|$)' \
      -e '(^|[^[:alnum:]_])[1-9][0-9]* (failed|failing|failures?|errors?)([^[:alnum:]_]|$)' \
      -e '(^|[^[:alnum:]_])(error|Error|ERROR)(\[[A-Za-z0-9_]+\])?:' \
      -e 'Exit code [1-9]' -e 'npm ERR!' -e 'Traceback \(most recent call last\)' \
      -e '(Failures|Errors): [1-9]'; then ok=false; else ok=true; fi
fi

state_set_many "$root" last_verify_ts "$(now_iso)" last_verify_epoch "$(epoch_now)" \
  last_verify_cmd "$(trunc "$(one_line "$cmd")" 200)" last_verify_ok "$ok" last_verify_name "$matched"
decision=pass; [ "$ok" = true ] || decision=fail
log_event "$root" verify.run "$decision" "$matched" "{\"cmd\":$(json_escape "$(trunc "$(one_line "$cmd")" 200)"),\"ok\":$ok}"
exit 0
