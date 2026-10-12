#!/bin/bash
# SessionStart — log the session, record it in .sdlc/state, and inject a compact SDLC context block.
# Reads: hooks.session_context, hooks.log_events, verify.*, commands.*, protect.*, paths.work, the active work doc
# (.sdlc/state/active-work, see lib/common.sh) and the loop engine's .sdlc/state/loop.pid + loop-status.json.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
source=$(json_get "$input" .source); [ -z "$source" ] && source=$(json_get "$input" .session_start_type)

if ! sdlc_initialized "$root"; then
  printf '%s\n' "[sdlc] This project has no .sdlc/config.json — run /sdlc:init to adopt the AI-native SDLC loop (safe for existing projects)."
  exit 0
fi

log_event "$root" session.start allow "${source:-startup}" "{\"cwd\":$(json_escape "$root")}"
state_set_many "$root" started "$(now_iso)" source "${source:-startup}"

[ "$(cfg_bool "$root" .hooks.session_context true)" = "true" ] || exit 0

# --- gather
ver=$(sdlc_plugin_version)
wdir=$(work_dir "$root")

work_line="none — /sdlc:go \"<request>\" starts one"
status=""
slug=$(active_work_slug "$root")
if [ -n "$slug" ]; then
  doc=$(active_work_file "$root")
  if [ -n "$doc" ]; then
    dslug=$(frontmatter_get "$root/$doc" slug); status=$(frontmatter_get "$root/$doc" status)
    work_line="$doc (slug: ${dslug:-$slug}, status: ${status:-?})"
  else
    work_line="$wdir/$slug.md — file missing (.sdlc/state/active-work is stale)"
  fi
fi

loop_line=""
if [ -f "$root/.sdlc/state/loop.pid" ]; then
  pid=""; read -r pid _ < "$root/.sdlc/state/loop.pid" || true
  if is_int "$pid" && [ "$pid" -gt 0 ] 2>/dev/null && kill -0 "$pid" 2>/dev/null; then
    lf="$root/.sdlc/state/loop-status.json"
    lstate=$(json_file_get "$lf" .state); litem=$(json_file_get "$lf" .item); lphase=$(json_file_get "$lf" .phase)
    loop_line="${lstate:-running}${litem:+ — $litem}${lphase:+ ($lphase)}, pid $pid — /sdlc:status shows the details"
  fi
fi

verify_line=$(verify_commands_human "$root")
[ "$(cfg_bool "$root" .verify.required_before_stop false)" = "true" ] && verify_line="$verify_line (required before stop)"

frozen_file=$(cfg "$root" .protect.frozen_paths_file ".sdlc/frozen-paths.txt"); case "$frozen_file" in /*) ;; *) frozen_file="$root/$frozen_file";; esac
frozen_n=0; [ -f "$frozen_file" ] && frozen_n=$(grep -Ev '^[[:space:]]*(#|$)' "$frozen_file" 2>/dev/null | wc -l | tr -d ' ')
guards="frozen paths: $frozen_n entr$( [ "$frozen_n" = 1 ] && printf 'y' || printf 'ies')"
if [ "$(cfg_bool "$root" .protect.protect_tests true)" = "true" ]; then
  if test_edit_allowed "$root"; then
    guards="$guards · existing test files: UNLOCKED for this change"
  else
    guards="$guards · existing test files locked, new ones are free (unlock: test_changes: allowed in the work doc, decided in the design)"
  fi
fi
case "$status" in ready|proposed) guards="$guards · design first: edits outside $wdir/ wait until the work doc is planned";; esac
deploy_gate_config "$root"
[ -n "$SDLC_DEPLOY_PATTERNS" ] && guards="$guards · production deploys gated ($SDLC_DEPLOY_ENV)"

# --- emit (≤ 12 lines; stdout is added to Claude's context)
printf '%s\n' "[sdlc] sdlc plugin v$ver — AI-native SDLC loop is active in this project (.sdlc/config.json)."
printf '%s\n' "Use: /sdlc:go \"<request>\" for one change · /sdlc:run \"<goal>\" for a goal or the queue · /sdlc:status for progress and what needs you"
printf '%s\n' "Active work: $work_line"
[ -n "$loop_line" ] && printf '%s\n' "Loop: $loop_line"
printf '%s\n' "Verify commands: $verify_line"
printf '%s\n' "Guards: $guards"
printf '%s\n' "Rules:"
printf '%s\n' "- One work doc per change ($wdir/<slug>.md): design first (Plan and Proof, status planned), then build"
printf '%s\n' "- Verify before reporting done (run the Commands in CLAUDE.md and paste output); fix the code, not the test"
exit 0
