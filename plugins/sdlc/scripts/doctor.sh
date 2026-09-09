#!/bin/bash
# sdlc plugin — adoption status, config validation, hook self-test and "next steps" (design §6 doctor).
# Usage: doctor.sh [--json] [--dir <root>] [--help]
set -o pipefail
. "$(dirname "$0")/lib/common.sh"
. "$(dirname "$0")/lib/scripts-extra.sh"

usage() {
  cat <<'EOF'
doctor.sh — where does this project stand on the AI-native SDLC playbook?

  --json        machine-readable output: {"root","checks":[{"id","status":"ok|warn|missing","label","detail"}],"next_steps":[...]}
  --dir <root>  project root (default: $CLAUDE_PROJECT_DIR, git toplevel, or cwd)
  --help

Status glyphs: ✓ adopted · △ partial/optional · ✗ missing. Then "Next steps" in the playbook's dependency order.
EOF
}

JSON=0; ROOT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1; shift;;
    --dir) ROOT_ARG="$2"; shift 2;;
    --dir=*) ROOT_ARG="${1#*=}"; shift;;
    -h|--help) usage; exit 0;;
    *) die "unknown option: $1 (see --help)";;
  esac
done
ROOT=$(resolve_root "$ROOT_ARG")
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sdlc-doctor.XXXXXX"); trap 'rm -rf "$TMP"' EXIT
ROWS="$TMP/rows.tsv"; : > "$ROWS"

row() { # row <ok|warn|missing> <id> <label> [detail]
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "${4:-}" >> "$ROWS"
}
glyph() { case "$1" in ok) printf '✓';; warn) printf '△';; *) printf '✗';; esac; }

INTENT_DIR=$(cfg "$ROOT" .paths.intent intent); SPEC_DIR=$(cfg "$ROOT" .paths.spec spec)
PLAN_DIR=$(cfg "$ROOT" .paths.plan plan); EVALS_DIR=$(cfg "$ROOT" .paths.evals evals)
REVIEW_FILE=$(cfg "$ROOT" .paths.review REVIEW.md)

# ---- git ----
if is_git_repo "$ROOT"; then
  br=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null); nc=$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 0)
  row ok git "git repository" "branch $br, $nc commits"
else
  row missing git "git repository" "not a git repo — the audit trail needs 'git init'"
fi

# ---- CLAUDE.md ----
if [ -f "$ROOT/CLAUDE.md" ]; then
  present=0; for s in $SDLC_CLAUDE_SECTIONS; do claude_md_section_present "$ROOT/CLAUDE.md" "$s" && present=$((present+1)); done
  lines=$(wc -l < "$ROOT/CLAUDE.md" | tr -d ' ')
  st=ok; [ "$present" -lt 5 ] && st=warn
  maxl=$(cfg "$ROOT" .claude_md.max_lines 120); case "$maxl" in ''|*[!0-9]*) maxl=120;; esac
  row $st claude_md "CLAUDE.md" "$present/5 sections, $lines lines$([ "$lines" -gt "$maxl" ] && printf ' — over one page (%s lines), consider trimming' "$maxl")"
  for s in $SDLC_CLAUDE_SECTIONS; do
    t=$(claude_md_section_title "$s")
    if claude_md_section_present "$ROOT/CLAUDE.md" "$s"; then
      if awk -v s="$s" '$0=="<!-- sdlc:begin " s " -->"{p=1} p{print} $0=="<!-- sdlc:end " s " -->"{p=0}' "$ROOT/CLAUDE.md" | grep -q 'TODO:'; then
        row warn "claude_md.$s" "  $t" "still has TODO placeholder"
      else
        row ok "claude_md.$s" "  $t" ""
      fi
    else
      row missing "claude_md.$s" "  $t" "section missing"
    fi
  done
else
  row missing claude_md "CLAUDE.md" "missing — init.sh creates it with the 5 playbook sections"
  for s in $SDLC_CLAUDE_SECTIONS; do row missing "claude_md.$s" "  $(claude_md_section_title "$s")" ""; done
fi

# ---- REVIEW.md ----
if [ -f "$ROOT/$REVIEW_FILE" ]; then
  if grep -q 'TODO:' "$ROOT/$REVIEW_FILE"; then row warn review "$REVIEW_FILE" "generated paths still TODO"; else row ok review "$REVIEW_FILE" ""; fi
