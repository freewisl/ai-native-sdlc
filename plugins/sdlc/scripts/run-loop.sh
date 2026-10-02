#!/bin/bash
# sdlc — autopilot loop driver behind /sdlc:run (solo repositories only).
# Works through approved intents one at a time, each in a fresh headless session
#   claude -p "/sdlc:go --slug <slug> --autopilot --merge <title>"
# so every change starts with a clean context, then — once at least one PR merged in this run — a self-check pass reviews
# what was merged (sdlc-reviewer + the /sdlc:scan checklist) and turns Important findings into new approved intents through
# a PR of their own, which the next iteration picks up. Stops when the queue is empty after the self-check, or at a cap.
#
# Usage: run-loop.sh [--dir <root>] [--max-items N] [--max-minutes M] [--once] [--no-self-check] [--dry-run] [--json]
#                    [--claude-bin <path>] [--plugin-dir <dir>]
#   --dry-run       print the queue and the commands; run nothing (needs neither gh nor claude)
#   --once          one item then stop (what sdlc-autopilot.yml uses: the schedule provides the cadence)
# Config (.sdlc/config.json → loop.*): enabled max_items(5) max_minutes(120) item_max_minutes(45) max_turns(120)
#   max_failures_per_slug(3) self_check(true) model("opus" — standard context, so long sessions auto-compact instead of growing
#   toward 1M) fix_rounds(2) checks_max_minutes(30) allowed_tools("Read,Edit,Write,MultiEdit,Grep,Glob,Task,Bash")
# Token economy: go runs with --hand-off (stops once the PR is pushed); this script waits for the PR checks in the shell (no
#   tokens), merges on green, and starts a short fix session only when a check fails. A usage-limit stop ends the run and
#   the next run RESUMES that session (claude --resume) instead of redoing the work; a max-turns stop resumes too.
#   Every session's total_cost_usd / turns / tokens is recorded in the loop.item event.
#   pause_file(".sdlc/state/pause")
# Requires: roles.solo=true, loop.enabled not false (init turns it on for solo repositories), a git repository on its default branch
#           with a clean tree, gh authenticated, claude.
# Stops:  queue empty · max_items · max_minutes · pause file present · 3 consecutive failures. A slug that failed
#         loop.max_failures_per_slug times is marked blocked in .sdlc/state/loop-failures.txt and skipped until a human clears it.
# Never:  deploy commands of the gated tier, the approval variable, disabling hooks. What merges is exactly what /sdlc:go merges —
#         green checks, reviewer Important = 0, hooks passed. Events: loop.run (summary) and loop.item (per slug) in events.jsonl.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"; . "$HERE/lib/hooks-extra.sh"; . "$HERE/lib/scripts-extra.sh"

usage() { sed -n '2,/^set -u/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; }
root=""; max_items=""; max_minutes=""; once=0; self_check=""; dry=0; json_out=0; claude_bin="${CLAUDE_BIN:-}"; plugin_dir="${SDLC_PLUGIN_DIR:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) root="$2"; shift 2;;
    --max-items) max_items="$2"; shift 2;;
    --max-minutes) max_minutes="$2"; shift 2;;
    --once) once=1; shift;;
    --no-self-check) self_check=false; shift;;
    --dry-run) dry=1; shift;;
    --json) json_out=1; shift;;
    --claude-bin) claude_bin="$2"; shift 2;;
    --plugin-dir) plugin_dir="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2;;
  esac
done
[ -z "$root" ] && root=$(project_root "")
root=$(cd "$root" && pwd)

