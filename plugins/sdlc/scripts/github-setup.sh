#!/bin/bash
# github-setup.sh — the GitHub-side infrastructure the playbook assumes, done with `gh` instead of clicking:
#   1. the CI auth secret (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, or ANTHROPIC_API_KEY)
#   2. a branch ruleset on the default branch: PR required, review-thread resolution, code-owner review when the plan
#      supports it, required status check `evals` (the Agent evals workflow), block force pushes, restrict deletions,
#      and — for a solo repository — an admin bypass for pull requests so the owner can merge their own PRs.
# Playbook basis: Stage 5 "Branch protection policies that require a code owner's approval are also worthwhile";
# "Branch protection turns anything the agent writes into a PR, with no direct path to main"; evals need a key with budget.
#
# Usage: github-setup.sh [--login] [--repo owner/name] [--auth oauth|api] [--token-stdin | --token-env VAR | --save-token]
#                        [--approvals N] [--checks "evals,other"] [--no-bypass] [--ruleset-name NAME] [--dry-run] [--dir ROOT]
# Token resolution when neither --token-stdin nor --token-env is given (so later repositories need zero steps):
#   1) env CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY  2) macOS keychain item `sdlc-ci-token`  3) file $SDLC_CI_TOKEN_FILE
#   (default ${XDG_CONFIG_HOME:-~/.config}/sdlc/ci-token, mode 600).  --save-token stores the token you pass into 2) or 3).
# --login runs `gh auth login --web` non-interactively: prints the one-time code + URL (an agent with a browser tool can finish it)
#   and waits until gh is authenticated.  Needs: gh with admin rights on the repo. Never prints the token.
set -u
here="$(cd "$(dirname "$0")" && pwd)"; . "$here/lib/common.sh"; . "$here/lib/scripts-extra.sh"   # git_remote_host / gh_ready

usage() { sed -n '2,/^set -u/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; }
repo=""; auth="${SDLC_AUTH:-}"; token_src=""; token_env=""; approvals=""; checks="evals"; bypass=1; rs_name="sdlc: protect default branch"; dry=0; root=""; do_login=0; save_token=0; save_file=0; want_bypass=0
token_file="${SDLC_CI_TOKEN_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/sdlc/ci-token}"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="$2"; shift 2;;
    --auth) auth="$2"; shift 2;;
    --token-stdin) token_src=stdin; shift;;
    --token-env) token_src=env; token_env="$2"; shift 2;;
    --save-token) save_token=1; shift;;
    --save-token-file) save_token=1; save_file=1; shift;;
    --bypass) want_bypass=1; shift;;
    --login) do_login=1; shift;;
    --approvals) approvals="$2"; shift 2;;
    --checks) checks="$2"; shift 2;;
    --no-bypass) bypass=0; shift;;
    --ruleset-name) rs_name="$2"; shift 2;;
    --dry-run) dry=1; shift;;
    --dir) root="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2;;
  esac
done
[ -z "$root" ] && root=$(project_root "")
[ -z "$auth" ] && auth=$(cfg "$root" .ci.auth api)   # same source init.sh rendered into the workflows
case "$auth" in oauth) secret_name=CLAUDE_CODE_OAUTH_TOKEN;; api) secret_name=ANTHROPIC_API_KEY;; *) echo "--auth must be oauth or api" >&2; exit 2;; esac

if ! has_cmd gh; then
  cat >&2 <<'EOF'
