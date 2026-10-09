#!/bin/bash
# sdlc — autopilot loop driver behind /sdlc:run (solo repositories only).
# Works through approved intents one at a time, each in a fresh headless session
#   claude -p "/sdlc:go --slug <slug> --autopilot --stop-after plan <title>"  then  "... --hand-off <title>"
# so every change starts with a clean context, then — once at least one PR merged in this run — a self-check pass reviews
# what was merged (sdlc-reviewer + the /sdlc:scan checklist) and turns Important findings into new approved intents through
# a PR of their own, which the next iteration picks up. Stops when the queue is empty after the self-check, or at a cap.
#
# Usage: run-loop.sh [--dir <root|worktree>] [--items a,b,c] [--max-items N] [--max-minutes M] [--item-max-minutes M]
#                    [--note "<text>"] [--once] [--no-self-check] [--dry-run] [--json] [--claude-bin <path>] [--plugin-dir <dir>]
#   --items         only these slugs, in this order (default: every approved intent, oldest first). An intent's
#                   `depends_on: [slug, ...]` keeps it waiting until those are merged.
#   --dir           a linked worktree works too: several loops can run side by side (one per worktree); a slug claimed
#                   by a live loop is skipped by the others.
#   --dry-run       print the queue and the commands; run nothing (needs neither gh nor claude)
#   --once          one item then stop (what sdlc-autopilot.yml uses: the schedule provides the cadence)
# Config (.sdlc/config.json → loop.*): enabled max_items(5) max_minutes(480) item_max_minutes(120) max_turns(500)
#   max_failures_per_slug(3) self_check(true) model("opus" — standard context, so long sessions auto-compact instead of growing
#   toward 1M) effort("high" — passed as --effort, so the user's own effort setting does not carry into unattended sessions)
#   fix_model("sonnet" — the short session that fixes a failing check) fix_rounds(2) checks_max_minutes(30)
#   allowed_tools("Read,Edit,Write,MultiEdit,Grep,Glob,Task,Bash"). Models are aliases (opus/sonnet/haiku): they follow the
#   newest model of each line without a change here; the model each session actually ran is recorded on loop.item.
# Token economy: each item runs as two fresh sessions when loop.split_phases (default): design (/sdlc:go --stop-after plan:
#   intent, spec, plan committed on sdlc/<slug>) and build (/sdlc:go --hand-off: implement from the committed plan) — the
#   build session does not carry the design conversation. go runs with --hand-off (stops once the PR is pushed); this script waits for the PR checks in the shell (no
#   tokens), merges on green, and starts a short fix session only when a check fails. A usage-limit stop ends the run and
#   the next run RESUMES that session (claude --resume) instead of redoing the work; a max-turns or time-cap stop resumes
#   too (every session gets its id up front with --session-id). The time cap kills claude itself, not just a subshell.
#   A stopped session that leaves uncommitted changes stops the run with the tree as it is; the next run starts right there
#   (on sdlc/<slug>, no commit or stash) and resumes that session first. A resumed session runs on its own branch.
#   Every session's total_cost_usd / turns / tokens is recorded in the loop.item event.
#   pause_file(".sdlc/state/pause")
# Requires: roles.solo=true, loop.enabled not false (init turns it on for solo repositories), a git repository on its default branch
#           with a clean tree (or the tree a stopped session left on its branch), gh authenticated, claude.
# Stops:  queue empty · max_items · max_minutes · pause file present · 3 consecutive failures. A slug that failed
#         loop.max_failures_per_slug times is marked blocked in .sdlc/state/loop-failures.txt and skipped until a human clears it.
# Never:  deploy commands of the gated tier, the approval variable, disabling hooks. What merges is exactly what /sdlc:go merges —
#         green checks, reviewer Important = 0, hooks passed. Events: loop.run (summary) and loop.item (per slug) in events.jsonl.
set -u
{ # the whole script is one block, read in full before it runs: editing this file under a running loop cannot change that loop
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"; . "$HERE/lib/hooks-extra.sh"; . "$HERE/lib/scripts-extra.sh"

usage() { sed -n '2,/^set -u/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; }
root=""; max_items=""; max_minutes=""; item_max_arg=""; items_arg=""; note_arg=""; once=0; self_check=""; dry=0; json_out=0; claude_bin="${CLAUDE_BIN:-}"; plugin_dir="${SDLC_PLUGIN_DIR:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) root="$2"; shift 2;;
    --max-items) max_items="$2"; shift 2;;
    --max-minutes) max_minutes="$2"; shift 2;;
    --item-max-minutes) item_max_arg="$2"; shift 2;;
    --items) items_arg="$2"; shift 2;;
    --note) note_arg="$2"; shift 2;;
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
[ -z "$max_minutes" ] && max_minutes=$(cfg "$root" .loop.max_minutes 480)
[ -z "$self_check" ] && self_check=$(cfg_bool "$root" .loop.self_check true)
[ "$once" = 1 ] && max_items=1
item_max=${item_max_arg:-$(cfg "$root" .loop.item_max_minutes 120)}; max_turns=$(cfg "$root" .loop.max_turns 500)
per_slug_max=$(cfg "$root" .loop.max_failures_per_slug 3); model=$(cfg "$root" .loop.model opus)
effort=$(cfg "$root" .loop.effort high); fix_model=$(cfg "$root" .loop.fix_model sonnet)
fix_rounds=$(cfg "$root" .loop.fix_rounds 2); checks_max=$(cfg "$root" .loop.checks_max_minutes 30)
auto_merge=$(cfg_bool "$root" .roles.auto_merge true)
resume_dir="$root/.sdlc/state/loop-resume"; mkdir -p "$resume_dir" 2>/dev/null || true
split_phases=$(cfg_bool "$root" .loop.split_phases true)
item_note=${note_arg:-$(cfg "$root" .loop.item_note "")}; [ -n "$item_note" ] && item_note=" ($item_note)"
plan_rel=$(cfg "$root" .paths.plan plan)
# claims live in the git common dir, shared by every worktree of the repository
claims_dir=$(cd "$root" && cd "$(git rev-parse --git-common-dir 2>/dev/null || echo .git)" 2>/dev/null && pwd)/sdlc-claims; mkdir -p "$claims_dir" 2>/dev/null || true
cur_claim=""; trap '[ -n "$cur_claim" ] && rm -f "$claims_dir/$cur_claim"' EXIT
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
tree_dirty() { git -C "$root" status --porcelain 2>/dev/null | grep -v '^?? .sdlc/' | grep -v '__pycache__/$' | head -1; }
to_default() { # check out the default branch; in a linked worktree (where it is checked out elsewhere) detach at its tip instead
  if git -C "$root" checkout -q "$def" 2>/dev/null; then
    if [ "$has_remote" = 1 ] && [ "$dry" = 0 ]; then git -C "$root" pull -q --ff-only 2>/dev/null || return 1; fi
    return 0
  fi
  [ "$has_remote" = 1 ] && [ "$dry" = 0 ] && git -C "$root" fetch -q origin 2>/dev/null
  git -C "$root" checkout -q --detach "origin/$def" 2>/dev/null || git -C "$root" checkout -q --detach "$def" 2>/dev/null
}
sync_default() {
  local dirty; dirty=$(tree_dirty)
  [ -n "$dirty" ] && { echo "ERROR: working tree is not clean — commit or stash before running the loop: $dirty" >&2; return 1; }
  to_default || { echo "ERROR: cannot check out (or update) $def" >&2; return 1; }
  return 0
}
claimed_by_other() { local f="$claims_dir/$1" pid; [ -f "$f" ] || return 1; pid=$(cut -d' ' -f1 "$f"); [ "$pid" != "$$" ] && kill -0 "$pid" 2>/dev/null; }
claim() { cur_claim="$1"; printf '%s %s\n' "$$" "$root" > "$claims_dir/$1"; }
release() { [ -n "$cur_claim" ] && rm -f "$claims_dir/$cur_claim"; cur_claim=""; }
deps_merged() { # deps_merged <slug> → 0 when every depends_on slug is merged (or its plan is implemented)
  local d deps; deps=$(frontmatter_get "$intent_dir/$1.md" depends_on | tr -d '[]"'"'" | tr ',' ' ')
  for d in $deps; do
    [ "$(frontmatter_get "$plan_dir/$d.md" status)" = implemented ] && continue
    [ "$(pr_state "$d")" = merged ] && continue
    return 1
  done
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
scan_queue() { # prints one line per candidate: Q<TAB>order<TAB>slug | B<TAB>slug (blocked) | W<TAB>slug (open PR, dependency or claimed)
  local s st ps c fc list i=0
  if [ -n "$items_arg" ]; then list=$(printf '%s' "$items_arg" | tr ',' ' '); else list=$(artifact_slugs "$intent_dir"); fi
  for s in $list; do
    i=$((i+1)); [ -f "$intent_dir/$s.md" ] || continue
    st=$(frontmatter_get "$intent_dir/$s.md" status); [ "$st" = "approved" ] || continue
    ps=$(frontmatter_get "$plan_dir/$s.md" status); [ "$ps" = "implemented" ] && continue
    fc=$(fail_count "$s"); if [ -n "$fc" ] && [ "$fc" -ge "$per_slug_max" ]; then printf 'B\t%s\n' "$s"; continue; fi
    case "$(pr_state "$s")" in
      merged) continue;;
      open) [ -s "$resume_dir/$s" ] || { printf 'W\t%s\n' "$s"; continue; };;   # a stopped session resumes even if its PR is already open
    esac
    claimed_by_other "$s" && { printf 'W\t%s\n' "$s"; continue; }      # another loop (another worktree) is on it
    deps_merged "$s" || { printf 'W\t%s\n' "$s"; continue; }            # depends_on not merged yet
    if [ -n "$items_arg" ]; then c=$(printf '%06d' "$i"); else c=$(frontmatter_get "$intent_dir/$s.md" created); fi
    printf 'Q\t%s\t%s\n' "${c:-9999}" "$s"
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
new_sid() { python3 -c 'import uuid; print(uuid.uuid4())'; }
run_claude() { # run_claude <prompt> <log-file> <max-minutes> [resume-session-id] [model] → exit code; output in log; sets cur_sid; parses the result
  local prompt="$1" log="$2" mins="$3" resume="${4:-}" m="${5:-$model}" rc
  local cmd=( "$claude_bin" -p "$prompt" --output-format json --max-turns "$max_turns" --permission-mode acceptEdits --allowedTools "$tools" )
  if [ -n "$resume" ]; then cmd+=( --resume "$resume" ); cur_sid="$resume"   # sessions persist so a stopped run can be resumed
  else cur_sid=$(new_sid); cmd+=( --session-id "$cur_sid" ); fi            # id known up front: even a time-cap kill can be resumed
  [ -n "$m" ] && cmd+=( --model "$m" ); [ -n "$effort" ] && cmd+=( --effort "$effort" ); [ -n "$plugin_dir" ] && cmd+=( --plugin-dir "$plugin_dir" )
  # exec: $! is claude itself, so the time cap kills the session instead of orphaning it behind a dead subshell
  ( cd "$root" && exec env -u CLAUDECODE "${cmd[@]}" < /dev/null > "$log" 2>&1 ) & wait_capped $! $(( mins * 60 )); rc=$?
  parse_result "$log"; return $rc
}
parse_result() { # parse_result <log> → r_err r_sub r_sid r_cost r_turns r_cr r_out r_limit (reset text when a usage limit stopped it) r_models
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
print('\x1f'.join(f(v) for v in [str(bool(d.get('is_error'))).lower(), d.get('subtype',''), d.get('session_id',''), round(d.get('total_cost_usd') or 0,4),
      d.get('num_turns') or 0, u.get('cache_read_input_tokens',0), u.get('output_tokens',0), m.group(0) if m else '',
      ','.join(sorted((d.get('modelUsage') or {}).keys()))]))
PY
)
  IFS=$'\037' read -r r_err r_sub r_sid r_cost r_turns r_cr r_out r_limit r_models <<EOF   # unit separator: a tab IFS would collapse empty fields