# ---------- preconditions ----------
sdlc_initialized "$root" || { echo "ERROR: no .sdlc/config.json in $root — run /sdlc:init first" >&2; exit 2; }
[ "$(cfg "$root" .roles.solo false)" = "true" ] || { echo "ERROR: /sdlc:run is solo-only (roles.solo is not true). In a team repository the intent and plan approvals belong to other people — use /sdlc:go per change." >&2; exit 2; }
loop_en=$(cfg "$root" .loop.enabled ""); [ -z "$loop_en" ] && loop_en=true   # solo is already verified above; a missing key means on (init writes it)
[ "$loop_en" = "true" ] || { echo "ERROR: the autopilot loop is switched off (loop.enabled=false in .sdlc/config.json). Set it to true to run unattended — it merges pull requests without a human at the gate." >&2; exit 2; }
pause_file="$root/$(cfg "$root" .loop.pause_file .sdlc/state/pause)"
[ -f "$pause_file" ] && { echo "PAUSED  $pause_file exists — remove it to resume the loop"; exit 0; }
is_git_repo "$root" || { echo "ERROR: $root is not a git repository" >&2; exit 2; }
[ -z "$max_items" ] && max_items=$(cfg "$root" .loop.max_items 5)
[ -z "$max_minutes" ] && max_minutes=$(cfg "$root" .loop.max_minutes 120)
[ -z "$self_check" ] && self_check=$(cfg_bool "$root" .loop.self_check true)
[ "$once" = 1 ] && max_items=1
item_max=$(cfg "$root" .loop.item_max_minutes 45); max_turns=$(cfg "$root" .loop.max_turns 120)
per_slug_max=$(cfg "$root" .loop.max_failures_per_slug 3); model=$(cfg "$root" .loop.model opus)
fix_rounds=$(cfg "$root" .loop.fix_rounds 2); checks_max=$(cfg "$root" .loop.checks_max_minutes 30)
auto_merge=$(cfg_bool "$root" .roles.auto_merge true)
resume_dir="$root/.sdlc/state/loop-resume"; mkdir -p "$resume_dir" 2>/dev/null || true
tools=$(cfg "$root" .loop.allowed_tools "Read,Edit,Write,MultiEdit,Grep,Glob,Task,Bash")
def=$(cfg "$root" .protect.default_branch main); intent_dir="$root/$(cfg "$root" .paths.intent intent)"; plan_dir="$root/$(cfg "$root" .paths.plan plan)"
has_remote=0; git -C "$root" remote get-url origin >/dev/null 2>&1 && has_remote=1
if [ "$dry" = 0 ]; then
  gh_ready "$root" || { echo "ERROR: gh is not logged in to $(git_remote_host "$root") — the loop needs it to read PR state and merge (gh auth login --hostname $(git_remote_host "$root"))" >&2; exit 2; }
  [ -z "$claude_bin" ] && claude_bin=$(command -v claude 2>/dev/null || true)
  [ -x "$claude_bin" ] || { echo "ERROR: claude executable not found (pass --claude-bin)" >&2; exit 2; }
fi
state_f="$root/.sdlc/state/loop-failures.txt"; mkdir -p "$root/.sdlc/state" 2>/dev/null || true; [ -f "$state_f" ] || : > "$state_f"
fail_count() { grep -E "^$1 " "$state_f" 2>/dev/null | awk '{print $2}' | tail -1; }
fail_bump() { local n; n=$(fail_count "$1"); n=$(( ${n:-0} + 1 )); grep -vE "^$1 " "$state_f" > "$state_f.tmp" 2>/dev/null || true; printf '%s %s\n' "$1" "$n" >> "$state_f.tmp"; mv "$state_f.tmp" "$state_f"; printf '%s' "$n"; }

# ---------- git sync: default branch, clean tree, up to date ----------
sync_default() {
  local dirty; dirty=$(git -C "$root" status --porcelain 2>/dev/null | grep -v '^?? .sdlc/' | head -1)
  [ -n "$dirty" ] && { echo "ERROR: working tree is not clean — commit or stash before running the loop: $dirty" >&2; return 1; }
  git -C "$root" checkout -q "$def" 2>/dev/null || { echo "ERROR: cannot check out $def" >&2; return 1; }
  if [ "$has_remote" = 1 ] && [ "$dry" = 0 ]; then git -C "$root" pull -q --ff-only 2>/dev/null || { echo "ERROR: git pull --ff-only failed on $def" >&2; return 1; }; fi
  return 0
}

