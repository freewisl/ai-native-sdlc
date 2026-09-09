---
name: evals
description: Manages the regression suite for the agent's configuration (Stage 4 Test, continuous evals) — adds a case from a recent task, review finding or incident (prompt.md + deterministic check.sh + fixture), runs the suite through run-evals.sh with a pass-rate threshold, lists cases and reports trends. Use when the user says "add an eval for this", "turn this incident into an eval", "run the evals", "eval pass rate", "list the eval cases", "regression test the CLAUDE.md change", "eval 추가", "평가 돌려", "이 인시던트를 eval로", "eval 결과 보여줘".
argument-hint: "add [<name>] [--from task|review|incident|scan] | run [--case <glob>] [--model <m>] [--threshold <t>] [--max-turns <n>] [--dry-run] | list | report"
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*")
---

# /sdlc:evals — regression tests for CLAUDE.md, skills and hooks

## What changes
- **Traditional:** QA is a stage gate; a prompt or model change ships on faith.
- **AI-native:** Evals are the AI-native equivalent of stage-gate QA: a live suite of real tasks that runs whenever the agent's configuration (CLAUDE.md, skills, hooks, model) changes and gates the change on the pass rate; every production incident becomes a permanent eval.


## When to use
Whenever the configuration that steers the agent changes (CLAUDE.md, `.claude/`, hooks, skills, a model
swap), on a schedule (CI), and after every production incident or shipped security fix. Prerequisites:
CLAUDE.md and the feedback loop.

Prerequisites: The CLAUDE.md and the feedback loop (Stage 4).
Infrastructure: CI that can run Claude Code non-interactively, and an API key with budget for eval runs. To run only on a cadence instead of on every configuration change, keep just the schedule trigger in sdlc-evals.yml.

## Inputs
- `$ARGUMENTS`: subcommand `add | run | list | report` and its flags (above).
- `.sdlc/config.json` → `paths.evals` (default `evals`), `evals.model`, `evals.max_turns`,
  `evals.threshold`, `evals.allowed_tools`.
- Case anatomy and check.sh patterns: `${CLAUDE_PLUGIN_ROOT}/skills/evals/references/case-format.md`.
- Starter cases (installed by init): `no-secret-in-code`, `respect-frozen-path`, `fix-without-editing-test`.

## Steps
0. **Seed the suite.** The platform engineer collects 20 to 50 real tasks from recent work together with their expected/accepted outcome and writes each as a case (prompt + the checks that define acceptable: tests pass, lint clean, behavior unchanged, policy followed). The suite is live: as models improve, cases that stop discriminating are replaced by new ones from monitoring.

### `list`
Read `<evals>/cases/*/case.json` and `prompt.md`; print a table: name · tags · expects · last result (from
the newest `<evals>/results/*/results.json` if present).

### `add [<name>] [--from …]`
1. Identify the source: the task just completed in this session, a review finding (`/sdlc:review`), an
   incident (`.sdlc/LESSONS.md` entry), or a vulnerability class (`/sdlc:scan`). Ask for the name if not
   given (kebab-case, describes the behavior under test, e.g. `no-pii-in-logs`).
2. Write the prompt as a real engineer would ask it (`prompt.md`), a minimal `fixture/` the task works on
   (language-neutral bash/text fixtures are preferred so the case survives stack changes), and `case.json`
   (`allowed_tools`, `max_turns`, `tags`, `expects`).
3. Write `check.sh` — **deterministic**: receives the work directory as `$1`, exits 0 on pass, prints one
   `stderr` line on fail; no model calls, no network, no timing dependence. Patterns in the reference file
   (file unchanged, grep must/must-not match, script exit code, JSON field equals).
4. Show all four files to the user and ask for confirmation of the check. Optionally run just this case:
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/run-evals.sh" --case <name> --max-turns 10` (costs API usage; ask).
5. Commit `evals(<name>): add case — <expects>`.

### `run [flags]`
1. Confirm cost expectations: cases × model × max_turns; `--dry-run` prints the plan without calling Claude.
2. Execute `bash "${CLAUDE_PLUGIN_ROOT}/scripts/run-evals.sh" <flags>` (flags default to config). The script
   copies each fixture plus the project's `CLAUDE.md`, `.claude/`, `.sdlc/` into a temp dir — that copy
   **is the configuration under test** — runs `claude -p` with the case's allowed tools, then `check.sh`.
3. Report: pass rate, passes/fails by name with the check's stderr line, total cost, threshold and whether
   it was met (exit 1 = below threshold = CI gate fails), results path `<evals>/results/<ts>/summary.md`.
4. If the run was for a configuration change and the pass rate dropped versus the previous result, say so
   explicitly: a skill/CLAUDE.md/hook change that lowers the pass rate must be reviewed before it merges.

### `report`
Read `<evals>/results/*/results.json` and `.sdlc/history/eval_pass_rate.jsonl`; print the pass-rate trend
(last 10 runs), cases that flipped, and cases that always pass (candidates to retire as the model
improves — evals are a live suite; add new discriminating cases from monitoring).

## Output
`list`: table. `add`: the four files and the commit. `run`: the results table with pass rate, cost,
threshold verdict, and the results directory. `report`: trend and recommendations.

## Governance
- The threshold (`evals.threshold`) is the merge gate; `sdlc-evals.yml` runs the suite on PRs that touch
  `CLAUDE.md`, `.claude/**`, `evals/**` and nightly. The team that owns the configuration change approves it.
- Every production incident becomes an eval written by the team that owned the incident, and stays in the
  suite as a regression test (`/sdlc:postmortem` calls `add`).
- Eval runs spend API budget: never launch a full run without telling the user the case count; respect
  `--max-turns`. Never edit a check to make a failing case pass — fix the configuration or retire the case
  with a reason in the commit message.

## Measurement
`run-evals.sh` appends `eval.run` events and `.sdlc/history/eval_pass_rate.jsonl` (also a monitoring band
input). This skill appends on `add`:
```bash
mkdir -p .sdlc/logs && printf '{"ts":"%s","event":"eval.add","session_id":"","decision":"allow","reason":"eval case added","detail":{"name":"%s","source":"%s"}}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<name>" "<task|review|incident|scan>" >> .sdlc/logs/events.jsonl
```
Feeds Stage 4 in `/sdlc:metrics`: pass rate over time and time from a production incident to a
permanent eval (LESSONS entry date → `eval.add`).
