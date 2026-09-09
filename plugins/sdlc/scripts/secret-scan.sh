#!/bin/bash
# sdlc plugin — secret scanner (design §6).
#   secret-scan.sh --text [<root>]            scan text from stdin
#   secret-scan.sh --staged <root> [--all]    scan added lines of `git diff --cached` (with --all: `git diff HEAD`, what `commit -a` would take)
# Patterns: scripts/secret-patterns.txt (+ secrets.extra_patterns from <root>/.sdlc/config.json when <root> is initialized).
# Output: one line per hit, "pattern#<n>\t<label>" — the matched secret itself is NEVER printed.
# Exit: 0 clean, 1 hit(s), 2 usage error. Allowlist tokens come from the pattern file header; template references (${…}, {{…}}) are also skipped.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib/common.sh"
. "$here/lib/hooks-extra.sh"

mode="${1:-}"; [ $# -gt 0 ] && shift
root="${1:-}"; all=false
case "$mode" in
  --text) ;;
  --staged) [ -n "$root" ] || { echo "usage: secret-scan.sh --staged <root> [--all]" >&2; exit 2; }
            [ "${2:-}" = "--all" ] && all=true ;;
  *) echo "usage: secret-scan.sh --text [<root>] | --staged <root> [--all]" >&2; exit 2 ;;
esac

pattern_file="$here/secret-patterns.txt"
[ -f "$pattern_file" ] || pattern_file="$(sdlc_plugin_root)/scripts/secret-patterns.txt"

# --- allowlist: "# Allowlist tokens (never flagged): a, b, c" header line, plus template references
allow_args=(-e '${' -e '{{' -e '<%')
allow_line=$(grep -i 'allowlist tokens' "$pattern_file" 2>/dev/null | head -1 | sed -E 's/^[^:]*:[[:space:]]*//')
if [ -n "$allow_line" ]; then
  while IFS= read -r tok; do tok=$(trim "$tok"); [ -n "$tok" ] && allow_args+=(-e "$tok"); done <<< "$(printf '%s' "$allow_line" | tr ',' '\n')"
else
  allow_args+=(-e example -e REPLACE_ME -e changeme -e '<your-' -e xxxx -e dummy -e placeholder)
fi

# --- patterns: file (comments/blank skipped) + extra_patterns
patterns=""
while IFS= read -r line || [ -n "$line" ]; do
  line=$(trim "$line"); case "$line" in ''|'#'*) continue;; esac
  patterns="${patterns}${line}"$'\n'
done < "$pattern_file"
if [ -n "$root" ] && sdlc_initialized "$root"; then
  extra=$(cfg_lines "$root" .secrets.extra_patterns)
  [ -n "$extra" ] && patterns="${patterns}${extra}"$'\n'
fi

# --- text to scan
if [ "$mode" = "--text" ]; then
  text=$(cat)
else
  git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
  evals_dir=$(cfg "$root" .paths.evals evals)
  if $all && git -C "$root" rev-parse --verify HEAD >/dev/null 2>&1; then
    diff=$(git -C "$root" diff HEAD --no-color --unified=0 --diff-filter=ACMR 2>/dev/null)
  else
    diff=$(git -C "$root" diff --cached --no-color --unified=0 --diff-filter=ACMR 2>/dev/null)
  fi
  # added lines only; files under the evals fixture dir are exempt (they hold intentionally fake secrets)
  text=$(printf '%s\n' "$diff" | awk -v skip="${evals_dir%/}/" '
    /^\+\+\+ / { f=$0; sub(/^\+\+\+ b\//,"",f); cur=f; next }
    /^\+/      { if (index(cur, skip) == 1) next; print substr($0, 2) }')
fi
[ -n "$(printf '%s' "$text" | tr -d '[:space:]')" ] || exit 0

# --- scan: for each pattern, find matches, drop allowlisted ones, report the pattern (never the value)
rc=0; n=0
while IFS= read -r pat; do
  [ -z "$pat" ] && continue
  n=$((n + 1))
  matches=$(printf '%s\n' "$text" | grep -E -i -o -e "$pat" 2>/dev/null) || continue
  hit=false
  while IFS= read -r m; do
    [ -z "$m" ] && continue
    printf '%s\n' "$m" | grep -qiF "${allow_args[@]}" 2>/dev/null && continue
    hit=true; break
  done <<< "$matches"
  if $hit; then
    label=$(printf '%s' "$pat" | tr '\t' ' '); label=$(trunc "$label" 40)
    printf 'pattern#%s\t%s\n' "$n" "$label"
    rc=1
  fi
done <<< "$patterns"
exit $rc