$line
EOF
  item_cost=$(python3 -c "print(round(${item_cost:-0}+${r_cost:-0},4))"); item_sessions=$(( ${item_sessions:-0} + 1 ))
  [ -n "$r_models" ] && item_models=$(printf '%s,%s' "${item_models:-}" "$r_models" | tr ',' '\n' | grep -v '^$' | sort -u | paste -sd, -)
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
plan_on_branch() { # plan_on_branch <slug> → 0 when sdlc/<slug> carries an approved (or implemented) plan
  git -C "$root" show "sdlc/$1:$plan_rel/$1.md" 2>/dev/null | sed -n '2,/^---$/p' | grep -Eq '^status:[[:space:]]*"?(approved|implemented)'
}
session_stopped() { # session_stopped <slug> <phase> <rc> → 0 (and sets outcome) when the session stopped early and must be resumed
  local s="$1" phase="$2" rc="$3" sid="${r_sid:-$cur_sid}"
  if [ -n "$r_limit" ]; then outcome=limited; limit_text="$r_limit"
  elif [ "$r_sub" = "error_max_turns" ]; then outcome=failed
  elif [ "$rc" -eq 124 ]; then outcome=timeout
  else rm -f "$resume_dir/$s" "$resume_dir/$s.phase"; return 1; fi
  [ -n "$sid" ] && { printf '%s' "$sid" > "$resume_dir/$s"; printf '%s' "$phase" > "$resume_dir/$s.phase"; }
  return 0
}
run_item() { # run_item <slug> → sets outcome (merged|open|failed|timeout|limited|dry)
  local s="$1" title log rc pr state round=0 resume="" phase=""
  title=$(frontmatter_get "$intent_dir/$s.md" title); [ -z "$title" ] && title="$s"
  log="$root/.sdlc/logs/loop-$s-$(date -u +%Y%m%dT%H%M%SZ).log"; mkdir -p "$root/.sdlc/logs"
  [ -s "$resume_dir/$s" ] && resume=$(cat "$resume_dir/$s"); [ -s "$resume_dir/$s.phase" ] && phase=$(cat "$resume_dir/$s.phase")
  if [ -z "$phase" ]; then if [ "$split_phases" = true ] && ! plan_on_branch "$s"; then phase=design; else phase=build; fi; fi
  if [ "$dry" = 1 ]; then
    if [ -n "$resume" ]; then printf 'WOULD RESUME  session %s for %s (%s phase; claude -p --resume, ≤ %s min, ≤ %s turns)\n' "$resume" "$s" "$phase" "$item_max" "$max_turns"
    else
      [ "$phase" = design ] && printf 'WOULD RUN  claude -p "/sdlc:go --slug %s --autopilot --stop-after plan %s" (design session: intent, spec, plan)\n' "$s" "$title"
      printf 'WOULD RUN  claude -p "/sdlc:go --slug %s --autopilot --hand-off %s" (fresh session, model %s, effort %s, ≤ %s min, ≤ %s turns), then wait for PR checks in the shell and merge on green\n' "$s" "$title" "${model:-default}" "${effort:-default}" "$item_max" "$max_turns"
    fi
    outcome=dry; return
  fi
  claim "$s"; item_cost=0; item_models=""; item_sessions=0
  printf 'ITEM    %s — %s%s\n' "$s" "$title" "$([ -n "$resume" ] && printf '  (resuming %s session %s)' "$phase" "$resume")"
  [ -n "$resume" ] && [ -z "$(tree_dirty)" ] && git -C "$root" checkout -q "sdlc/$s" 2>/dev/null   # the conversation it resumes was on that branch
  if [ "$phase" = design ]; then
    if [ -n "$resume" ]; then
      run_claude "Continue the design phase of the /sdlc:go run for $s from where it stopped (intent, spec and plan on branch sdlc/$s; skip what is already committed), then stop after the plan is committed." "$log" "$item_max" "$resume"; rc=$?
    else
      run_claude "/sdlc:go --slug $s --autopilot --stop-after plan $title$item_note" "$log" "$item_max"; rc=$?
    fi
    if session_stopped "$s" design "$rc"; then finish_item "$s" "$log" "$rc" "$resume"; return; fi
    resume=""
    if [ "$(pr_state "$s")" = merged ]; then outcome=merged; finish_item "$s" "$log" "$rc" "$resume"; return; fi
    if plan_on_branch "$s"; then phase=build
    elif [ -n "$(open_pr "$s")" ]; then phase=checks
    else outcome=failed; echo "DESIGN  $s — no approved plan on sdlc/$s after the design session"; finish_item "$s" "$log" "$rc" "$resume"; return; fi
    git -C "$root" checkout -q --detach 2>/dev/null; to_default >/dev/null 2>&1
  fi
  if [ "$phase" = build ]; then
    if [ -n "$resume" ]; then
      run_claude "Continue the /sdlc:go --hand-off run for $s from where it stopped. Steps already committed on branch sdlc/$s are done; pick up at the first unfinished step." "$log.build" "$item_max" "$resume"; rc=$?
    else
      run_claude "/sdlc:go --slug $s --autopilot --hand-off $title$item_note" "$log.build" "$item_max"; rc=$?
    fi
    if session_stopped "$s" build "$rc"; then finish_item "$s" "$log" "$rc" "$resume"; return; fi
  fi
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
          run_claude "PR #$pr (branch sdlc/$s) has failing checks. Check out that branch, read the failures (gh pr checks $pr; gh run view <run-id> --log-failed), fix the code — never weaken a test — run the verify commands from CLAUDE.md, commit and push, then stop. Do not wait for the checks and do not merge; print SDLC_FIX pushed when done." "$log.fix$round" "$item_max" "" "$fix_model"
          [ -n "$r_limit" ] && { outcome=limited; limit_text="$r_limit"; break; }
          [ -n "$(tree_dirty)" ] || to_default >/dev/null 2>&1;;
        *) echo "CHECKS  did not finish in $checks_max min — PR #$pr left open"; break;;
      esac
    done
  else outcome=failed; fi
  finish_item "$s" "$log" "${rc:-0}" "$resume"
}
finish_item() { # finish_item <slug> <log> <rc> <resumed> — leave a dirty tree as it is (a stopped session's changes stay put)
  local s="$1" log="$2" rc="$3" resumed="$4"
  if [ -n "$(tree_dirty)" ]; then stop_dirty="$(tree_dirty)"; else to_default >/dev/null 2>&1 || true; fi
  release
  log_event "$root" loop.item "$outcome" "/sdlc:run" "{\"slug\":$(json_escape "$s"),\"exit\":$rc,\"log\":$(json_escape "$(rel_path "$root" "$log")"),\"cost_usd\":$item_cost,\"sessions\":$item_sessions,\"turns\":${r_turns:-0},\"cache_read\":${r_cr:-0},\"output\":${r_out:-0},\"models\":$(json_escape "${item_models:-}"),\"resumed\":$([ -n "$resumed" ] && printf true || printf false),\"split\":$split_phases}"
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
# Uncommitted changes on sdlc/<slug> left by that item's stopped session: resume it here first, as it is — no commit, no checkout
resume_here=""
if [ -n "$(tree_dirty)" ]; then
  b=$(git -C "$root" symbolic-ref --short -q HEAD 2>/dev/null)
  case "$b" in sdlc/?*)
    s="${b#sdlc/}"; fc=$(fail_count "$s")
    if [ -s "$resume_dir/$s" ] && { [ -z "$fc" ] || [ "$fc" -lt "$per_slug_max" ]; } \
       && { [ -z "$items_arg" ] || case ",$items_arg," in *",$s,"*) true;; *) false;; esac; }; then resume_here="$s"; fi;;
  esac