# ---------- queue: approved intents whose plan is not implemented, not blocked, without an open PR ----------
pr_state() { # pr_state <slug> → merged | open | none   (dry-run: none)
  [ "$dry" = 1 ] && { printf none; return; }
  local n   # run inside the repository so gh resolves owner/repo AND host from the origin remote (GH_HOST is exported by gh_ready)
  n=$(cd "$root" && gh pr list --head "sdlc/$1" --state merged --json number --jq '.[0].number' 2>/dev/null); [ -n "$n" ] && { printf merged; return; }
  n=$(cd "$root" && gh pr list --head "sdlc/$1" --state open --json number --jq '.[0].number' 2>/dev/null); [ -n "$n" ] && { printf open; return; }
  printf none
}
scan_queue() { # prints one line per candidate: Q<TAB>created<TAB>slug | B<TAB>slug (blocked) | W<TAB>slug (open PR, waiting)
  local s st ps c fc
  for s in $(artifact_slugs "$intent_dir"); do
    st=$(frontmatter_get "$intent_dir/$s.md" status); [ "$st" = "approved" ] || continue
    ps=$(frontmatter_get "$plan_dir/$s.md" status); [ "$ps" = "implemented" ] && continue
    fc=$(fail_count "$s"); if [ -n "$fc" ] && [ "$fc" -ge "$per_slug_max" ]; then printf 'B\t%s\n' "$s"; continue; fi
    case "$(pr_state "$s")" in
      merged) continue;;
      open) [ -s "$resume_dir/$s" ] || { printf 'W\t%s\n' "$s"; continue; };;   # a stopped session resumes even if its PR is already open
    esac
    c=$(frontmatter_get "$intent_dir/$s.md" created); printf 'Q\t%s\t%s\n' "${c:-9999}" "$s"
  done
}
build_queue() { # sets blocked/waiting in the caller's shell, prints the runnable slugs in created order
  local scan; scan=$(scan_queue)
  blocked=$(printf '%s\n' "$scan" | awk -F'\t' '$1=="B"{printf "%s ", $2}'); waiting=$(printf '%s\n' "$scan" | awk -F'\t' '$1=="W"{printf "%s ", $2}')
  printf '%s\n' "$scan" | awk -F'\t' '$1=="Q"{print $2 "\t" $3}' | sort | cut -f2
}

