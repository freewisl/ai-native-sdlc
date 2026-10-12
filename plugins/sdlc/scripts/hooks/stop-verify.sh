#!/bin/bash
# Stop — "verification is part of done": when verify.required_before_stop is on and files were edited without a
# successful verify run afterwards, block once with the verify commands. stop_hook_active → always allow.
# Reads: verify.required_before_stop, verify.commands, commands.*
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
[ "$(json_get_raw "$input" .stop_hook_active)" = "true" ] && exit 0
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
sdlc_initialized "$root" || exit 0
[ "$(state_get "$root" edited)" = "true" ] || exit 0

block_stop() { # <rule> <reason>
  log_event "$root" hook.stop deny "$1" null
  printf '{"decision":"block","reason":%s}\n' "$(json_escape "$2")"
  exit 0
}

# verify before done
if [ "$(cfg_bool "$root" .verify.required_before_stop false)" = "true" ]; then
  configured=false
  while IFS=$'\t' read -r name vcmd; do [ -n "$vcmd" ] && configured=true; done <<< "$(verify_commands "$root")"
  if [ "$configured" = false ]; then
    log_event "$root" hook.stop skip "verify.required_before_stop is on but no verify command is configured (commands.test/build/lint)" null
  else
    edit_ep=$(state_get "$root" last_edit_epoch); edit_ts=$(state_get "$root" last_edit_ts)
    ver_ep=$(state_get "$root" last_verify_epoch);  ver_ts=$(state_get "$root" last_verify_ts)
    ver_ok=$(state_get "$root" last_verify_ok);     ver_cmd=$(state_get "$root" last_verify_cmd)
    stale=""
    if [ -z "$ver_ep$ver_ts" ]; then stale=missing
    elif is_int "$ver_ep" && is_int "$edit_ep"; then [ "$ver_ep" -lt "$edit_ep" ] && stale=older
    elif [ -n "$edit_ts" ] && [ "$ver_ts" \< "$edit_ts" ]; then stale=older
    fi
    [ -z "$stale" ] && [ "$ver_ok" != "true" ] && stale=failed
    if [ -n "$stale" ]; then
      list=$(verify_commands_human "$root")
      case "$stale" in
        failed) reason="[sdlc] The last verify run failed (${ver_cmd:-?}). Verification is part of done: fix the code, not the test, then re-run the verify commands ($list) and paste their output before finishing." ;;
        *)      reason="[sdlc] Verification is part of done: run the verify commands ($list) and paste their output before finishing. If a test fails, fix the code, not the test." ;;
      esac
      block_stop "verify_$stale" "$reason"
    fi
  fi
fi
exit 0