fi
if [ -n "$resume_here" ]; then
  echo "RESUME  $resume_here — its stopped session left uncommitted changes on $b; resuming it here before anything else"
  [ "$has_remote" = 1 ] && [ "$dry" = 0 ] && git -C "$root" fetch -q origin 2>/dev/null
  from_sha=$(git -C "$root" rev-parse -q --verify "origin/$def" 2>/dev/null || git -C "$root" rev-parse "$def" 2>/dev/null || echo "")
else
  sync_default || exit 2
  from_sha=$(git -C "$root" rev-parse HEAD 2>/dev/null || echo "")
fi
start_epoch=$(epoch_now)
items=0; merged=0; opened=0; failed=0; consec_fail=0; stop=""; stop_dirty=""; done_slugs=" "; blocked=""; waiting=""; selfcheck="skipped"; run_cost=0; limit_text=""
printf '== sdlc run  root=%s  default=%s  max_items=%s  max_minutes=%s  self_check=%s%s\n' "$root" "$def" "$max_items" "$max_minutes" "$self_check" "$([ "$dry" = 1 ] && printf '  (dry run)')"
while :; do
  [ -f "$pause_file" ] && { stop="paused"; break; }
  [ "$items" -ge "$max_items" ] && { stop="max_items"; break; }
  [ $(( ($(epoch_now) - start_epoch) / 60 )) -ge "$max_minutes" ] && { stop="max_minutes"; break; }
  [ "$consec_fail" -ge 3 ] && { stop="3 consecutive failures"; break; }
  blocked=""; waiting=""; next=""
  if [ -n "$resume_here" ]; then next="$resume_here"; resume_here=""
  else
    build_queue > "$root/.sdlc/state/loop-queue.txt"   # redirection, not $(...): build_queue must set blocked/waiting in this shell
    for s in $(cat "$root/.sdlc/state/loop-queue.txt"); do case "$done_slugs" in *" $s "*) continue;; esac; next="$s"; break; done
  fi
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
    timeout) failed=$((failed+1)); consec_fail=$((consec_fail+1)); echo "TIMEOUT $next hit the ${item_max}-minute cap — the next run resumes that session";;
    dry) ;;
    *) failed=$((failed+1)); consec_fail=$((consec_fail+1)); n=$(fail_bump "$next"); [ "$n" -ge "$per_slug_max" ] && echo "BLOCKED $next failed $n times — remove its line from .sdlc/state/loop-failures.txt to retry";;
  esac
  if [ -n "$stop_dirty" ]; then
    if [ -s "$resume_dir/$next" ]; then echo "STOP    the stopped session left uncommitted changes ($stop_dirty) — left as they are on sdlc/$next; the next run resumes that session right here (nothing to commit or stash)"
    else echo "STOP    working tree left dirty by the session ($stop_dirty) — left as it is; commit or discard it before the next run"; fi
    stop="dirty tree"; break
  fi
  [ "$once" = 1 ] && [ "$items" -ge 1 ] && { stop="once"; break; }
