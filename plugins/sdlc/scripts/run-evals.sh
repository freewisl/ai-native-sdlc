#!/bin/bash
# run-evals.sh — run the project's configuration-regression evals (playbook Stage 4, "Continuous evals in CI").
#
# A case is a directory <evals>/cases/<name>/ with:
#   prompt.md   the task, written the way an engineer would ask it
#   fixture/    files the task works on (optional; copied into a fresh work dir per run)
#   check.sh    deterministic acceptance check; receives the work dir as $1; exit 0 = pass.
#               The work dir also holds the configuration under test (CLAUDE.md, .claude/, .sdlc/) and a .git/; judge the
#               agent's OUTPUT, so exclude those paths (see templates/evals/cases/*/check.sh). Harness records (result.json,
#               stderr.txt) are written next to the work dir in <work>.meta/, never inside it.
#   case.json   optional {"allowed_tools": "...", "max_turns": 20, "timeout_seconds": 600, "tags": [...]}
#
# The configuration under test — CLAUDE.md, .claude/ (settings, skills, agents) and .sdlc/ (config, frozen paths,
# bands, LESSONS) — is copied next to the fixture, `claude -p` runs the prompt non-interactively, then check.sh judges
# the outcome. So a change to CLAUDE.md, a skill or a hook is regression-tested like code.
#
# Outputs: <evals>/results/<ts>/results.json + summary.md · events .sdlc/logs/events.jsonl (eval.run, eval.suite)
#          · .sdlc/history/eval_pass_rate.jsonl (feeds monitor.py). Exit 1 when pass rate < threshold.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib/common.sh"
. "$here/lib/hooks-extra.sh"

usage() {
  cat <<'EOF'
run-evals.sh — regression-test the agent's configuration (CLAUDE.md, skills, hooks) against real tasks

  --case <glob>        run only cases whose directory name matches (e.g. 'no-*')
  --model <m>          model for the agent runs (default: evals.model in .sdlc/config.json, else sonnet)
  --threshold <0..1>   minimum pass rate (default: evals.threshold, else 1.0); below it exit 1 — the CI gate
  --max-turns <n>      per-case turn cap (default: evals.max_turns, else 30; case.json overrides)
  --dry-run            print the exact commands; call nothing, write nothing
  --json               print results.json to stdout at the end
  --dir <root>         project root (default: $CLAUDE_PROJECT_DIR, git toplevel, or cwd)
  --claude-bin <path>  claude executable (default: $CLAUDE_BIN or `claude` on PATH)
  --plugin-dir <path>  load a plugin for the runs (default: $CLAUDE_PLUGIN_ROOT when set, so the sdlc hooks apply)
  --help

Needs: claude (authenticated or ANTHROPIC_API_KEY), bash, git (optional), jq or python3.
EOF
}

glob=""; model=""; threshold=""; max_turns=""; dry=false; want_json=false; root=""; claude_bin="${CLAUDE_BIN:-}"; plugin_dir="${CLAUDE_PLUGIN_ROOT:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --case) glob="$2"; shift 2;;
    --model) model="$2"; shift 2;;
    --threshold) threshold="$2"; shift 2;;
    --max-turns) max_turns="$2"; shift 2;;
    --dry-run) dry=true; shift;;
    --json) want_json=true; shift;;
    --dir) root="$2"; shift 2;;
    --claude-bin) claude_bin="$2"; shift 2;;
    --plugin-dir) plugin_dir="$2"; shift 2;;
    --help|-h) usage; exit 0;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2;;
  esac
done
[ -z "$root" ] && root=$(project_root "")
root=$(cd "$root" 2>/dev/null && pwd) || { printf 'ERROR: project root not found: %s\n' "$root" >&2; exit 2; }

evals_rel=$(cfg "$root" .paths.evals evals)
cases_dir="$root/${evals_rel%/}/cases"
[ -z "$model" ] && model=$(cfg "$root" .evals.model sonnet)
[ -z "$threshold" ] && threshold=$(cfg "$root" .evals.threshold 1.0)
[ -z "$max_turns" ] && max_turns=$(cfg "$root" .evals.max_turns 30)
default_tools=$(cfg "$root" .evals.allowed_tools "Read,Edit,Write,Grep,Glob,Bash")
history_dir=$(cfg "$root" .monitor.history_dir ".sdlc/history")

if [ ! -d "$cases_dir" ]; then
  printf 'No eval cases: %s does not exist. Add one with /sdlc:evals add (or copy %s/templates/evals/cases/*).\n' "$cases_dir" "$(sdlc_plugin_root)" >&2
  exit 2
fi
if [ -z "$claude_bin" ]; then claude_bin=$(command -v claude 2>/dev/null || true); fi
if [ "$dry" = false ] && [ -z "$claude_bin" ]; then
  printf 'ERROR: claude executable not found (install: npm install -g @anthropic-ai/claude-code, or pass --claude-bin)\n' >&2; exit 2
fi