else row missing review "$REVIEW_FILE" "missing"; fi

# ---- artifact chain ----
for pair in "intent:$INTENT_DIR" "spec:$SPEC_DIR" "plan:$PLAN_DIR"; do
  kind=${pair%%:*}; d=${pair#*:}
  if [ -d "$ROOT/$d" ]; then
    n=$(count_md_artifacts "$ROOT/$d"); tpl_ok=""; [ -f "$ROOT/$d/TEMPLATE.md" ] && tpl_ok=", TEMPLATE.md"
    approved=$(for f in $(artifact_slugs "$ROOT/$d"); do frontmatter_get "$ROOT/$d/$f.md" status; done | grep -c approved 2>/dev/null || true)
    row ok "dir.$kind" "$d/" "${n:-0} ${kind}s (${approved:-0} approved)$tpl_ok"
  else
    row missing "dir.$kind" "$d/" "missing"
  fi
done
TRIAGE_DIR=$(cfg "$ROOT" .monitor.triage_dir "$INTENT_DIR/triage")
if [ -d "$ROOT/$TRIAGE_DIR" ]; then
  q=$(find "$ROOT/$TRIAGE_DIR" -maxdepth 1 -name '*.md' 2>/dev/null | grep -c . || true)
  if [ "${q:-0}" -gt 0 ]; then row warn triage "$TRIAGE_DIR/ queue" "$q intents waiting for /sdlc:triage"; else row ok triage "$TRIAGE_DIR/ queue" "empty"; fi
else row missing triage "$TRIAGE_DIR/ queue" "missing"; fi

# ---- config ----
CFG="$ROOT/.sdlc/config.json"
if [ ! -f "$CFG" ]; then
  row missing config ".sdlc/config.json" "missing — run init.sh (hooks fall back to safe defaults)"
elif ! json_valid "$CFG"; then
  row missing config ".sdlc/config.json" "INVALID JSON — hooks ignore it until fixed"
else
  res=$(python3 - "$CFG" <<'PY' 2>/dev/null || echo "WARN python3 unavailable — structure not validated"
import json, sys
c = json.load(open(sys.argv[1], encoding='utf-8'))
req = {'version': int, 'language': str, 'paths': dict, 'commands': dict, 'verify': dict, 'plan': dict, 'protect': dict,
       'secrets': dict, 'environments': dict, 'gate': dict, 'hooks': dict, 'evals': dict, 'monitor': dict}
errs = [k for k, t in req.items() if k not in c or not isinstance(c[k], t)]
for p in ('intent', 'spec', 'plan', 'evals'):
    if not isinstance(c.get('paths', {}).get(p), str): errs.append('paths.' + p)
for e in ('dev', 'staging', 'production'):
    env = c.get('environments', {}).get(e)
    if not isinstance(env, dict) or env.get('autonomy') not in ('free', 'constrained', 'gated'): errs.append('environments.' + e)
if errs:
    print('ERR missing/invalid: ' + ', '.join(errs)); sys.exit(0)
warn = []
cmds = c.get('commands', {})
if not cmds.get('test'): warn.append('commands.test empty')
if not cmds.get('build'): warn.append('commands.build empty')
if c.get('environments', {}).get('production', {}).get('autonomy') == 'gated' and not c['environments']['production'].get('approval_env'):
    warn.append('production.approval_env empty (gate can never be approved)')
print(('WARN ' + '; '.join(warn)) if warn else 'OK v%s, language=%s, test=%s' % (c.get('version'), c.get('language'), cmds.get('test')))
PY
)
  case "$res" in OK*) row ok config ".sdlc/config.json" "${res#OK }";; WARN*) row warn config ".sdlc/config.json" "${res#WARN }";; *) row missing config ".sdlc/config.json" "${res#ERR }";; esac
fi

# ---- frozen paths ----
FP=$(cfg "$ROOT" .protect.frozen_paths_file .sdlc/frozen-paths.txt)
if [ -f "$ROOT/$FP" ]; then
  n=$(grep -cvE '^[[:space:]]*(#|$)' "$ROOT/$FP" 2>/dev/null || true)
  row ok frozen "$FP" "${n:-0} protected entries"
else row missing frozen "$FP" "missing"; fi