# ---------- one item ----------
wait_capped() { # wait_capped <pid> <seconds> → 0 finished, 124 killed
  local pid="$1" secs="$2" i=0
  while kill -0 "$pid" 2>/dev/null; do [ "$i" -ge "$secs" ] && { kill "$pid" 2>/dev/null; sleep 1; kill -9 "$pid" 2>/dev/null; return 124; }; sleep 1; i=$((i+1)); done
  wait "$pid" 2>/dev/null; return $?
}
run_claude() { # run_claude <prompt> <log-file> <max-minutes> [resume-session-id] → exit code; output in log; parses the result
  local prompt="$1" log="$2" mins="$3" resume="${4:-}" rc
  local cmd=( "$claude_bin" -p "$prompt" --output-format json --max-turns "$max_turns" --permission-mode acceptEdits --allowedTools "$tools" )
  [ -n "$resume" ] && cmd+=( --resume "$resume" )   # sessions persist (no --no-session-persistence) so a stopped run can be resumed
  [ -n "$model" ] && cmd+=( --model "$model" ); [ -n "$plugin_dir" ] && cmd+=( --plugin-dir "$plugin_dir" )
  ( cd "$root" && env -u CLAUDECODE "${cmd[@]}" < /dev/null > "$log" 2>&1 ) & wait_capped $! $(( mins * 60 )); rc=$?
  parse_result "$log"; return $rc
}
parse_result() { # parse_result <log> → r_err r_sub r_sid r_cost r_turns r_cr r_out r_limit (reset text when a usage limit stopped it)
  local line
  line=$(python3 - "$1" <<'PY'
import json,sys,re
d={}
for l in reversed(open(sys.argv[1],encoding='utf-8',errors='replace').read().splitlines()):
    l=l.strip()
    if l.startswith('{'):
        try:
            x=json.loads(l)
            if x.get('type')=='result': d=x; break
        except Exception: pass
u=d.get('usage') or {}; t=d.get('result') or ''
m=re.search(r"hit your (session|weekly|usage|daily) limit[^\n]*", t, re.I)
f=lambda v: str(v).replace('\t',' ').replace('\n',' ')
print('\t'.join(f(v) for v in [str(bool(d.get('is_error'))).lower(), d.get('subtype',''), d.get('session_id',''), round(d.get('total_cost_usd') or 0,4),
      d.get('num_turns') or 0, u.get('cache_read_input_tokens',0), u.get('output_tokens',0), m.group(0) if m else '']))
PY
)
  IFS=$'\t' read -r r_err r_sub r_sid r_cost r_turns r_cr r_out r_limit <<EOF
$line
EOF
  item_cost=$(python3 -c "print(round(${item_cost:-0}+${r_cost:-0},4))"); item_sessions=$(( ${item_sessions:-0} + 1 ))
  run_cost=$(python3 -c "print(round(${run_cost:-0}+${r_cost:-0},4))")
}
open_pr() { # open_pr <slug> → number of the open PR from sdlc/<slug>, or empty
  ( cd "$root" && gh pr list --head "sdlc/$1" --state open --json number --jq '.[0].number' 2>/dev/null )
}
wait_checks() { # wait_checks <pr> → green | failed | timeout   (shell-side wait: costs no tokens)
  local pr="$1" out rc
  out="$root/.sdlc/state/checks-$pr.txt"
  ( cd "$root" && gh pr checks "$pr" --watch --interval 30 > "$out" 2>&1 ) & wait_capped $! $(( checks_max * 60 )); rc=$?
  if [ $rc -eq 124 ]; then printf timeout; return; fi
  if [ $rc -eq 0 ] || grep -qi 'no checks reported' "$out"; then printf green; else printf failed; fi
}
merge_pr() { # merge_pr <pr> → 0 merged
  ( cd "$root" && { gh pr merge "$1" --squash --delete-branch >/dev/null 2>&1 || gh pr merge "$1" --auto --squash --delete-branch >/dev/null 2>&1; } )
}
run_item() { # run_item <slug> → sets outcome (merged|open|failed|timeout|limited|dry)
  local s="$1" title log rc pr state round=0 resume=""
  title=$(frontmatter_get "$intent_dir/$s.md" title); [ -z "$title" ] && title="$s"
  log="$root/.sdlc/logs/loop-$s-$(date -u +%Y%m%dT%H%M%SZ).log"; mkdir -p "$root/.sdlc/logs"
  [ -s "$resume_dir/$s" ] && resume=$(cat "$resume_dir/$s")
  if [ "$dry" = 1 ]; then
    if [ -n "$resume" ]; then printf 'WOULD RESUME  session %s for %s (claude -p --resume, ≤ %s min, ≤ %s turns)\n' "$resume" "$s" "$item_max" "$max_turns"
    else printf 'WOULD RUN  claude -p "/sdlc:go --slug %s --autopilot --hand-off %s" (fresh session, ≤ %s min, ≤ %s turns), then wait for PR checks in the shell and merge on green\n' "$s" "$title" "$item_max" "$max_turns"; fi
    outcome=dry; return
  fi
  item_cost=0; item_sessions=0
  if [ -n "$resume" ]; then
    printf 'ITEM    %s — %s  (resuming session %s)\n' "$s" "$title" "$resume"
    run_claude "Continue the /sdlc:go --hand-off run for $s from where it stopped. Steps already committed on branch sdlc/$s are done; pick up at the first unfinished step." "$log" "$item_max" "$resume"; rc=$?
  else
    printf 'ITEM    %s — %s\n' "$s" "$title"
    run_claude "/sdlc:go --slug $s --autopilot --hand-off $title" "$log" "$item_max"; rc=$?
  fi
  rm -f "$resume_dir/$s"
  if [ -n "$r_limit" ]; then
    [ -n "$r_sid" ] && printf '%s' "$r_sid" > "$resume_dir/$s"
    outcome=limited; limit_text="$r_limit"
  elif [ "$r_sub" = "error_max_turns" ]; then
    [ -n "$r_sid" ] && printf '%s' "$r_sid" > "$resume_dir/$s"   # next run continues instead of starting cold
    outcome=failed
  elif [ $rc -eq 124 ]; then outcome=timeout
  else
    state=$(pr_state "$s")
    if [ "$state" = merged ]; then outcome=merged
    elif pr=$(open_pr "$s") && [ -n "$pr" ]; then
      outcome=open
      while :; do
        printf 'CHECKS  PR #%s — waiting in the shell (≤ %s min)\n' "$pr" "$checks_max"
        case "$(wait_checks "$pr")" in
          green)
            if [ "$auto_merge" = true ] && merge_pr "$pr"; then outcome=merged; fi
            break;;
          failed)
            [ "$round" -ge "$fix_rounds" ] && { echo "CHECKS  still failing after $round fix round(s) — PR #$pr left open for a human"; break; }
            round=$((round+1))
            printf 'FIX     PR #%s round %s — short session for the failing checks\n' "$pr" "$round"
            run_claude "PR #$pr (branch sdlc/$s) has failing checks. Check out that branch, read the failures (gh pr checks $pr; gh run view <run-id> --log-failed), fix the code — never weaken a test — run the verify commands from CLAUDE.md, commit and push, then stop. Do not wait for the checks and do not merge; print SDLC_FIX pushed when done." "$log.fix$round" "$item_max"
            [ -n "$r_limit" ] && { outcome=limited; limit_text="$r_limit"; break; }
            git -C "$root" checkout -q "$def" 2>/dev/null || true;;
          *) echo "CHECKS  did not finish in $checks_max min — PR #$pr left open"; break;;
        esac
      done
    else outcome=failed; fi
  fi
  git -C "$root" checkout -q "$def" 2>/dev/null || true; [ "$has_remote" = 1 ] && git -C "$root" pull -q --ff-only 2>/dev/null || true
  log_event "$root" loop.item "$outcome" "/sdlc:run" "{\"slug\":$(json_escape "$s"),\"exit\":$rc,\"log\":$(json_escape "$(rel_path "$root" "$log")"),\"cost_usd\":$item_cost,\"sessions\":$item_sessions,\"turns\":${r_turns:-0},\"cache_read\":${r_cr:-0},\"output\":${r_out:-0},\"resumed\":$([ -n "$resume" ] && printf true || printf false),\"fix_rounds\":$round}"
  printf 'RESULT  %s → %s  ($%s, %s session(s); log: %s)\n' "$s" "$outcome" "$item_cost" "$item_sessions" "$(rel_path "$root" "$log")"
}

