#!/bin/bash
# sdlc plugin — artifact chain status: intent → spec → plan per slug, active plan, spec rework after plan.
# Usage: status.sh [slug] [--dir <root>] [--help]      (tolerates a project without git: dates show "-")
set -o pipefail
. "$(dirname "$0")/lib/common.sh"
. "$(dirname "$0")/lib/scripts-extra.sh"

usage() {
  cat <<'EOF'
status.sh — one row per slug across intent/, spec/, plan/

  slug          show only this chain, with file paths and the list of spec commits after the first plan commit
  --dir <root>  project root (default: $CLAUDE_PROJECT_DIR, git toplevel, or cwd)
  --help

Columns: slug | intent (status, created, last commit) | spec | plan | active | rework
  rework = number of spec commits made after the first plan commit (spec changed while/after implementation was planned).
  active = the plan is the current .sdlc/state/active-plan.
EOF
}

SLUG=""; ROOT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) ROOT_ARG="$2"; shift 2;;
    --dir=*) ROOT_ARG="${1#*=}"; shift;;
    -h|--help) usage; exit 0;;
    -*) die "unknown option: $1 (see --help)";;
    *) SLUG="$1"; shift;;
  esac
done
ROOT=$(resolve_root "$ROOT_ARG")
INTENT_DIR=$(cfg "$ROOT" .paths.intent intent); SPEC_DIR=$(cfg "$ROOT" .paths.spec spec); PLAN_DIR=$(cfg "$ROOT" .paths.plan plan)
GIT=0; is_git_repo "$ROOT" && GIT=1
ACTIVE=$(active_plan_file "$ROOT")

# first_commit_ts <rel>  / last_commit_ts <rel>  → unix seconds ("" when untracked or no git)
first_commit_ts() { [ "$GIT" = 1 ] || return 0; git -C "$ROOT" log --format=%ct --reverse -- "$1" 2>/dev/null | head -1; }
last_commit_ts()  { [ "$GIT" = 1 ] || return 0; git -C "$ROOT" log -1 --format=%ct -- "$1" 2>/dev/null; }
fmt_date() { # epoch → YYYY-MM-DD on BSD (date -r) and GNU (date -d @) alike
  [ -n "$1" ] || { printf '%s' "-"; return; }
  date -u -r "$1" +%Y-%m-%d 2>/dev/null || date -u -d "@$1" +%Y-%m-%d 2>/dev/null || printf '%s' "-"
}
# commits_after <rel> <unix-ts> → count of commits touching rel with committer time > ts
commits_after() {
  [ "$GIT" = 1 ] && [ -n "$2" ] || { printf '0'; return; }
  git -C "$ROOT" log --format=%ct -- "$1" 2>/dev/null | awk -v t="$2" '$1>t{c++} END{print c+0}'
}
# cell <dir> <slug> → "status created lastcommit" or "-"
cell() {
  local f="$ROOT/$1/$2.md" st cr lc
  [ -f "$f" ] || { printf '%s' "-"; return; }
  st=$(frontmatter_get "$f" status); cr=$(frontmatter_get "$f" created); lc=$(fmt_date "$(last_commit_ts "$1/$2.md")")
  if [ "$GIT" = 1 ] && [ -z "$(last_commit_ts "$1/$2.md")" ]; then lc="uncommitted"; fi
  printf '%s %s %s' "${st:-?}" "${cr:--}" "$lc"
}

slugs=$( { artifact_slugs "$ROOT/$INTENT_DIR"; artifact_slugs "$ROOT/$SPEC_DIR"; artifact_slugs "$ROOT/$PLAN_DIR"; } | sort -u )
if [ -n "$SLUG" ]; then
  printf '%s\n' "$slugs" | grep -qx -- "$SLUG" || die "no intent/spec/plan named '$SLUG' under $INTENT_DIR/ $SPEC_DIR/ $PLAN_DIR/"
  slugs="$SLUG"
fi
if [ -z "$slugs" ]; then
  printf 'sdlc status — %s\nno artifacts yet (%s/ %s/ %s/). Start with /sdlc:intent.\n' "$ROOT" "$INTENT_DIR" "$SPEC_DIR" "$PLAN_DIR"
  exit 0
fi
[ "$GIT" = 1 ] || note "not a git repository — commit dates and rework counts are unavailable"

printf 'sdlc status — %s\n\n' "$ROOT"
printf '%-28s | %-34s | %-34s | %-34s | %-6s | %s\n' "slug" "intent (status created commit)" "spec" "plan" "active" "rework"
printf '%s\n' "$(printf '%0.s-' $(seq 1 160))"
for s in $slugs; do
  i=$(cell "$INTENT_DIR" "$s"); sp=$(cell "$SPEC_DIR" "$s"); pl=$(cell "$PLAN_DIR" "$s")
  act="-"; [ -n "$ACTIVE" ] && [ "$ACTIVE" = "$PLAN_DIR/$s.md" ] && act="yes"
  plan_first=$(first_commit_ts "$PLAN_DIR/$s.md")
  rw="-"
  if [ -f "$ROOT/$SPEC_DIR/$s.md" ] && [ -n "$plan_first" ]; then rw=$(commits_after "$SPEC_DIR/$s.md" "$plan_first"); fi
  printf '%-28s | %-34s | %-34s | %-34s | %-6s | %s\n' "$s" "$i" "$sp" "$pl" "$act" "$rw"
done

if [ -n "$SLUG" ]; then
  printf '\nfiles:\n'
  for d in "$INTENT_DIR" "$SPEC_DIR" "$PLAN_DIR"; do
    f="$d/$SLUG.md"
    if [ -f "$ROOT/$f" ]; then
      printf '  %-40s type=%s status=%s author=%s created=%s\n' "$f" "$(frontmatter_get "$ROOT/$f" type)" "$(frontmatter_get "$ROOT/$f" status)" "$(frontmatter_get "$ROOT/$f" author)" "$(frontmatter_get "$ROOT/$f" created)"
    else printf '  %-40s (missing)\n' "$f"; fi
  done
  if [ "$GIT" = 1 ]; then
    pf=$(first_commit_ts "$PLAN_DIR/$SLUG.md")
    if [ -n "$pf" ] && [ -f "$ROOT/$SPEC_DIR/$SLUG.md" ]; then
      printf '\nspec commits after the first plan commit (%s):\n' "$(fmt_date "$pf")"
      git -C "$ROOT" log --format='%ct %h %cs %s' -- "$SPEC_DIR/$SLUG.md" 2>/dev/null | awk -v t="$pf" '$1>t{ $1=""; sub(/^ /,""); print "  " $0 }'
    fi
    printf '\nhistory (all three files):\n'
    git -C "$ROOT" log --format='  %cs %h %s' -- "$INTENT_DIR/$SLUG.md" "$SPEC_DIR/$SLUG.md" "$PLAN_DIR/$SLUG.md" 2>/dev/null | head -30
  fi
fi

TRIAGE_DIR=$(cfg "$ROOT" .monitor.triage_dir "$INTENT_DIR/triage")
if [ -d "$ROOT/$TRIAGE_DIR" ]; then
  q=$(find "$ROOT/$TRIAGE_DIR" -maxdepth 1 -name '*.md' 2>/dev/null | grep -c . || true)
  printf '\ntriage queue: %s intent(s) in %s/%s\n' "${q:-0}" "$TRIAGE_DIR" "$([ "${q:-0}" -gt 0 ] && printf ' — /sdlc:triage')"
fi
[ -n "$ACTIVE" ] && printf 'active plan: %s\n' "$ACTIVE"
exit 0