# ---- evals ----
if [ -d "$ROOT/$EVALS_DIR/cases" ]; then
  n=$(find "$ROOT/$EVALS_DIR/cases" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | grep -c . || true)
  if [ "${n:-0}" -gt 0 ]; then row ok evals "$EVALS_DIR/cases" "$n cases"; else row warn evals "$EVALS_DIR/cases" "0 cases — /sdlc:evals add"; fi
else row missing evals "$EVALS_DIR/cases" "missing"; fi
EV="$ROOT/.sdlc/logs/events.jsonl"; HIST="$ROOT/.sdlc/history/eval_pass_rate.jsonl"
if [ -f "$EV" ] && grep -q '"event":"eval.run"' "$EV" 2>/dev/null; then
  last_ts=$(grep '"event":"eval.run"' "$EV" | tail -1 | sed -E 's/.*"ts":"([^"]+)".*/\1/')
  rate=""; [ -f "$HIST" ] && rate=$(tail -1 "$HIST" | sed -E 's/.*"value":[[:space:]]*([0-9.]+).*/\1/')
  row ok eval_run "last eval run" "$last_ts${rate:+, pass rate $rate}"
else row warn eval_run "last eval run" "never run — bash scripts/run-evals.sh"; fi

# ---- hooks self-test (sample payloads through the plugin's guard hooks, in an isolated temp project) ----
HOOKS="$SDLC_PLUGIN_ROOT/scripts/hooks"
if [ -f "$HOOKS/guard-edit.sh" ] || [ -f "$HOOKS/guard-bash.sh" ]; then
  ST="$TMP/selftest"; mkdir -p "$ST/.sdlc" "$ST/src/gen"
  cp "$SDLC_TEMPLATES/config/config.json" "$ST/.sdlc/config.json"
  printf 'src/gen/\n' > "$ST/.sdlc/frozen-paths.txt"
  run_hook() { # run_hook <script> <payload-json> → exit code
    local out; out=$(cd "$ST" && printf '%s' "$2" | env -u RELEASE_APPROVAL CLAUDE_PROJECT_DIR="$ST" CLAUDE_PLUGIN_ROOT="$SDLC_PLUGIN_ROOT" SDLC_SESSION_ID=doctor-selftest bash "$1" 2>/dev/null); local rc=$?
    # a JSON "deny" decision on stdout counts as a block too
    [ "$rc" = 0 ] && printf '%s' "$out" | grep -q '"permissionDecision":[[:space:]]*"deny"' && rc=2
    return $rc
  }
  pass=0; total=0; fails=""
  probe() { # probe <label> <script> <payload> <expect: block|allow>
    total=$((total+1)); local rc=0
    run_hook "$HOOKS/$2" "$3" || rc=$?
    if [ "$4" = block ] && [ "$rc" = 2 ]; then pass=$((pass+1)); elif [ "$4" = allow ] && [ "$rc" = 0 ]; then pass=$((pass+1)); else fails="$fails $1(rc=$rc,expected $4)"; fi
  }
  if [ -f "$HOOKS/guard-edit.sh" ]; then
    probe frozen-edit guard-edit.sh "{\"session_id\":\"doctor\",\"cwd\":\"$ST\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$ST/src/gen/client.txt\",\"old_string\":\"a\",\"new_string\":\"b\"}}" block
    probe secret-write guard-edit.sh "{\"session_id\":\"doctor\",\"cwd\":\"$ST\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$ST/src/config.sh\",\"content\":\"export AWS_KEY=AKIAQ7ZK3M9PB2XT8LWD\"}}" block
    probe normal-edit guard-edit.sh "{\"session_id\":\"doctor\",\"cwd\":\"$ST\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$ST/src/app.txt\",\"old_string\":\"a\",\"new_string\":\"hello\"}}" allow
  else fails="$fails guard-edit.sh(not found)"; total=$((total+3)); fi
  if [ -f "$HOOKS/guard-bash.sh" ]; then
    probe prod-deploy guard-bash.sh "{\"session_id\":\"doctor\",\"cwd\":\"$ST\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"kubectl apply -f deploy.yaml -n production\"}}" block
    probe git-status guard-bash.sh "{\"session_id\":\"doctor\",\"cwd\":\"$ST\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git status\"}}" allow
    probe prod-first guard-bash.sh "{\"session_id\":\"doctor\",\"cwd\":\"$ST\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"./production-deploy.sh\"}}" block
  else fails="$fails guard-bash.sh(not found)"; total=$((total+3)); fi
  if [ "$pass" = "$total" ]; then row ok hooks "hooks self-test" "$pass/$total probes behaved (frozen path, secret, production gate both word orders)"
  else row warn hooks "hooks self-test" "$pass/$total probes —$fails"; fi
