#!/bin/bash
# SessionEnd — log session.end for concurrency/duration metrics. Removes nothing.
# Reads: hooks.log_events
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
sdlc_initialized "$root" || exit 0

reason=$(json_get "$input" .reason)
log_event "$root" session.end allow "${reason:-end}" null
state_set "$root" ended "$(now_iso)"
exit 0