gh (GitHub CLI) is not installed. Install it (macOS: brew install gh · Linux: https://cli.github.com), run `gh auth login`,
then re-run this script. Until then, do the two steps by hand: repository Settings → Secrets → Actions → CLAUDE_CODE_OAUTH_TOKEN
(value from `claude setup-token`), and Settings → Rules → Rulesets → New branch ruleset (PR required, status check `evals`).
EOF
  exit 2
fi
# ---------- 0. gh login (device flow, non-interactive start) ----------
gh_host=$(git_remote_host "${root:-$(project_root "")}"); export GH_HOST="$gh_host"   # scoped: other hosts' expired logins must not fail this repository
if ! gh auth status --hostname "$gh_host" >/dev/null 2>&1; then
  if [ "$do_login" = 1 ] && [ "$dry" = 0 ]; then
    tmpout=$(mktemp); ( printf '\n' | gh auth login --web -h "$gh_host" --git-protocol https --skip-ssh-key > "$tmpout" 2>&1 ) &
    lp=$!; for i in 1 2 3 4 5 6 7 8 9 10; do grep -q 'one-time code' "$tmpout" 2>/dev/null && break; sleep 1; done
    code=$(grep -o '[A-Z0-9]\{4\}-[A-Z0-9]\{4\}' "$tmpout" | head -1)
    echo "LOGIN   GitHub device flow started. Open https://$gh_host/login/device and enter the code: ${code:-see below}"
    echo "LOGIN   SDLC_DEVICE_CODE=${code}  SDLC_DEVICE_URL=https://$gh_host/login/device"
    echo "LOGIN   waiting up to 5 minutes for the approval…"
    for i in $(seq 1 150); do gh auth status --hostname "$gh_host" >/dev/null 2>&1 && break; kill -0 "$lp" 2>/dev/null || break; sleep 2; done
    gh auth status --hostname "$gh_host" >/dev/null 2>&1 && echo "LOGIN   gh authenticated ($gh_host)" || { echo "LOGIN   not completed — rerun with --login when ready" >&2; cat "$tmpout" | tail -3 >&2; rm -f "$tmpout"; exit 2; }
    rm -f "$tmpout"
  elif [ "$dry" = 0 ]; then echo "gh is not logged in to $gh_host — run: gh auth login --hostname $gh_host   (or: $0 --login)" >&2; exit 2
  else echo "NOTE    gh is not authenticated — dry run shows the payload only; run --login first for a real run" >&2; [ -z "$repo" ] && { echo "        (and pass --repo owner/name, it cannot be read from gh while logged out)" >&2; exit 2; }; fi
fi

# ---------- repository facts ----------
if [ -z "$repo" ]; then repo=$(cd "$root" && gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null); fi
[ -z "$repo" ] && { echo "cannot determine the repository — pass --repo owner/name (is the cwd a GitHub checkout?)" >&2; exit 2; }
info=$(gh repo view "$repo" --json isPrivate,defaultBranchRef,owner,visibility 2>/dev/null || echo '{}')
default_branch=$(json_get "$info" .defaultBranchRef.name); [ -z "$default_branch" ] && default_branch=main
is_private=$(json_get "$info" .isPrivate); [ -z "$is_private" ] && is_private=false
collaborators=$(gh api "repos/$repo/collaborators?per_page=100" --jq 'length' 2>/dev/null | tr -cd '0-9')
solo=0
if [ -n "$collaborators" ] && [ "$collaborators" -le 1 ]; then solo=1
elif [ -z "$collaborators" ]; then collaborators="unknown"; fi   # unknown ≠ one: keep team gates (fail closed)
cfg_solo=$(cfg "$root" .roles.solo false); [ "$cfg_solo" = "true" ] && solo=1   # an explicit roles.solo in config counts
[ -z "$approvals" ] && { if [ "$solo" = 1 ]; then approvals=0; else approvals=1; fi; }
printf '== github-setup  repo=%s  default=%s  private=%s  collaborators=%s  solo=%s  auth=%s%s\n' "$repo" "$default_branch" "$is_private" "$collaborators" "$solo" "$auth" "$([ "$dry" = 1 ] && printf '  (dry run)')"

# ---------- 1. secret ----------
token=""
if [ -z "$token_src" ]; then   # auto: env → keychain → file
  token="${!secret_name:-}"; [ -n "$token" ] && token_src="env:$secret_name"
  if [ "$auth" = oauth ]; then   # the stored token comes from `claude setup-token` — a subscription token, meaningless as an API key
    if [ -z "$token" ] && has_cmd security; then token=$(security find-generic-password -a sdlc -s sdlc-ci-token -w 2>/dev/null || true); [ -n "$token" ] && token_src=keychain; fi
    if [ -z "$token" ] && [ -f "$token_file" ]; then token=$(cat "$token_file"); [ -n "$token" ] && token_src="file:$token_file"; fi
  elif [ -z "$token" ] && { [ -f "$token_file" ] || { has_cmd security && security find-generic-password -a sdlc -s sdlc-ci-token >/dev/null 2>&1; }; }; then
    echo "NOTE    a stored subscription token exists but ci.auth=api — not used as ANTHROPIC_API_KEY (set ci.auth to oauth, or --auth oauth, to use it)"
  fi
  [ -n "$token" ] && echo "TOKEN   using stored token ($token_src)"
fi
if [ -n "$token_src" ]; then
  if [ "$token_src" = stdin ]; then token=$(cat); elif [ "$token_src" = env ]; then token="${!token_env:-}"; fi
  token=$(printf '%s' "$token" | tr -d '\r\n')
  if [ -n "$token" ] && [ "$save_token" = 1 ] && [ "$dry" = 0 ]; then
    if [ "$save_file" = 0 ] && has_cmd security; then security add-generic-password -U -a sdlc -s sdlc-ci-token -w "$token" >/dev/null 2>&1 && echo "TOKEN   saved to macOS keychain (sdlc-ci-token) — later repositories need no token step"
    else mkdir -p "$(dirname "$token_file")" && (umask 077; printf '%s' "$token" > "$token_file") && echo "TOKEN   saved to $token_file (mode 600) — later repositories need no token step"; fi
  fi
  if [ -z "$token" ]; then
    if [ "$dry" = 1 ]; then echo "SECRET  would set $secret_name (no token supplied in this dry run)"; token_src=""
    else echo "SECRET  $secret_name: no token received (empty stdin / env)" >&2; exit 2; fi
  fi
  if [ -z "$token_src" ]; then :
  elif [ "$dry" = 1 ]; then echo "SECRET  would set $secret_name (length ${#token})"
  else printf '%s' "$token" | gh secret set "$secret_name" --repo "$repo" && echo "SECRET  set $secret_name (length ${#token})" || { echo "SECRET  failed to set $secret_name" >&2; exit 1; }; fi
else
  if gh secret list --repo "$repo" 2>/dev/null | grep -q "^$secret_name"; then echo "SECRET  $secret_name already present"
  else echo "SECRET  $secret_name not set — create the token with \`claude setup-token\` and run: $0 --token-stdin  (paste the token, Ctrl-D)"; fi
fi

# ---------- 2. repository settings (before the ruleset: rulesets can be unavailable on the plan, these never are) ----------
# /sdlc:go --merge uses `gh pr merge --auto`; GitHub accepts that only with allow_auto_merge AND a protected base branch. Without
# protection (Free-plan private repos) go waits for green checks and merges directly instead — the setting still costs nothing.
if [ "$dry" = 1 ]; then echo "REPO    would set allow_auto_merge=true, delete_branch_on_merge=true"
elif gh api -X PATCH "repos/$repo" -F allow_auto_merge=true -F delete_branch_on_merge=true >/dev/null 2>&1; then echo "REPO    allow_auto_merge=true, delete_branch_on_merge=true (for /sdlc:go --merge)"
else echo "REPO    could not enable allow_auto_merge (permission) — /sdlc:go --merge merges directly after green checks" >&2; fi

# ---------- 3. ruleset ----------
existing=$(gh api "repos/$repo/rulesets" --jq ".[] | select(.name==\"$rs_name\") | .id" 2>/dev/null | head -1)
checks_json=$(printf '%s' "$checks" | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' | while IFS= read -r c; do printf '{"context":%s},' "$(json_escape "$c")"; done); checks_json="[${checks_json%,}]"
bypass_json='[]'   # default: no bypass, so the required status check stays enforced even for the owner (approvals 0 already lets a solo owner merge)
[ "$want_bypass" = 1 ] && [ "$bypass" = 1 ] && bypass_json='[{"actor_id":5,"actor_type":"RepositoryRole","bypass_mode":"pull_request"}]'   # --bypass: admin skips ALL rules on PRs, incl. evals
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
        "require_code_owner_review": $1, "require_last_push_approval": false, "required_review_thread_resolution": true } },
    { "type": "required_status_checks", "parameters": { "strict_required_status_checks_policy": false, "required_status_checks": $checks_json } }
  ]
}
EOF
}
if [ "$dry" = 1 ]; then echo "RULESET would $([ -n "$existing" ] && printf 'update #%s' "$existing" || printf create) '$rs_name' on $default_branch (code-owner review: $([ "$solo" = 1 ] && printf no || printf yes)):"; payload $([ "$solo" = 1 ] && printf false || printf true) | sed 's/^/        /'; exit 0; fi
apply() { # apply <code_owner> → prints api response or fails
  if [ -n "$existing" ]; then payload "$1" | gh api -X PUT "repos/$repo/rulesets/$existing" --input - 2>&1
  else payload "$1" | gh api -X POST "repos/$repo/rulesets" --input - 2>&1; fi
}
first=true; [ "$solo" = 1 ] && first=false
out=$(apply $first); rc=$?
if [ $rc -ne 0 ] && [ "$first" = true ]; then
  # Code-owner review is unavailable on personal Free-plan private repos; retry without it before giving up.
  out2=$(apply false); rc2=$?
  if [ $rc2 -eq 0 ]; then echo "RULESET '$rs_name' applied WITHOUT code-owner review (not available for this repository plan/owner type); PR + review-thread resolution + status check '$checks' + no force-push/deletion${bypass_json:+$([ "$bypass_json" != '[]' ] && printf ' + admin bypass for PRs (solo repo)')}"
  else
    echo "RULESET failed:" >&2; printf '%s\n' "$out2" | head -5 >&2
    echo "        Rulesets on private repositories need GitHub Pro/Team/Enterprise (public repositories: free). Create the rule by hand or make the repo public." >&2; exit 1
  fi
elif [ $rc -ne 0 ]; then
  echo "RULESET failed:" >&2; printf '%s\n' "$out" | head -5 >&2
  echo "        Rulesets on private repositories need GitHub Pro/Team/Enterprise (public repositories: free). The plugin's push guard (protect.block_direct_push) still keeps the agent off the default branch." >&2; exit 1
else
  echo "RULESET '$rs_name' applied: PR required (approvals $approvals)$([ "$first" = true ] && printf ' + code-owner review') + review-thread resolution + status check '$checks' + no force-push/deletion$([ "$bypass_json" != '[]' ] && printf ' + admin bypass for PRs (--bypass: evals becomes advisory for admins)')"
fi

echo "NOTE    the status check '$checks' appears on PRs once .github/workflows/sdlc-evals.yml has run at least once."
exit 0