else
  row warn hooks "hooks self-test" "hooks: not found under scripts/hooks/ (plugin build incomplete?)"
fi

# ---- settings ----
SET="$ROOT/.claude/settings.json"
if [ -f "$SET" ]; then
  if ! json_valid "$SET"; then row missing settings_deny ".claude/settings.json" "INVALID JSON"
  else
    nd=$(json_file_get "$SET" '.permissions.deny' | tr -cd '"' | wc -c | tr -d ' '); nd=$((nd/2))
    if [ "$nd" -gt 0 ]; then row ok settings_deny ".claude/settings.json deny list" "$nd rules"; else row warn settings_deny ".claude/settings.json deny list" "no deny rules (Read(.env*), git push --force …)"; fi
    if [ -n "$(json_file_get "$SET" '.sandbox.enabled')" ]; then row ok sandbox "sandbox block" "enabled=$(json_file_get "$SET" '.sandbox.enabled')"
    else row warn sandbox "sandbox block" "not configured (optional: init.sh --sandbox or managed settings)"; fi
  fi
else
  row missing settings_deny ".claude/settings.json deny list" "file missing"
  row warn sandbox "sandbox block" "not configured (optional)"
fi

# ---- auto mode readiness (playbook Stage 3: tuned CLAUDE.md, policy skills, hooks that block unsafe actions, a test suite Claude can run) ----
am_missing=""
{ [ -f "$ROOT/CLAUDE.md" ] && ! grep -qE 'TODO: (set the|describe)' "$ROOT/CLAUDE.md" 2>/dev/null; } || am_missing="$am_missing CLAUDE.md-tuned"
[ "$(json_file_get "$CFG" '.policies' 2>/dev/null | tr -d '[] \n')" != "" ] || am_missing="$am_missing policy-skills"
[ -f "$ROOT/$FP" ] && [ -f "$SDLC_PLUGIN_ROOT/hooks/hooks.json" ] || am_missing="$am_missing hooks"
[ -n "$(cfg "$ROOT" .commands.test "")" ] || am_missing="$am_missing test-command"
if [ -z "$am_missing" ]; then row ok auto_mode "auto mode readiness" "all four guardrails present — auto-accept is reasonable for routine work (tight spec, small blast radius, covered by tests)"
else row warn auto_mode "auto mode readiness" "missing:$am_missing — keep per-edit approval until these exist"; fi

# ---- autopilot loop (/sdlc:run) ----
if [ "$(cfg "$ROOT" .loop.enabled false)" = "true" ]; then
  pf="$ROOT/$(cfg "$ROOT" .loop.pause_file .sdlc/state/pause)"
  if [ -f "$pf" ]; then row warn loop "autopilot loop (/sdlc:run)" "enabled but PAUSED — $(rel_path "$ROOT" "$pf") exists; remove it to resume"
  elif [ "$(cfg "$ROOT" .roles.solo false)" != "true" ]; then row warn loop "autopilot loop (/sdlc:run)" "loop.enabled=true but roles.solo is false — the loop refuses on team repositories"
  else row ok loop "autopilot loop (/sdlc:run)" "enabled — max_items $(cfg "$ROOT" .loop.max_items 5), self_check $(cfg "$ROOT" .loop.self_check true), blocked slugs: $(grep -c . "$ROOT/.sdlc/state/loop-failures.txt" 2>/dev/null || echo 0)"; fi
else row ok loop "autopilot loop (/sdlc:run)" "off (loop.enabled=false) — /sdlc:go per change; enable only on a solo repository you are willing to let merge on green checks"; fi

