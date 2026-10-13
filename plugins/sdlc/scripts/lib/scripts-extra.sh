#!/bin/bash
# sdlc plugin — the two helpers scripts/github-setup.sh needs beyond lib/common.sh: resolve_root and git_remote_host.
# Source AFTER common.sh:  . "$here/lib/common.sh"; . "$here/lib/scripts-extra.sh"
# bash 3.2 compatible (macOS).

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# resolve_root [dir]  → absolute project root: --dir, else $CLAUDE_PROJECT_DIR, else git toplevel of cwd, else cwd
resolve_root() {
  local d="${1:-}"
  if [ -n "$d" ]; then
    [ -d "$d" ] || die "--dir '$d' is not a directory"
    (cd "$d" && pwd)
    return
  fi
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "$CLAUDE_PROJECT_DIR" ]; then printf '%s' "$CLAUDE_PROJECT_DIR"; return; fi
  local top; top=$(git rev-parse --show-toplevel 2>/dev/null || true)
  printf '%s' "${top:-$(pwd)}"
}

# git_remote_host <root> → host of the origin remote (git@host:…, ssh://…, https://…); github.com when there is none
git_remote_host() {
  local u; u=$(git -C "$1" remote get-url origin 2>/dev/null)
  case "$u" in
    git@*:*) u="${u#git@}"; printf '%s' "${u%%:*}";;
    ssh://*|https://*|http://*) u="${u#*://}"; u="${u#*@}"; u="${u%%/*}"; printf '%s' "${u%%:*}";;
    *) printf 'github.com';;
  esac
}
