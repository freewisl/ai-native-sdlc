---
name: doctor
description: Runs the sdlc adoption health check (doctor.sh) and interprets it — maps every failing item to the playbook play it blocks and the exact command that fixes it, and tells the user what to adopt next. Use when the user asks "is sdlc set up correctly", "check the sdlc setup", "why is the hook not firing", "what should we adopt next", "sdlc health check", "run the doctor", "sdlc 상태 점검", "설정 확인해줘", "훅이 왜 안 돌아", "다음에 뭘 도입하지".
argument-hint: "[--json]"
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/*")
---

# /sdlc:doctor — adoption health check

## When to use
After `/sdlc:init`, after upgrading the plugin, when a hook or skill behaves unexpectedly, or when the
team wants to know which play to adopt next.

## Inputs
- `$ARGUMENTS`: `--json` for machine-readable output (pass through).
- `bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"` output: the status table (✓ ok / △ partial / ✗ missing),
  config schema check, hook self-test (sample payloads through each hook), and "next steps" by the
  playbook's dependency graph.

## Steps
1. Run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh" $ARGUMENTS` from the repository root. If the script
   is missing or fails to start, report that and check `claude plugin list` — the plugin may not be
   installed/enabled in this project.
2. Show the table as printed. Then, for every ✗ and △ row, add one line: **what play it blocks** and
   **the fix command**, using the map below. Do not invent rows the script did not report.
3. If the hook self-test fails or reports "hooks not registered": say plainly that hook changes and
   plugin installs take effect only after a Claude Code restart, and that `claude --debug` shows hook
   registration at start-up. If `jq` is missing, note the hooks fall back to `python3` (slower but
   functional); if both are missing the guards cannot parse tool input and will be no-ops.
4. Show the script's "Next steps" as printed — it is the single source of the adoption order (the playbook's
   dependency figure: layer 1 start-anywhere plays → layer 2 skills · subagents · evals → layer 3 requirements
   & design · PR review → layer 4 CI/CD → layer 5 closing the loop). Do not reorder it. Note the `auto mode
   readiness` row: auto-accept is reasonable only when the four guardrails exist (tuned CLAUDE.md, policy skills,
   hooks, a test suite Claude can run).
5. Offer the single most valuable next command (one, not a list) based on the first missing item in
   that order.

### Fix map (✗ → play blocked → fix)
| Item | Blocks | Fix |
|---|---|---|
| Not a git repository | the whole audit trail | `git init && git add -A && git commit -m "init"` |
| `.sdlc/config.json` missing/invalid | every hook, skill and script | `/sdlc:init` (schema errors: fix the key the script names) |
| CLAUDE.md missing or a section (Commands/Conventions/Architecture/Things Claude gets wrong/Verifying) missing | Stage 3 CLAUDE.md, plan mode, review, parallel sessions | `/sdlc:init` adds the missing section stubs; fill them |
| `commands.test` (or build/lint) empty | Stage 4 feedback loop, `/sdlc:verify`, evals, Stop hook | set `commands.*` in `.sdlc/config.json` and CLAUDE.md "Verifying your work" |
| `REVIEW.md` missing | Stage 5 PR review | `/sdlc:init` copies the template, or `/sdlc:review` creates it |
| `intent/` `spec/` `plan/` missing | Stage 1–3 artifact chain | `/sdlc:init` |
| no eval cases | Stage 4 continuous evals | `/sdlc:evals add` (3 starter cases ship with init) |
| hooks not registered / self-test failed | all guardrails and gates | enable the plugin, restart Claude Code; check `claude --debug` |
| `jq` missing | hook speed (python fallback) | `brew install jq` / `apt install jq` |
| `gh` missing | `/sdlc:review --pr`, CI metrics in bands.json | install GitHub CLI and `gh auth login` |
| `policies: []` | Stage 2 policy-constrained specs | `/sdlc:policy <name>` (or `--example secure-api-review`) |
| `frozen-paths.txt` empty | Stage 3 protected-path guard | add generated/frozen paths (one per line) |
| bands metric without `command` or history | Stage 6 monitoring | edit `.sdlc/bands.json`; run `/sdlc:monitor --dry-run` |
| `commands.rollback` empty | Stage 5 release, Stage 6 3σ runbook | set the rehearsed rollback command in config |
| `.github/workflows/sdlc-*.yml` missing | Stage 5 CI/CD automation | `/sdlc:init --github` |
| `.sdlc/LESSONS.md` missing | Stage 6 post-mortem loop | `/sdlc:postmortem` creates it from the template |

## Output
The doctor table, the annotated ✗/△ lines, the restart note when relevant, and one recommended next
command. With `--json`, print the JSON unchanged and add the interpretation below it.

## Governance
Read-only: the doctor changes nothing. It also never marks a play "adopted" on its own — the table
reflects files and config that exist in git, which is the evidence an auditor would accept.

## Measurement
Append `doctor.run` to `.sdlc/logs/events.jsonl` with the counts:
```bash
mkdir -p .sdlc/logs && printf '{"ts":"%s","event":"doctor.run","session_id":"","decision":"allow","reason":"doctor executed","detail":{"ok":%s,"warn":%s,"fail":%s}}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<n>" "<n>" "<n>" >> .sdlc/logs/events.jsonl
```
Feeds the adoption trend in `/sdlc:metrics` (plays adopted over time).
