---
name: monitor
description: Runs the closing-the-loop monitor (Stage 6) — deterministic control-band detection over the metrics in .sdlc/bands.json with Western Electric rules, then the tiered response (1σ log, 2σ read-only diagnosis, 3σ propose a PR or a pre-approved runbook) that writes findings as intent.md into the triage queue. Use when the user says "run the monitor", "check the control bands", "did anything breach", "add a metric to the bands", "모니터 돌려", "밴드 확인", "지표 감시 추가".
argument-hint: "[--dry-run] [--metric <name>] | add <name>"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*")
---

# /sdlc:monitor — a deterministic script watches; Claude acts only when a band is breached

## What changes
- **Traditional:** Maintenance is a reactive phase. All tickets or incidents wait on a person to act on it and restart the process. An alert fires at 3 a.m. and can be missed, a ticket can sit in the backlog until someone picks it up, and post-mortem actions may not reach the codebase at all if another fire starts first.
- **AI-native:** A trigger such as a control-band breach, a ticket, a channel message or a schedule invokes Claude without a person in the path. Claude diagnoses, acts only through gated routes, and writes what it finds as intent.md, which then goes through the stages described above. People triage and review that work, and no longer have to start it.


## Live data (dry run — nothing is written or invoked)
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/monitor.py" --dry-run 2>&1 | tail -40 || true`

## When to use
Manually after a deploy or when CI looks unhealthy; normally a trigger runs it without a person in the path — a scheduled GitHub/GitLab workflow (`.github/workflows/sdlc-monitor.yml`), a webhook from the monitoring stack, a cron job inside the network, or an Agent SDK service that receives webhooks in a sandboxed container; `monitor.py` is stateless in every case. Also to add or tune a metric.

Prerequisites: intent.md, which gives the loop a structured output to restart; Claude-accelerated PR reviews; hooks as an action boundary; and a rollback path for CI/CD, which the highest autonomy tier invokes.
Infrastructure: A metrics store the detection script can query (Prometheus, the CI system's API, or equivalents), read access to the repository, a way to run Claude Code non-interactively in CI, or the Agent SDK for a service that receives webhooks. The trigger layer can be a scheduled workflow in GitHub or GitLab, a webhook from the monitoring stack, a cron job inside the network, or an Agent SDK service in a sandboxed container — in every case monitor.py runs stateless.

## Inputs
- `$ARGUMENTS`: `--dry-run` (decisions only), `--metric <name>`, or `add <name>` to define a new metric.
- `.sdlc/bands.json` (metrics, tiers, runbooks), `.sdlc/history/<metric>.jsonl` (observations), `.sdlc/config.json` `monitor.*`.

## Steps
1. **Run**: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/monitor.py" $ARGUMENTS`. Without `--dry-run` it appends observations to history, logs `band.check`/`band.breach`, and on ≥2σ invokes `claude -p` read-only for a diagnosis written to `<intent>/triage/<ts>-<metric>.md`; on 3σ it also follows the configured `routes` (`pull_request` via `gh`, `runbook:<name>` from `bands.json` `runbooks`). The tier is decided by the script, never by the model.
2. **Explain each decision**: metric, value, baseline mean ± σ, rule fired (R1 3σ spike / R2 2-of-3 beyond 2σ / R3 4-of-5 beyond 1σ / R4 8 same side), tier, action. Point to the triage intent when one was written and tell the user to run `/sdlc:triage`.
3. **`add <name>`**: ask for the observation command (must print one number; e.g. `gh run list --limit 50 --json conclusion --jq '[.[]|select(.conclusion=="failure")]|length/50'`, a Prometheus `curl` + `jq`, or `history_only: true` for values other scripts append), the window (default 30) and the tier actions; append the metric object to `.sdlc/bands.json` (edit with python, keep formatting); commit `monitor: add metric <name>`. Remind that 3σ `routes` must name only pre-approved runbooks and that `runbooks.<name>` must be a real, rehearsed command (the rollback path from `commands.rollback`).
4. **Closed-loop examples (from the playbook)**: CI test-failure rate breaches 3σ → the agent quarantines the flaky test or opens a revert PR and the review gate decides; post-deploy 5xx rate breaches 3σ with a deployment in the window → the agent triggers the existing rollback runbook; PR cycle time trips a drift rule → the agent writes a report for engineering leadership (`routes: ["report"]`) — the loop works for process metrics as well as production ones. The bands template ships the last two as disabled examples.
5. **Tuning**: when the user dismisses a finding as noise, record the reason in `_tuning` inside `bands.json` (array of `{metric, date, note}`) and suggest a wider window or a higher tier threshold.

## Output
The decision lines (`ts  event  tier  reason`), the intents written, and the next step (`/sdlc:triage`, or "all within bands").

## Governance
Detection is deterministic and version-controlled (bands.json, history). Claude at 2σ is read-only; at 3σ it may only open a PR into the review gate or trigger a runbook that was approved in advance. Production access stays denied by permissions/managed settings. Every invocation, finding and decision is logged with a timestamp.

## Measurement
Events: `band.check`, `band.breach` (decision = tier), `monitor.diagnose`, `monitor.intent`, `monitor.propose`, `monitor.pr`, `monitor.runbook`. `/sdlc:metrics` derives "band breach → triage intent" time and repeat-incident counts from them.
