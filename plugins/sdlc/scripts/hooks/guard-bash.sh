#!/bin/bash
# PreToolUse Bash — (1) rollback command always allowed, (2) per-environment deploy gate
# (production gated / staging ask / dev free) with gate.log + events, (3) secret scan of the diff on `git commit` (design §3).
# Uninitialized project → only the built-in production gate (patterns from templates/config/config.json, approval env RELEASE_APPROVAL).
# Reads: commands.rollback, environments.*.{patterns,autonomy,approval_env}, gate.mode, gate.log, secrets.scan_commits
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
cmd=$(json_get "$input" .tool_input.command)
[ -z "$cmd" ] && exit 0
cmd_short=$(trunc "$(one_line "$cmd")" 100)
initialized=false; sdlc_initialized "$root" && initialized=true

# (1) the practiced rollback path is never gated
if [ "$initialized" = true ]; then
  rb=$(cfg "$root" .commands.rollback "")
  if [ -n "$rb" ]; then
    case "$cmd" in "$rb"|"$rb "*)   # the command IS the rollback (possibly with arguments) — not merely a command that mentions it
      gate_log "$root" allow rollback "$cmd"
      log_event "$root" hook.gate allow rollback "{\"cmd\":$(json_escape "$cmd_short")}"
      exit 0;;
    esac
  fi
fi

# (2) environment tiers — first matching environment decides (production, staging, dev, then any custom names)
if [ "$initialized" = true ]; then
  cfgjson=$(cat "$root/$SDLC_CONFIG_REL"); gate_mode=$(cfg "$root" .gate.mode deny)
  keys=$(json_keys "$cfgjson" .environments); envs=""
  for k in production staging dev; do printf '%s\n' "$keys" | grep -qx "$k" && envs="$envs $k"; done
  while IFS= read -r k; do
    case "$k" in ''|production|staging|dev) continue;; esac
    case "$k" in *[!A-Za-z0-9_]*) continue;; esac   # names must be usable in a JSON path
    envs="$envs $k"
  done <<< "$keys"
else
  cfgjson=$(cat "$(sdlc_default_config)" 2>/dev/null); gate_mode=deny; envs="production"
fi

for env in $envs; do
  patterns=$(json_lines "$cfgjson" ".environments.$env.patterns")
  [ -z "$patterns" ] && continue
  matched=""
  while IFS= read -r pat; do
    [ -z "$pat" ] && continue
    if printf '%s\n' "$cmd" | grep -E -i -q -e "$pat" 2>/dev/null; then matched="$pat"; break; fi
  done <<< "$patterns"
  [ -z "$matched" ] && continue

  autonomy=$(json_get "$cfgjson" ".environments.$env.autonomy"); [ -z "$autonomy" ] && autonomy=gated
  detail="{\"env\":\"$env\",\"pattern\":$(json_escape "$matched"),\"cmd\":$(json_escape "$cmd_short")}"
  case "$autonomy" in
    free)
      gate_log "$root" allow "$env" "$cmd"; log_event "$root" hook.gate allow "$env: autonomy free" "$detail" ;;
    constrained)
      gate_log "$root" ask "$env" "$cmd"; log_event "$root" hook.gate ask "$env: autonomy constrained" "$detail"
      ask_json "[sdlc] $env deploy — confirm: '$cmd_short' matches environments.$env pattern '$matched' (autonomy: constrained). Approve to run it now." ;;
    *)  # gated (and unknown values, which get the safest treatment)
      approval_env=$(json_get "$cfgjson" ".environments.$env.approval_env")
      case "$approval_env" in ''|*[!A-Za-z0-9_]*) approval_env=RELEASE_APPROVAL;; esac
      approval="${!approval_env:-}"
      if [ -n "$approval" ]; then
        gate_log "$root" allow "$env" "$cmd"; log_event "$root" hook.gate allow "$env: approved via $approval_env" "$detail"
      else
        gate_log "$root" deny "$env" "$cmd"; log_event "$root" hook.gate deny "$env: no $approval_env" "$detail"
        msg="'$cmd_short' matches a $env pattern ('$matched'). Why: $env is gated (environments.$env.autonomy: gated) — Production deploys need a release authorization. To proceed: Set $approval_env=<ticket> (release manager) and retry; the rollback command (commands.rollback) is always allowed. If this command only reads (grep/cat/log), rephrase it or narrow the pattern in .sdlc/config.json."
        if [ "$gate_mode" = "ask" ]; then ask_json "[sdlc] $msg"; else block "$msg"; fi
      fi ;;
  esac
  break
done