# ---------- self-check: review what merged, file Important findings as approved intents via a PR ----------
self_check_pass() { # self_check_pass <from-sha> → prints clean | pr=<n> | pr=<n>-open
  local from="$1" log prompt out pr idir
  idir=$(rel_path "$root" "$intent_dir")
  log="$root/.sdlc/logs/loop-selfcheck-$(date -u +%Y%m%dT%H%M%SZ).log"
  prompt="You are the self-check step of /sdlc:run on branch $def. Changes merged in this run: \`git diff $from..HEAD\`. Do not fix code and do not edit anything outside $idir/.
1) Launch the sdlc-reviewer agent on that diff against REVIEW.md, and apply the /sdlc:scan security checklist to the same files.
2) For every finding tagged Important (skip every Nit), write $idir/<kebab-slug>.md from $idir/TEMPLATE.md with frontmatter status: approved, source: review or scan, approved_by: sdlc:run (self-check), approval_basis: \"Important finding on merged change\", and the finding as Problem / Proposed outcome / Affected users and systems / Constraints / Open questions.
3) If you wrote at least one intent: git checkout -b sdlc/findings-$(date -u +%Y%m%d-%H%M), commit them, git push -u origin that branch, gh pr create --fill, and print exactly: SDLC_SELFCHECK pr=<number> intents=<count>
   If there is nothing Important: print exactly: SDLC_SELFCHECK clean"
  # progress lines go to stderr: stdout carries only the result token the caller captures
  if [ "$dry" = 1 ]; then echo "WOULD SELF-CHECK  reviewer + scan checklist over the merged diff → Important findings become approved intents via a PR" >&2; printf clean; return; fi
  echo "SELF-CHECK  reviewing what merged since ${from:0:8}" >&2
  run_claude "$prompt" "$log" "$item_max" || true
  [ -n "$r_limit" ] && { echo "SELF-CHECK  stopped by a usage limit ($r_limit)" >&2; printf clean; return; }
  out=$(grep -o 'SDLC_SELFCHECK [^"\\]*' "$log" | tail -1)
  pr=$(printf '%s' "$out" | grep -o 'pr=[0-9]*' | cut -d= -f2)
  if [ -n "$pr" ]; then
    ( cd "$root" && gh pr checks "$pr" --watch >/dev/null 2>&1 ) || true
    if ( cd "$root" && gh pr merge "$pr" --squash --delete-branch >/dev/null 2>&1 ); then echo "SELF-CHECK  findings PR #$pr merged — new intents enter the queue" >&2; git -C "$root" pull -q --ff-only 2>/dev/null || true; printf 'pr=%s' "$pr"
    else echo "SELF-CHECK  findings PR #$pr could not be merged (checks not green?) — left open" >&2; printf 'pr=%s-open' "$pr"; fi
  else echo "SELF-CHECK  clean" >&2; printf clean; fi
}

