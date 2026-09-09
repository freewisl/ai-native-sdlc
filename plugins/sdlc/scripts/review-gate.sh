#!/bin/bash
# review-gate.sh — turn the reviewer's machine-readable tally into a merge check (playbook Stage 5: "a platform engineer
# who wants to gate merges on findings can read the severity counts that the check run publishes").
# Reads the review text (file or stdin), finds the last `SDLC_REVIEW_TALLY {...}` line and exits 1 when important > --max-important.
# Findings never approve a PR; this only lets CI *block* on Important counts. Usage: review-gate.sh [review.md] [--max-important N] [--max-nits N]
set -u
here="$(cd "$(dirname "$0")" && pwd)"; . "$here/lib/common.sh"
file=""; max_imp=0; max_nits=""
while [ $# -gt 0 ]; do
  case "$1" in
    --max-important) max_imp="$2"; shift 2;;
    --max-nits) max_nits="$2"; shift 2;;
    -h|--help) sed -n '2,6p' "$0"; exit 0;;
    *) file="$1"; shift;;
  esac
done
text=$( [ -n "$file" ] && cat "$file" || cat )
line=$(printf '%s\n' "$text" | grep -E 'SDLC_REVIEW_TALLY[[:space:]]*\{' | tail -1)
[ -z "$line" ] && { echo "review-gate: no SDLC_REVIEW_TALLY line found — the reviewer did not publish a tally (treat as not reviewed)"; exit 2; }
json=${line#*SDLC_REVIEW_TALLY}; json=$(printf '%s' "$json" | sed 's/^[[:space:]]*//')
imp=$(json_get "$json" .important); nits=$(json_get "$json" .nits)
printf 'review-gate: important=%s nits=%s (max important %s%s)\n' "${imp:-?}" "${nits:-?}" "$max_imp" "${max_nits:+, max nits $max_nits}"
rc=0
[ -n "$imp" ] && [ "$imp" -gt "$max_imp" ] 2>/dev/null && { echo "review-gate: FAIL — $imp Important finding(s) exceed $max_imp"; rc=1; }
[ -n "$max_nits" ] && [ -n "$nits" ] && [ "$nits" -gt "$max_nits" ] 2>/dev/null && { echo "review-gate: FAIL — $nits nits exceed $max_nits"; rc=1; }
[ $rc -eq 0 ] && echo "review-gate: PASS"
exit $rc