ts=$(date -u +%Y%m%dT%H%M%SZ)
results_dir="$root/${evals_rel%/}/results/$ts"
total=0; passed=0; rows=""; suite_cost=0

# wait_capped <pid> <seconds> — 0 when the process exited in time, 124 when killed (macOS has no `timeout`)
wait_capped() {
  local pid="$1" secs="$2" ticks=0 max
  max=$((secs * 2))   # separate statement: in `local a=$1 b=$((a*2))` the arithmetic expands before `a` is assigned
  while kill -0 "$pid" 2>/dev/null; do
    ticks=$((ticks + 1))
    if [ "$ticks" -ge "$max" ]; then kill "$pid" 2>/dev/null; sleep 1; kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; return 124; fi
    sleep 0.5
  done
  wait "$pid"
}
fnum() { printf '%s' "${1:-0}" | awk '{ v=$1+0; printf "%.4f", v }'; }

printf '== sdlc evals  root=%s cases=%s model=%s threshold=%s%s\n' "$root" "$cases_dir" "$model" "$threshold" "$([ "$dry" = true ] && printf ' (dry run)')"
for case_dir in "$cases_dir"/*/; do
  [ -d "$case_dir" ] || continue
  name=$(basename "$case_dir")
  [ -f "$case_dir/prompt.md" ] || { printf 'SKIP  %s (no prompt.md)\n' "$name"; continue; }
  [ -f "$case_dir/check.sh" ] || { printf 'SKIP  %s (no check.sh)\n' "$name"; continue; }
  if [ -n "$glob" ]; then case "$name" in $glob) ;; *) continue;; esac; fi
  cj="$case_dir/case.json"
  tools=$(json_file_get "$cj" .allowed_tools); [ -z "$tools" ] && tools="$default_tools"
  turns=$(json_file_get "$cj" .max_turns); [ -z "$turns" ] && turns="$max_turns"
  tmo=$(json_file_get "$cj" .timeout_seconds); [ -z "$tmo" ] && tmo=600
  total=$((total + 1))

  work=$(mktemp -d "${TMPDIR:-/tmp}/sdlc-eval-$name.XXXXXX")
  [ -d "$case_dir/fixture" ] && cp -R "$case_dir/fixture/." "$work/"
  # configuration under test (fixture files win: only copy what the fixture did not provide). Every copied file is listed in
  # <work>.meta/copied.txt so check.sh can exclude exactly those (and still judge anything the agent writes into CLAUDE.md/.claude/).
  meta="$work.meta"; mkdir -p "$meta"; : > "$meta/copied.txt"
  copy_cfg() { # copy_cfg <src> <dest> — copies and records relative paths of every file copied
    cp -R "$1" "$2"; ( cd "$work" && find "${2#"$work/"}" -type f ) >> "$meta/copied.txt" 2>/dev/null || true
  }
  [ -f "$root/CLAUDE.md" ] && [ ! -e "$work/CLAUDE.md" ] && copy_cfg "$root/CLAUDE.md" "$work/CLAUDE.md"
  if [ -d "$root/.claude" ]; then
    mkdir -p "$work/.claude"
    for x in settings.json skills agents; do [ -e "$root/.claude/$x" ] && [ ! -e "$work/.claude/$x" ] && copy_cfg "$root/.claude/$x" "$work/.claude/$x"; done
  fi
  if [ -d "$root/.sdlc" ]; then
    mkdir -p "$work/.sdlc"
    for x in config.json frozen-paths.txt bands.json LESSONS.md; do [ -e "$root/.sdlc/$x" ] && [ ! -e "$work/.sdlc/$x" ] && copy_cfg "$root/.sdlc/$x" "$work/.sdlc/$x"; done
  fi
  if has_cmd git; then ( cd "$work" && git init -q . && git add -A && git -c user.email=eval@sdlc -c user.name=sdlc-eval commit -qm "eval fixture: $name" ) >/dev/null 2>&1; fi

  prompt=$(cat "$case_dir/prompt.md")
  cmd=( "$claude_bin" -p "$prompt" --output-format json --max-turns "$turns" --model "$model" --permission-mode acceptEdits --allowedTools "$tools" --no-session-persistence )
  [ -n "$plugin_dir" ] && cmd+=( --plugin-dir "$plugin_dir" )

  if [ "$dry" = true ]; then
    printf 'DRY   %s\n      cd %s && env -u CLAUDECODE %s\n      then: bash %s/check.sh %s   (timeout %ss)\n' "$name" "$work" "$(printf '%q ' "${cmd[@]}")" "$case_dir" "$work" "$tmo"
    rm -rf "$work"; continue
  fi

  t0=$(date +%s)
  ( cd "$work" && env -u CLAUDECODE "${cmd[@]}" < /dev/null > "$meta/result.json" 2> "$meta/stderr.txt" ) &   # records stay outside the work tree
  wait_capped $! "$tmo"; rc=$?
  t1=$(date +%s); dur=$((t1 - t0))
  cost=$(json_file_get "$meta/result.json" .total_cost_usd); cost=$(fnum "$cost")
  is_err=$(json_file_get "$meta/result.json" .is_error); [ -z "$is_err" ] && is_err=false
  suite_cost=$(awk -v a="$suite_cost" -v b="$cost" 'BEGIN{printf "%.4f", a+b}')

  check_out=$(bash "$case_dir/check.sh" "$work" 2>&1); check_rc=$?
  if [ "$check_rc" -eq 0 ]; then pass=true; passed=$((passed + 1)); printf 'PASS  %-32s %4ss  $%s\n' "$name" "$dur" "$cost"
  else pass=false; printf 'FAIL  %-32s %4ss  $%s  %s%s\n' "$name" "$dur" "$cost" "$(one_line "$(trunc "$check_out" 160)")" "$([ "$rc" = 124 ] && printf ' [agent timed out]')"; fi
  note=""; [ "$rc" = 124 ] && note="timeout"; [ "$is_err" = "true" ] && note="${note:+$note,}agent_error"
  rows="$rows{\"case\":$(json_escape "$name"),\"pass\":$pass,\"rc\":$rc,\"cost_usd\":$cost,\"duration_s\":$dur,\"model\":$(json_escape "$model"),\"check\":$(json_escape "$(trunc "$check_out" 400)"),\"note\":$(json_escape "$note"),\"work\":$(json_escape "$work")},"
  log_event "$root" eval.run "$([ "$pass" = true ] && printf pass || printf fail)" "$name" "{\"cost_usd\":$cost,\"duration_s\":$dur,\"model\":$(json_escape "$model")}"
  [ "$pass" = true ] && rm -rf "$work" "$meta"   # keep failed work dirs (and their .meta records) for inspection
done

if [ "$dry" = true ]; then printf '== dry run: %s case(s) would run\n' "$total"; exit 0; fi
if [ "$total" -eq 0 ]; then printf '== no cases matched\n'; exit 0; fi

rate=$(awk -v p="$passed" -v t="$total" 'BEGIN{printf "%.4f", (t>0)? p/t : 0}')
gate_ok=$(awk -v r="$rate" -v th="$threshold" 'BEGIN{print (r+0 >= th+0) ? "true" : "false"}')
mkdir -p "$results_dir"
printf '{"ts":"%s","root":%s,"model":%s,"threshold":%s,"total":%s,"passed":%s,"pass_rate":%s,"cost_usd":%s,"gate":%s,"cases":[%s]}\n' \
  "$(now_iso)" "$(json_escape "$root")" "$(json_escape "$model")" "$(fnum "$threshold")" "$total" "$passed" "$rate" "$suite_cost" "$gate_ok" "${rows%,}" > "$results_dir/results.json"
{
  printf '# Eval run %s\n\n- root: %s\n- model: %s\n- cases: %s · passed: %s · pass rate: %s · threshold: %s · gate: %s\n- cost: $%s\n\n| case | result | s | $ | note |\n|---|---|---|---|---|\n' \
    "$ts" "$root" "$model" "$total" "$passed" "$rate" "$threshold" "$([ "$gate_ok" = true ] && printf PASS || printf FAIL)" "$suite_cost"
  printf '%s' "${rows%,}" | tr '}' '\n' | sed 's/^,//' | while IFS= read -r r; do
    [ -z "$r" ] && continue; r="$r}"
    printf '| %s | %s | %s | %s | %s |\n' "$(json_get "$r" .case)" "$([ "$(json_get "$r" .pass)" = true ] && printf PASS || printf FAIL)" "$(json_get "$r" .duration_s)" "$(json_get "$r" .cost_usd)" "$(json_get "$r" .note)"
  done
} > "$results_dir/summary.md"
log_event "$root" eval.suite "$([ "$gate_ok" = true ] && printf pass || printf fail)" "pass_rate=$rate threshold=$threshold" "{\"total\":$total,\"passed\":$passed,\"pass_rate\":$rate,\"cost_usd\":$suite_cost,\"results\":$(json_escape "$results_dir")}"
if [ -d "$root/.sdlc" ]; then
  hd="$history_dir"; case "$hd" in /*) ;; *) hd="$root/$hd";; esac
  mkdir -p "$hd" 2>/dev/null && printf '{"ts":"%s","value":%s,"total":%s,"passed":%s}\n' "$(now_iso)" "$rate" "$total" "$passed" >> "$hd/eval_pass_rate.jsonl"
fi
printf '== %s/%s passed (rate %s, threshold %s) cost $%s → %s\n   results: %s\n' "$passed" "$total" "$rate" "$threshold" "$suite_cost" "$([ "$gate_ok" = true ] && printf 'GATE PASS' || printf 'GATE FAIL — review the configuration change before merging')" "$results_dir"
[ "$want_json" = true ] && cat "$results_dir/results.json"
[ "$gate_ok" = true ]
