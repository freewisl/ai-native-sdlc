---
name: run
description: Runs the autopilot loop for a solo repository — works through every approved intent with /sdlc:go (autopilot + merge) in fresh sessions, then reviews what merged and files Important findings as new approved intents through a PR, repeating until nothing is left to fix or a cap is reached. Use when the user types "/sdlc:run", "run the loop until it's clean", "keep going through the backlog", "무인으로 돌려", "백로그 끝까지", "알아서 계속 돌려".
argument-hint: "[--max-items N] [--max-minutes M] [--once] [--no-self-check] [--dry-run]"
disable-model-invocation: true
allowed-tools: Read, Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(git *), Bash(gh *)
---

# /sdlc:run — the loop that keeps running

## What changes
- **`/sdlc:go`:** one sentence, one change, one PR; a human types the next sentence and (without `--merge`) merges.
- **`/sdlc:run`:** the backlog is the input. Every approved intent becomes a merged PR, the merged result is reviewed again, and Important findings become the next items — until the queue is empty. The human's job moves from typing and merging to reading what merged (the playbook's auto-mode stance: review artifacts after the session instead of watching edits).

## When to use
A repository with `roles.solo: true` where the owner has decided that green checks + the reviewer agent + hooks are enough to merge without a person at the gate. Never on team repositories: there the intent and plan approvals belong to other people, and the script refuses. Typed by the user only (`disable-model-invocation`) — a loop that merges code must never start from a phrase the model inferred.

## Inputs
- `$ARGUMENTS`: `--max-items N` (default `loop.max_items`, 5) · `--max-minutes M` (120) · `--once` (one item; what the CI workflow uses) · `--no-self-check` · `--dry-run` (queue and commands only).
- `.sdlc/config.json` → `loop.*`: `enabled` (false — must be set to true on purpose), `max_items`, `max_minutes`, `item_max_minutes`, `max_turns`, `max_failures_per_slug`, `self_check`, `model`, `allowed_tools`, `pause_file`.

## Steps
1. **Preflight.** `bash "${CLAUDE_PLUGIN_ROOT}/scripts/run-loop.sh" --dry-run $ARGUMENTS`. Show the queue it prints (approved intents without an implemented plan, an open PR, or a block mark) and the caps. If it refuses — not solo, `loop.enabled` false, dirty tree, pause file, gh not logged in — print its message verbatim and stop; do not work around any of these.
2. **Run.** `bash "${CLAUDE_PLUGIN_ROOT}/scripts/run-loop.sh" $ARGUMENTS`. The script does the work: for each item a fresh `claude -p "/sdlc:go --slug <slug> --autopilot --merge <title>"` (its own context, capped in minutes and turns), then the self-check pass once something merged. Do not run `/sdlc:go` yourself in this session — the point of the loop is one clean context per change.
3. **Report** what the script printed: items, merged, left open (checks not green — a human looks), failed/blocked (with the log path each), self-check result, why it stopped. For every blocked slug say how to clear it (`.sdlc/state/loop-failures.txt`). For every open PR give the URL.
4. **Never** set the release approval variable, run deploy commands of the gated tier, edit hooks or `loop.*` to get past a stop, or delete the pause file. If the user wants to stop a running loop: `touch .sdlc/state/pause`.

## Stop conditions (the script's, not yours)
| Stop | Why |
|---|---|
| Queue empty after self-check | Nothing Important left — the loop is done |
| `max_items` / `max_minutes` | Cost and blast-radius cap per run |
| 3 consecutive failures | Something systemic (auth, build) — a human looks |
| Pause file | The owner said stop |
| Per-slug block after `max_failures_per_slug` | The same change keeps failing; it needs a person, not a fourth try |

## Output
The script's summary line, the list of merged PR numbers, open PRs, blocked slugs with log paths, and one line on what a human should read (`git log --merges` since the start SHA, the `loop.item` events).

## Governance
- What merges is exactly what `/sdlc:go` merges: tests and the `evals` check green, reviewer Important = 0, hooks passed, pushed through a PR (the push guard still blocks the default branch). The loop adds no bypass.
- The gated deploy tier stays human. The loop never deploys.
- Unattended runs (`sdlc-autopilot.yml`) use `--once` on a schedule; the same caps and the pause file apply. PRs opened with the default `GITHUB_TOKEN` do not trigger other workflows, so that workflow needs `SDLC_GH_TOKEN` (fine-grained PAT) for the review and evals checks to run on the loop's PRs.
- Self-check intents carry `approved_by: sdlc:run (self-check)` — the audit trail says which approvals were automatic.

## Measurement
`loop.run` (items, merged, open, failed, self_check, stop, minutes) and `loop.item` events; `/sdlc:metrics` shows merged-per-run and the share of items that needed a human (open + blocked).
