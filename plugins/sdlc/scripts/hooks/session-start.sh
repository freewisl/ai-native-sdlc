#!/bin/bash
# SessionStart — log the session, record it in .sdlc/state, and inject a compact SDLC context block (design §3).
# Reads: hooks.session_context, hooks.log_events, paths.intent, paths.plan, verify.*, commands.*, protect.*, plan.require_for_edits, environments.production.*
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
plan_line="none — start with /sdlc:plan"
plan_rel=$(active_plan_file "$root")
if [ -n "$plan_rel" ]; then
  if [ -f "$root/$plan_rel" ]; then
    slug=$(frontmatter_get "$root/$plan_rel" slug); status=$(frontmatter_get "$root/$plan_rel" status)
    plan_line="$plan_rel (slug: ${slug:-?}, status: ${status:-?})"
  else
    plan_line="$plan_rel (file missing — .sdlc/state/active-plan is stale)"
  fi
fi
intent_dir=$(cfg "$root" .paths.intent intent)
triage_n=0
[ -d "$root/$intent_dir/triage" ] && triage_n=$(find "$root/$intent_dir/triage" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
verify_line=$(verify_commands_human "$root")
[ "$(cfg_bool "$root" .verify.required_before_stop false)" = "true" ] && verify_line="$verify_line (required before stop)"

guards=""
frozen_file=$(cfg "$root" .protect.frozen_paths_file ".sdlc/frozen-paths.txt"); case "$frozen_file" in /*) ;; *) frozen_file="$root/$frozen_file";; esac
frozen_n=0; [ -f "$frozen_file" ] && frozen_n=$(grep -Ev '^[[:space:]]*(#|$)' "$frozen_file" 2>/dev/null | wc -l | tr -d ' ')
guards="frozen paths: $frozen_n entr$( [ "$frozen_n" = 1 ] && printf 'y' || printf 'ies')"
if [ "$(cfg_bool "$root" .protect.protect_tests true)" = "true" ]; then
  if [ "${SDLC_ALLOW_TEST_EDIT:-}" = "1" ] || [ -f "$(state_dir "$root")/allow-test-edit" ]; then
    guards="$guards · test edits: UNLOCKED for this fix"
  else
    guards="$guards · test files locked during fixes (unlock only when the test itself must change: plan test_changes: allowed, or /sdlc:verify --bugfix)"
  fi
fi
[ "$(cfg_bool "$root" .plan.require_for_edits false)" = "true" ] && guards="$guards · edits need an approved plan"
prod_env=$(cfg "$root" .environments.production.approval_env RELEASE_APPROVAL)
[ "$(cfg "$root" .environments.production.autonomy gated)" = "gated" ] && guards="$guards · production commands gated ($prod_env)"

# --- emit (≤ 12 lines; stdout is added to Claude's context)
printf '%s\n' "[sdlc] sdlc plugin v$ver — AI-native SDLC loop is active in this project (.sdlc/config.json)."
printf '%s\n' "Active plan: $plan_line"
printf '%s\n' "Pending intents ($intent_dir/triage/): $triage_n"
printf '%s\n' "Verify commands: $verify_line"
printf '%s\n' "Guards: $guards"
printf '%s\n' "Rules:"
printf '%s\n' "- Nothing is implemented without an approved plan — start with /sdlc:plan"
printf '%s\n' "- Verify before reporting done (run the Commands in CLAUDE.md and paste output; never edit tests during a fix)"
printf '%s\n' "- Same mistake twice → /sdlc:lesson adds it to CLAUDE.md"
exit 0