# ---------- main loop ----------
sync_default || exit 2
start_epoch=$(epoch_now); from_sha=$(git -C "$root" rev-parse HEAD 2>/dev/null || echo "")
items=0; merged=0; opened=0; failed=0; consec_fail=0; stop=""; done_slugs=" "; blocked=""; waiting=""; selfcheck="skipped"; run_cost=0; limit_text=""
printf '== sdlc run  root=%s  default=%s  max_items=%s  max_minutes=%s  self_check=%s%s\n' "$root" "$def" "$max_items" "$max_minutes" "$self_check" "$([ "$dry" = 1 ] && printf '  (dry run)')"
while :; do
  [ -f "$pause_file" ] && { stop="paused"; break; }
  [ "$items" -ge "$max_items" ] && { stop="max_items"; break; }
  [ $(( ($(epoch_now) - start_epoch) / 60 )) -ge "$max_minutes" ] && { stop="max_minutes"; break; }
  [ "$consec_fail" -ge 3 ] && { stop="3 consecutive failures"; break; }
  blocked=""; waiting=""; next=""
  build_queue > "$root/.sdlc/state/loop-queue.txt"   # redirection, not $(...): build_queue must set blocked/waiting in this shell
  for s in $(cat "$root/.sdlc/state/loop-queue.txt"); do case "$done_slugs" in *" $s "*) continue;; esac; next="$s"; break; done
  if [ -z "$next" ]; then
    if [ "$self_check" = "true" ] && [ "$selfcheck" = "skipped" ] && { [ "$merged" -gt 0 ] || { [ "$dry" = 1 ] && [ "$items" -gt 0 ]; }; }; then
      selfcheck=$(self_check_pass "$from_sha"); from_sha=$(git -C "$root" rev-parse HEAD 2>/dev/null || echo "$from_sha")
      case "$selfcheck" in pr=*-open|clean) stop="queue empty"; break;; pr=*) continue;; esac
    fi
    stop="queue empty"; break
  fi
  items=$((items+1)); done_slugs="$done_slugs$next "
  outcome=""; run_item "$next"
  case "$outcome" in
    merged) merged=$((merged+1)); consec_fail=0;;
    open) opened=$((opened+1)); consec_fail=0;;
    limited) echo "LIMIT   $limit_text — stopping the run; the next /sdlc:run resumes $next where it stopped"; stop="usage limit"; break;;
    dry) ;;
    *) failed=$((failed+1)); consec_fail=$((consec_fail+1)); n=$(fail_bump "$next"); [ "$n" -ge "$per_slug_max" ] && echo "BLOCKED $next failed $n times — remove its line from .sdlc/state/loop-failures.txt to retry";;
  esac
  [ "$once" = 1 ] && [ "$items" -ge 1 ] && { stop="once"; break; }
done
mins=$(( ($(epoch_now) - start_epoch) / 60 ))
printf '== done  items=%s merged=%s open=%s failed=%s blocked=[%s] waiting=[%s] self_check=%s stop=%s (%s min, $%s)\n' "$items" "$merged" "$opened" "$failed" "$(trim "$blocked")" "$(trim "$waiting")" "$selfcheck" "$stop" "$mins" "$run_cost"
[ "$dry" = 0 ] && log_event "$root" loop.run "allow" "/sdlc:run" "{\"items\":$items,\"merged\":$merged,\"open\":$opened,\"failed\":$failed,\"self_check\":$(json_escape "$selfcheck"),\"stop\":$(json_escape "$stop"),\"minutes\":$mins,\"cost_usd\":$run_cost}"
[ "$json_out" = 1 ] && printf '{"items":%s,"merged":%s,"open":%s,"failed":%s,"blocked":%s,"waiting":%s,"self_check":%s,"stop":%s,"minutes":%s,"dry_run":%s}\n' "$items" "$merged" "$opened" "$failed" "$(json_escape "$(trim "$blocked")")" "$(json_escape "$(trim "$waiting")")" "$(json_escape "$selfcheck")" "$(json_escape "$stop")" "$mins" "$([ "$dry" = 1 ] && printf true || printf false)"
[ "$failed" -gt 0 ] && exit 1
exit 0