done
mins=$(( ($(epoch_now) - start_epoch) / 60 ))
printf '== done  items=%s merged=%s open=%s failed=%s blocked=[%s] waiting=[%s] self_check=%s stop=%s (%s min, $%s)\n' "$items" "$merged" "$opened" "$failed" "$(trim "$blocked")" "$(trim "$waiting")" "$selfcheck" "$stop" "$mins" "$run_cost"
[ "$dry" = 0 ] && log_event "$root" loop.run "allow" "/sdlc:run" "{\"items\":$items,\"merged\":$merged,\"open\":$opened,\"failed\":$failed,\"self_check\":$(json_escape "$selfcheck"),\"stop\":$(json_escape "$stop"),\"minutes\":$mins,\"cost_usd\":$run_cost}"
[ "$json_out" = 1 ] && printf '{"items":%s,"merged":%s,"open":%s,"failed":%s,"blocked":%s,"waiting":%s,"self_check":%s,"stop":%s,"minutes":%s,"dry_run":%s}\n' "$items" "$merged" "$opened" "$failed" "$(json_escape "$(trim "$blocked")")" "$(json_escape "$(trim "$waiting")")" "$(json_escape "$selfcheck")" "$(json_escape "$stop")" "$mins" "$([ "$dry" = 1 ] && printf true || printf false)"
[ "$failed" -gt 0 ] && exit 1
exit 0
}