# ---- GitHub ----
have=0; miss=""
for w in $SDLC_WORKFLOWS; do if [ -f "$ROOT/.github/workflows/$w" ]; then have=$((have+1)); else miss="$miss $w"; fi; done
unrendered=""; for w in $SDLC_WORKFLOWS; do [ -f "$ROOT/.github/workflows/$w" ] && grep -qE '\{\{[a-z_]+\}\}' "$ROOT/.github/workflows/$w" && unrendered="$unrendered $w"; done
if [ -n "$unrendered" ]; then row warn workflows ".github/workflows/sdlc-*.yml" "$have/6 — unrendered {{placeholders}} in:$unrendered (re-run init.sh --github with --marketplace/--ci-workflow)"
elif [ "$have" = 6 ]; then row ok workflows ".github/workflows/sdlc-*.yml" "6/6"
elif [ "$have" -gt 0 ]; then row warn workflows ".github/workflows/sdlc-*.yml" "$have/6 — missing:$miss"
else row missing workflows ".github/workflows/sdlc-*.yml" "none — init.sh --github"; fi
if [ -f "$ROOT/.github/CODEOWNERS" ]; then
  if grep -qE 'TODO|\{\{[a-z_]+\}\}' "$ROOT/.github/CODEOWNERS"; then row warn codeowners ".github/CODEOWNERS" "placeholders (TODO / {{…}}) remain — set real owners"; else row ok codeowners ".github/CODEOWNERS" ""; fi
else row missing codeowners ".github/CODEOWNERS" "missing — human approval gate"; fi

# ---- CI auth consistency: config ci.auth ↔ workflow secret lines (offline) ----
ci_auth=$(cfg "$ROOT" .ci.auth "")
L_OAUTH_A='claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}'; L_OAUTH_C='CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}'
L_API_A='anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}'; L_API_C='ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}'
case "$ci_auth" in oauth) exp_secret=CLAUDE_CODE_OAUTH_TOKEN; other_secret=ANTHROPIC_API_KEY; o1="$L_API_A"; o2="$L_API_C";; api) exp_secret=ANTHROPIC_API_KEY; other_secret=CLAUDE_CODE_OAUTH_TOKEN; o1="$L_OAUTH_A"; o2="$L_OAUTH_C";; *) exp_secret=""; other_secret=""; o1=""; o2="";; esac
if ls "$ROOT"/.github/workflows/sdlc-*.yml >/dev/null 2>&1; then
  mism=""; if [ -n "$o1" ]; then for f in "$ROOT"/.github/workflows/sdlc-*.yml; do { grep -qF "$o1" "$f" || grep -qF "$o2" "$f"; } && mism="$mism$(basename "$f") "; done; fi
  if [ -z "$exp_secret" ]; then row warn ci_auth "CI auth" "ci.auth missing in .sdlc/config.json — re-run /sdlc:init (it records the auth the workflows use)"
  elif [ -n "$mism" ]; then row warn ci_auth "CI auth" "${mism}use $other_secret but ci.auth=$ci_auth — re-run /sdlc:init to merge the auth line"
  else row ok ci_auth "CI auth" "ci.auth=$ci_auth, workflows use $exp_secret"; fi
fi

# ---- branch protection (GitHub ruleset), secret, auto-merge ----
if [ -z "${SDLC_SKIP_GH_DETECT:-}" ] && is_git_repo "$ROOT" && gh_ready "$ROOT"; then
  repo=$(cd "$ROOT" && gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)
  if [ -n "$repo" ]; then
    rs=$(gh api "repos/$repo/rulesets" --jq '[.[] | select(.target=="branch" and .enforcement=="active")] | length' 2>/dev/null || echo "")
    names=$(gh secret list --repo "$repo" 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
    am=$(gh api "repos/$repo" --jq '.allow_auto_merge' 2>/dev/null || echo "")
    if [ "${rs:-0}" -gt 0 ] 2>/dev/null; then row ok ruleset "branch protection ($repo)" "$rs active branch ruleset(s)"
    else row warn ruleset "branch protection ($repo)" "no active branch ruleset (private repos need GitHub Pro; the push guard still blocks direct pushes) — bash scripts/github-setup.sh"; fi
    if [ -n "$exp_secret" ]; then
      case " $names " in *" $exp_secret "*) row ok ci_secret "CI auth secret" "$exp_secret present";; *) row warn ci_secret "CI auth secret" "$exp_secret not set (ci.auth=$ci_auth; present: ${names:-none}) — github-setup.sh --token-stdin";; esac
    else case "$names" in *CLAUDE_CODE_OAUTH_TOKEN*|*ANTHROPIC_API_KEY*) row ok ci_secret "CI auth secret" "present";; *) row warn ci_secret "CI auth secret" "CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY not set — github-setup.sh --token-stdin";; esac; fi
    case "$am" in true) row ok auto_merge "auto-merge ($repo)" "allow_auto_merge=true";; false) row warn auto_merge "auto-merge ($repo)" "allow_auto_merge=false — /sdlc:go --merge falls back to a direct merge after green checks; github-setup.sh enables it";; esac
  fi
