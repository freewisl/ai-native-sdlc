#!/bin/bash
# github-setup.sh — the GitHub side of an sdlc repository, done with `gh` instead of clicking:
#   1. repository settings: auto-merge allowed (a person can queue a PR to merge when its checks pass), merged branches deleted
#   2. a branch ruleset on the default branch: pull request required (stale approvals dismissed, review threads resolved,
#      code-owner review when the plan supports it), no force pushes, no deletion, and the status checks named with --checks.
#      A solo repository needs no approval, so its owner — or the loop — can merge their own PRs.
# The loop runs Claude on this machine: there is no CI secret to set, and no status check is required unless you ask for one.
#
# Usage: github-setup.sh [--login] [--repo owner/name] [--approvals N] [--checks "ci,lint"] [--bypass | --no-bypass]
#                        [--ruleset-name NAME] [--dry-run] [--dir ROOT]
#   --login       run `gh auth login --web` for this repository's host without a terminal: prints the one-time code and the
#                 URL (an agent with a browser tool can finish it) and waits until gh is authenticated. gh keeps the
#                 credential; this script never reads or prints a token.
#   --repo        owner/name (default: read from the checkout through gh)
#   --approvals   approving reviews required to merge, 0-10 (default: 0 for a solo repository, 1 otherwise)
#   --checks      status checks that must pass before a merge, comma separated (default: none)
#   --bypass      let repository admins merge a PR past the rules, a failing required check say (default: off)
#   --no-bypass   no admin bypass, even when --bypass is given
#   --dry-run     print what would change, with the ruleset payload; change nothing
# A repository is solo when .sdlc/config.json says `mode: solo` or it has one collaborator; collaborators gh cannot count
# are treated as a team (the stricter rules). Needs gh with admin rights on the repository.
set -u
here="$(cd "$(dirname "$0")" && pwd)"; . "$here/lib/common.sh"; . "$here/lib/scripts-extra.sh"   # git_remote_host, resolve_root

usage() { sed -n '2,/^set -u/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; }
need() { [ "$#" -ge 2 ] || { printf '%s needs a value\n' "$1" >&2; usage >&2; exit 2; }; }   # need "$@": the option has its value
repo=""; approvals=""; checks=""; want_bypass=0; no_bypass=0; rs_name="sdlc: protect default branch"; dry=0; root=""; do_login=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) need "$@"; repo="$2"; shift 2;;
    --approvals) need "$@"; approvals="$2"; shift 2;;
    --checks) need "$@"; checks="$2"; shift 2;;
    --ruleset-name) need "$@"; rs_name="$2"; shift 2;;
    --dir) need "$@"; root="$2"; shift 2;;
    --bypass) want_bypass=1; shift;;
    --no-bypass) no_bypass=1; shift;;
    --login) do_login=1; shift;;
    --dry-run) dry=1; shift;;
    --auth|--token-stdin|--token-env|--save-token|--save-token-file)
      printf '%s was removed in 1.0: the loop runs claude on this machine, so there is no CI secret to set.\n' "$1" >&2; exit 2;;
    -h|--help) usage; exit 0;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2;;
  esac
