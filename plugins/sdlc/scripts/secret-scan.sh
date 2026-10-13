#!/bin/bash
# sdlc plugin — secret scanner (design §6).
#   secret-scan.sh --text [<root>]            scan text from stdin
#   secret-scan.sh --staged <root> [--all] [--untracked]
#                                             scan added lines of `git diff --cached`; every added line of every file counts, no directory is exempt
#                                             --all        …and `git diff HEAD` instead: the unstaged changes of tracked files too (what `commit -a` takes)
#                                             --untracked  …and the text files git neither tracks nor ignores (what `git add` could stage): at most
#                                                          200 files, 256 KB each, 1 MB in all; symlinks, binaries and anything .gitignore covers are skipped
# Patterns: scripts/secret-patterns.txt (+ secrets.extra_patterns from <root>/.sdlc/config.json when <root> is initialized).
# Output: one line per hit, "pattern#<n>\t<label>" — the matched secret itself is NEVER printed.
# Exit: 0 clean, 1 hit(s), 2 usage error. Allowlist tokens come from the pattern file header; template references (${…}, {{…}}) are also skipped.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib/common.sh"
. "$here/lib/hooks-extra.sh"

mode="${1:-}"; [ $# -gt 0 ] && shift
root="${1:-}"; [ $# -gt 0 ] && shift
all=false; untracked=false
case "$mode" in
  --text) ;;
  --staged) [ -n "$root" ] || { echo "usage: secret-scan.sh --staged <root> [--all] [--untracked]" >&2; exit 2; }
            for opt in "$@"; do
              case "$opt" in --all) all=true;; --untracked) untracked=true;; esac
            done ;;
  *) echo "usage: secret-scan.sh --text [<root>] | --staged <root> [--all] [--untracked]" >&2; exit 2 ;;
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

# untracked_text <root> → every line of the text files git neither tracks nor ignores, as if they were all added. Bounded: the
# first 200 files git lists, regular files only (a symlink is not followed), at most 256 KB each, binaries skipped, 1 MB of text in all.
untracked_text() {
  local root="$1" f size name sized=""
  local -a files=() ok=()
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$root/$f" ] && [ ! -L "$root/$f" ] && files[${#files[@]}]="./$f"
  done <<< "$(git -C "$root" -c core.quotepath=off ls-files -o --exclude-standard 2>/dev/null | head -n 200)"
  [ "${#files[@]}" -gt 0 ] || return 0
  sized=$(cd "$root" 2>/dev/null && wc -c "${files[@]}" 2>/dev/null)
  while read -r size name; do
    case "$size" in ''|*[!0-9]*) continue;; esac
    [ "$size" -le 262144 ] && [ -n "$name" ] && [ "$name" != total ] && ok[${#ok[@]}]="$name"
  done <<< "$sized"
  [ "${#ok[@]}" -gt 0 ] || return 0
  ( cd "$root" 2>/dev/null && LC_ALL=C grep -I -h -m 5000 '' "${ok[@]}" 2>/dev/null ) | head -c 1048576     # C locale: only a NUL byte makes a file binary, not a legacy encoding
}

# --- text to scan
if [ "$mode" = "--text" ]; then
  text=$(cat)
else
  git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
  if $all && git -C "$root" rev-parse --verify HEAD >/dev/null 2>&1; then
    diff=$(git -C "$root" diff HEAD --no-color --unified=0 --diff-filter=ACMR 2>/dev/null)
  else
    diff=$(git -C "$root" diff --cached --no-color --unified=0 --diff-filter=ACMR 2>/dev/null)
  fi
  # every added line of every file, no path exempt: a directory of fixtures is not a reason to wave real credentials through.
  # The per-file header (diff … up to the first @@) is skipped by state, so an added line that itself starts with "++ " is still scanned.
  text=$(printf '%s\n' "$diff" | awk '
    /^diff /   { hdr = 1; next }
    /^@@ /     { hdr = 0; next }
    hdr        { next }
    /^\+/      { print substr($0, 2) }')
  if $untracked; then text="$text"$'\n'"$(untracked_text "$root")"; fi
fi
[ -n "$(printf '%s' "$text" | tr -d '[:space:]')" ] || exit 0

# --- fast reject: one pass with every pattern at once. Text that matches none of them is clean; the rest goes through the loop
# below, which names the pattern and applies the allowlist. A pattern with a back-reference means something else inside the
# combination, and a grep error proves nothing — both skip this step.
combined=""; fast=true
while IFS= read -r pat; do
  [ -z "$pat" ] && continue
  case "$pat" in *'\1'*|*'\2'*|*'\3'*|*'\4'*|*'\5'*|*'\6'*|*'\7'*|*'\8'*|*'\9'*) fast=false;; esac
  combined="${combined:+$combined|}($pat)"
done <<< "$patterns"
if $fast && [ -n "$combined" ]; then
  printf '%s\n' "$text" | LC_ALL=C grep -E -i -q -e "$combined" 2>/dev/null
  [ $? -eq 1 ] && exit 0
fi

# --- scan: for each pattern, find matches, drop allowlisted ones, report the pattern (never the value)
rc=0; n=0
while IFS= read -r pat; do
  [ -z "$pat" ] && continue
  n=$((n + 1))
  matches=$(printf '%s\n' "$text" | LC_ALL=C grep -E -i -o -e "$pat" 2>/dev/null) || continue      # bytes, not characters: text in a legacy encoding is not "binary"
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