fi

# ---- bands ----
BANDS="$ROOT/$(cfg "$ROOT" .monitor.bands .sdlc/bands.json)"
if [ -f "$BANDS" ] && json_valid "$BANDS"; then
  nm=$(json_file_get "$BANDS" '.metrics' | grep -o '"name"' | wc -l | tr -d ' ')
  hist_dir="$ROOT/$(cfg "$ROOT" .monitor.history_dir .sdlc/history)"
  pts=0; [ -d "$hist_dir" ] && pts=$(cat "$hist_dir"/*.jsonl 2>/dev/null | grep -c . || true)
  if grep -qE 'TODO|\{\{' "$BANDS"; then row warn bands "bands ($(rel_path "$ROOT" "$BANDS"))" "$nm metrics, ${pts:-0} history points — runbook placeholder (TODO) remains"
  else row ok bands "bands ($(rel_path "$ROOT" "$BANDS"))" "$nm metrics, ${pts:-0} history points"; fi
elif [ -f "$BANDS" ]; then row missing bands "bands" "INVALID JSON"
else row missing bands "bands (.sdlc/bands.json)" "missing"; fi

# ---- OTel ----
if [ -n "${CLAUDE_CODE_ENABLE_TELEMETRY:-}" ]; then row ok otel "OpenTelemetry" "CLAUDE_CODE_ENABLE_TELEMETRY=$CLAUDE_CODE_ENABLE_TELEMETRY in environment (exporter ${OTEL_METRICS_EXPORTER:-unset})"
elif [ -f "$SET" ] && grep -q 'CLAUDE_CODE_ENABLE_TELEMETRY' "$SET" 2>/dev/null; then row ok otel "OpenTelemetry" "configured in .claude/settings.json env"
else row warn otel "OpenTelemetry" "not configured (optional: templates/config/otel.env → settings env or managed settings)"; fi

# ---- tools ----
for t in jq python3 gh claude git; do
  if has_cmd "$t"; then v=$("$t" --version 2>/dev/null | head -1 | cut -c1-40); row ok "tool.$t" "tool: $t" "$v"
  else case "$t" in python3|git) row missing "tool.$t" "tool: $t" "required";; *) row warn "tool.$t" "tool: $t" "optional$([ "$t" = gh ] && printf ' — PR routes and PR metrics need it')";; esac; fi
done

# ---- output ----
sdlc_next_steps "$ROOT" > "$TMP/next.txt"
if [ "$JSON" = 1 ]; then
  python3 - "$ROOT" "$ROWS" "$TMP/next.txt" <<'PY'
import json, sys
root, rows, nxt = sys.argv[1:4]
checks = []
for line in open(rows, encoding='utf-8'):
    line = line.rstrip('\n')
    if not line: continue
    st, cid, label, detail = (line.split('\t') + ['', '', '', ''])[:4]
    checks.append({"id": cid, "status": st, "label": label.strip(), "detail": detail})
steps = [l.rstrip('\n') for l in open(nxt, encoding='utf-8') if l.strip()]
summary = {"ok": sum(c['status'] == 'ok' for c in checks), "warn": sum(c['status'] == 'warn' for c in checks), "missing": sum(c['status'] == 'missing' for c in checks)}
print(json.dumps({"root": root, "summary": summary, "checks": checks, "next_steps": steps}, indent=2, ensure_ascii=False))
PY
else
  printf 'sdlc doctor — %s\n\n' "$ROOT"
  while IFS=$'\t' read -r st cid label detail; do
    printf '  %s %-40s %s\n' "$(glyph "$st")" "$label" "$detail"
  done < "$ROWS"
  ok=$(cut -f1 "$ROWS" | grep -c '^ok$'); wn=$(cut -f1 "$ROWS" | grep -c '^warn$'); ms=$(cut -f1 "$ROWS" | grep -c '^missing$')
  printf '\n  ✓ %s  △ %s  ✗ %s\n\nNext steps (dependency order):\n' "$ok" "$wn" "$ms"
  sed 's/^/  /' "$TMP/next.txt"
fi
exit 0
