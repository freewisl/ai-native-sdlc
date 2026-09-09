---
name: metrics
description: Reports the playbook's leading and lagging indicators for every stage — time to committed intent, intent→spec elapsed, spec rework, plan→implemented, CLAUDE.md lessons, verify and eval pass rates, gate decisions, band breaches, triage outcomes — computed from git, .sdlc logs and history, with "source needed" for anything the repo cannot see. Use when the user asks "how is the loop performing", "sdlc metrics", "show the indicators", "are we getting faster", "지표 보여줘", "메트릭", "루프 성과".
argument-hint: "[--since 90d] [--format md|json]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*")
---

# /sdlc:metrics — how you measure whether it worked

## Live data
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/metrics.py" $ARGUMENTS 2>&1 || true`

## When to use
Monthly tuning (the playbook's tech-lead review), before widening autonomy (auto mode, `verify.required_before_stop`, more parallel sessions), or when someone asks whether the AI-native process is paying off.

## Inputs
- `$ARGUMENTS`: `--since 90d|12w|6m|1y` (default 90d), `--format md|json`.
- Sources: git history of `intent/ spec/ plan/`, `.sdlc/logs/events.jsonl`, `.sdlc/logs/gate.log`, `.sdlc/history/*.jsonl`, `.sdlc/LESSONS.md`, `CLAUDE.md`, `evals/cases`, and `gh` when installed.

## Steps
1. If the live data above is missing, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/metrics.py" $ARGUMENTS`.
2. Present the six stage tables. For every indicator with a value, say in one sentence whether it moves the right way per the playbook (e.g. intent time weeks → hours; spec rework → 0; time to first review → minutes; eval pass rate stable; repeat incidents falling).
3. For every "source needed" row, name the exact connection that would fill it: `gh` login (PR metadata), CI workflow conclusions (first-pass success), the incident tracker (change failure rate, escaped defects), OpenTelemetry export (hook wait times, fleet-wide session concurrency).
4. Recommend at most three actions, tied to indicators (e.g. "spec rework 4 → add a Flagged-concerns review before plan"; "verify pass rate 60% → keep `verify.required_before_stop` off until commands are stable").

## Output
The tables, the one-line reading per indicator, the connections to add, and the three actions.

## Governance
Read-only. Numbers come from committed history and local logs; nothing is estimated where data is missing.

## Measurement
This skill is the measurement surface for all stages; it writes no events.