done
case "$approvals" in ''|[0-9]|10) ;; *) echo "--approvals must be a number from 0 to 10" >&2; exit 2;; esac
case "$repo" in ''|*/*) ;; *) echo "--repo must be owner/name" >&2; exit 2;; esac
root=$(resolve_root "$root") || exit 2   # a --dir that is not a directory

if ! has_cmd gh; then
  cat >&2 <<'EOF'
gh (GitHub CLI) is not installed. Install it (macOS: brew install gh · Linux: https://cli.github.com), run `gh auth login`,
then re-run this script. Until then, do it by hand: repository Settings → General → allow auto-merge and delete merged branches,
and Settings → Rules → Rulesets → New branch ruleset (pull request required, no force pushes, restrict deletions).
EOF
  exit 2
fi
# ---------- 0. gh login (device flow, non-interactive start) ----------
gh_host=$(git_remote_host "$root"); export GH_HOST="$gh_host"   # scoped: other hosts' expired logins must not fail this repository
if ! gh auth status --hostname "$gh_host" >/dev/null 2>&1; then
  if [ "$do_login" = 1 ] && [ "$dry" = 0 ]; then
    tmpout=$(mktemp); ( printf '\n' | gh auth login --web -h "$gh_host" --git-protocol https --skip-ssh-key > "$tmpout" 2>&1 ) &
    lp=$!; for i in 1 2 3 4 5 6 7 8 9 10; do grep -q 'one-time code' "$tmpout" 2>/dev/null && break; sleep 1; done
    code=$(grep -o '[A-Z0-9]\{4\}-[A-Z0-9]\{4\}' "$tmpout" | head -1)
    echo "LOGIN   GitHub device flow started. Open https://$gh_host/login/device and enter the code: ${code:-see below}"
    echo "LOGIN   SDLC_DEVICE_CODE=${code}  SDLC_DEVICE_URL=https://$gh_host/login/device"
    echo "LOGIN   waiting up to 5 minutes for the approval…"
    for i in $(seq 1 150); do gh auth status --hostname "$gh_host" >/dev/null 2>&1 && break; kill -0 "$lp" 2>/dev/null || break; sleep 2; done
    gh auth status --hostname "$gh_host" >/dev/null 2>&1 && echo "LOGIN   gh authenticated ($gh_host)" || { echo "LOGIN   not completed — rerun with --login when ready" >&2; tail -3 "$tmpout" >&2; rm -f "$tmpout"; exit 2; }
    rm -f "$tmpout"
  elif [ "$dry" = 0 ]; then echo "gh is not logged in to $gh_host — run: gh auth login --hostname $gh_host   (or: $0 --login)" >&2; exit 2
  else echo "NOTE    gh is not authenticated — dry run shows the payload only; run --login first for a real run" >&2; [ -z "$repo" ] && { echo "        (and pass --repo owner/name, it cannot be read from gh while logged out)" >&2; exit 2; }; fi
fi

# ---------- repository facts ----------
if [ -z "$repo" ]; then repo=$(cd "$root" && gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null); fi
if [ -z "$repo" ]; then
  if git -C "$root" remote get-url origin >/dev/null 2>&1; then echo "cannot determine the repository — pass --repo owner/name (is origin a GitHub repository?)" >&2
  else echo "this checkout has no origin remote: the loop merges on this machine and there is nothing to set up on GitHub (pass --repo owner/name to set a repository up anyway)" >&2; fi
  exit 2
fi
info=$(gh repo view "$repo" --json isPrivate,defaultBranchRef,owner,visibility 2>/dev/null || echo '{}')
default_branch=$(json_get "$info" .defaultBranchRef.name); [ -z "$default_branch" ] && default_branch=main
is_private=$(json_get "$info" .isPrivate); [ -z "$is_private" ] && is_private=false
solo=0; solo_why="team"
collaborators=$(gh api "repos/$repo/collaborators?per_page=100" --jq 'length' 2>/dev/null | tr -cd '0-9')
if [ -n "$collaborators" ] && [ "$collaborators" -le 1 ]; then solo=1; solo_why="one collaborator"
elif [ -z "$collaborators" ]; then collaborators="unknown"; fi   # unknown ≠ one: keep team gates (fail closed)
[ "$(cfg "$root" .mode "")" = solo ] && { solo=1; solo_why="mode solo"; }   # an explicit mode in the config counts
[ -z "$approvals" ] && { if [ "$solo" = 1 ]; then approvals=0; else approvals=1; fi; }
printf '== github-setup  repo=%s  default=%s  private=%s  collaborators=%s  solo=%s (%s)%s\n' "$repo" "$default_branch" "$is_private" "$collaborators" "$solo" "$solo_why" "$([ "$dry" = 1 ] && printf '  (dry run)')"

# ---------- 1. repository settings (before the ruleset: rulesets can be unavailable on the plan, these never are) ----------
# `gh pr merge --auto` is accepted only with allow_auto_merge AND a protected base branch; the setting costs nothing either way.
if [ "$dry" = 1 ]; then echo "REPO    would set allow_auto_merge=true, delete_branch_on_merge=true"
elif gh api -X PATCH "repos/$repo" -F allow_auto_merge=true -F delete_branch_on_merge=true >/dev/null 2>&1; then echo "REPO    allow_auto_merge=true, delete_branch_on_merge=true"
else echo "REPO    could not enable allow_auto_merge / delete_branch_on_merge (permission) — set them under Settings → General" >&2; fi

# ---------- 2. ruleset ----------
existing=$(gh api "repos/$repo/rulesets" --jq ".[] | select(.name==$(json_escape "$rs_name")) | .id" 2>/dev/null | head -1)
checks_json=$(printf '%s' "$checks" | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' | while IFS= read -r c; do printf '{"context":%s},' "$(json_escape "$c")"; done); checks_json="[${checks_json%,}]"
bypass_json='[]'   # default: no bypass, so a required check stays enforced even for the owner (approvals 0 already lets a solo owner merge)
[ "$want_bypass" = 1 ] && [ "$no_bypass" = 0 ] && bypass_json='[{"actor_id":5,"actor_type":"RepositoryRole","bypass_mode":"pull_request"}]'   # --bypass: an admin skips ALL rules on PRs
rsc_rule=""; [ "$checks_json" != "[]" ] && rsc_rule=",
    { \"type\": \"required_status_checks\", \"parameters\": { \"strict_required_status_checks_policy\": false, \"required_status_checks\": $checks_json } }"
payload() { # payload <code_owner:true|false>
  cat <<EOF
{
  "name": $(json_escape "$rs_name"),
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "bypass_actors": $bypass_json,
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "pull_request", "parameters": { "required_approving_review_count": $approvals, "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": $1, "require_last_push_approval": false, "required_review_thread_resolution": true } }$rsc_rule
  ]
}
EOF
}
checks_note() { [ -z "$checks" ] || echo "NOTE    a required check that no workflow reports blocks every merge — name only checks your workflows really run."; }
if [ "$dry" = 1 ]; then echo "RULESET would $([ -n "$existing" ] && printf 'update #%s' "$existing" || printf create) '$rs_name' on $default_branch (code-owner review: $([ "$solo" = 1 ] && printf no || printf yes)):"; payload $([ "$solo" = 1 ] && printf false || printf true) | sed 's/^/        /'; checks_note; exit 0; fi
apply() { # apply <code_owner> → prints api response or fails
  if [ -n "$existing" ]; then payload "$1" | gh api -X PUT "repos/$repo/rulesets/$existing" --input - 2>&1
  else payload "$1" | gh api -X POST "repos/$repo/rulesets" --input - 2>&1; fi
}
summary() { # summary <code_owner:true|false> → what the ruleset enforces
  printf 'PR required (approvals %s)%s + review-thread resolution%s + no force-push/deletion%s' "$approvals" "$([ "$1" = true ] && printf ' + code-owner review')" \
    "$([ -n "$checks" ] && printf " + status checks '%s'" "$checks")" "$([ "$bypass_json" != '[]' ] && printf ' + admin bypass for PRs (--bypass)')"
}
first=true; [ "$solo" = 1 ] && first=false
out=$(apply $first); rc=$?
if [ $rc -ne 0 ] && [ "$first" = true ]; then
  # Code-owner review is unavailable on personal Free-plan private repos; retry without it before giving up.
  out2=$(apply false); rc2=$?
  if [ $rc2 -eq 0 ]; then echo "RULESET '$rs_name' applied WITHOUT code-owner review (not available for this repository plan/owner type): $(summary false)"
  else
    echo "RULESET failed:" >&2; printf '%s\n' "$out2" | head -5 >&2
    echo "        Rulesets on private repositories need GitHub Pro/Team/Enterprise (public repositories: free). Create the rule by hand or make the repo public." >&2; exit 1
  fi
elif [ $rc -ne 0 ]; then
  echo "RULESET failed:" >&2; printf '%s\n' "$out" | head -5 >&2
  echo "        Rulesets on private repositories need GitHub Pro/Team/Enterprise (public repositories: free). The plugin's push guard (protect.block_direct_push) still keeps the agent off the default branch." >&2; exit 1
else
  echo "RULESET '$rs_name' applied: $(summary "$first")"
fi
checks_note
exit 0