# (2b) no direct path to the default branch — branch protection in a hook, for repositories where GitHub rulesets are unavailable.
#      Parses each `git push` clause (split on && ; | ||): refspecs (+, refs/heads/, src:dst), --all/--mirror, --delete <default>,
#      bare `git push` while on the default branch, `git -C <dir> push`. Feature-branch pushes and PR creation are allowed.
push_hits_default() { # push_hits_default <clause> <default-branch> <current-branch> → prints a reason and returns 0 when the clause pushes to the default branch
  local def="$2" cur="$3" tok seen_git=0 seen_push=0 skipnext=0 remote="" refspecs="" all=0 delete=0 r dst
  # shellcheck disable=SC2086
  set -- $1
  for tok in "$@"; do
    if [ $seen_git = 0 ]; then case "$tok" in git|*/git) seen_git=1;; esac; continue; fi
    if [ $seen_push = 0 ]; then
      if [ $skipnext = 1 ]; then skipnext=0; continue; fi
      case "$tok" in push) seen_push=1;; -C|-c|--git-dir|--work-tree|--namespace|--exec-path) skipnext=1;; -*) ;; *) return 1;; esac
      continue
    fi
    if [ $skipnext = 1 ]; then skipnext=0; continue; fi
    case "$tok" in
      --all|--mirror|--branches) all=1;;
      --delete|-d) delete=1;;
      --repo|--receive-pack|--exec|--push-option|-o) skipnext=1;;
      -*) ;;
      *) if [ -z "$remote" ]; then remote="$tok"; else refspecs="$refspecs $tok"; fi;;
    esac
  done
  [ $seen_push = 1 ] || return 1
  [ $all = 1 ] && { printf 'pushes every branch (--all/--mirror), including %s' "$def"; return 0; }
  for r in $refspecs; do
    r="${r#+}"; case "$r" in *:*) dst="${r#*:}";; *) dst="$r";; esac
    dst="${dst#refs/heads/}"
    if [ "$dst" = "$def" ]; then
      if [ $delete = 1 ]; then printf 'deletes the default branch %s' "$def"; else printf 'refspec targets %s' "$def"; fi; return 0
    fi
  done
  if [ -z "$refspecs" ] && [ $delete = 0 ] && [ "$cur" = "$def" ]; then printf 'bare push while on %s' "$def"; return 0; fi
  return 1
}
if [ "$initialized" = true ] && [ "$(cfg_bool "$root" .protect.block_direct_push true)" = "true" ]; then
  case "$cmd" in *git*push*|*push*)
    def=$(git -C "$root" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##'); [ -z "$def" ] && def=$(cfg "$root" .protect.default_branch main)
    cur=$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null)
    reason=""; old_ifs="$IFS"; IFS=$'\n'
    for clause in $(printf '%s\n' "$cmd" | sed -E 's/&&|\|\||;|\|/\n/g'); do
      IFS="$old_ifs"
      if r=$(push_hits_default "$clause" "$def" "$cur"); then reason="$r"; break; fi
      IFS=$'\n'
    done
    IFS="$old_ifs"
    if [ -n "$reason" ]; then
      log_event "$root" hook.guard deny direct_push "{\"cmd\":$(json_escape "$cmd_short"),\"branch\":\"$def\",\"reason\":$(json_escape "$reason")}"
      block "pushing to the default branch '$def' ($reason: '$cmd_short'). Why: anything the agent writes arrives as a pull request through branch protection — there is no direct path to the default branch (protect.block_direct_push). To proceed: push a feature branch and open a PR (git push -u origin <branch> && gh pr create); a human merges it. To allow direct pushes in this repository set protect.block_direct_push: false."
    fi ;;
  esac
fi

# (3) secrets in the changes being committed
if [ "$initialized" = true ] && [ "$(cfg_bool "$root" .secrets.scan_commits true)" = "true" ]; then
  if printf '%s\n' "$cmd" | grep -E -q '(^|[^[:alnum:]_./-])git([[:space:]]+-[^[:space:]]+)*[[:space:]]+commit([[:space:]]|$)'; then
    all=""
    printf '%s\n' "$cmd" | grep -E -q -e '(^|[[:space:]])--all([[:space:]]|$)' -e '(^|[[:space:]])-[A-Za-z]*a[A-Za-z]*([[:space:]]|$)' && all="--all"
    scan_root="$root"
    git -C "$scan_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || scan_root=$(json_get "$input" .cwd)
    hits=""; [ -n "$scan_root" ] && hits=$(bash "$here/../secret-scan.sh" --staged "$scan_root" $all 2>/dev/null)
    if [ -n "$hits" ]; then
      log_event "$root" hook.guard deny commit_secret "{\"cmd\":$(json_escape "$cmd_short"),\"hits\":$(json_escape "$(one_line "$hits")")}"
      block "possible secret in the changes being committed ($(one_line "$hits")). Why: credentials never enter version control (secrets.scan_commits). To proceed: take the value out of the diff (git restore --staged <file>, then edit it to read from the environment or a secret manager) and commit again."
    fi
  fi
fi
exit 0
