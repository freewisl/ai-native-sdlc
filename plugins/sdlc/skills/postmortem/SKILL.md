---
name: postmortem
description: Closes an incident the AI-native way — captures trigger, impact, root cause and fix as a version-controlled lesson in .sdlc/LESSONS.md, adds the CLAUDE.md correction when a rule follows, turns the incident into a permanent eval, and opens an intent for follow-up work larger than one PR; explains the Claude Tag on-call channel pattern. Use when the user says "write the post-mortem", "record the lessons from this incident", "we just fixed an outage", "포스트모템", "장애 회고", "교훈 기록".
argument-hint: "<incident title> [--date YYYY-MM-DD] [--pr <n>]"
disable-model-invocation: true
---

# /sdlc:postmortem — the response becomes part of the loop and the memory for next time

## What changes
- **Traditional:** Incidents arrive at 3 a.m. and wait for a person; post-mortem actions may never reach the codebase if another fire starts first.
- **AI-native:** The response itself becomes part of the loop and the memory for future incidents: the channel is the audit trail, the lesson is version-controlled, the incident becomes a permanent eval, and larger follow-up re-enters as intent.md.


## When to use
After an incident is resolved (or a fix PR merged) — the same day, while the channel history is fresh. Also for near-misses worth a lesson.

Prerequisites: A resolved incident (or a near-miss) and the fix's PR; LESSONS.md and the evals suite exist (init creates them).
Infrastructure: A version-controlled lessons file; optionally a chat channel where Claude Tag handled the incident and MCP access to confirm the metric is back at baseline.

## Inputs
- `$ARGUMENTS`: title, optional `--date`, `--pr <n>` (the fix).
- `.sdlc/LESSONS.md` (create from `${CLAUDE_PLUGIN_ROOT}/templates/<lang>/LESSONS.md` if missing), `CLAUDE.md`, `<evals>/cases/`, the triage intent if the incident came from `/sdlc:monitor`.

## Steps
1. Interview briefly (≤ 5 questions, skip what is known): what fired the response (band breach / ticket / channel message / scan finding); user impact and duration; root cause (technical and process); the fix (PR/commit) and how it was verified back at baseline (metric, log, screenshot); what would have caught it earlier.
2. Append an entry to `.sdlc/LESSONS.md` in the template format (`## <date> — <title>`, Trigger, Impact, Root cause, Fix, Guard, Owner). Future investigations (and the `sdlc-diagnoser` agent) read this file first.
3. If a rule follows that Claude should apply from the next session on, run `/sdlc:lesson "<one line>"` (CLAUDE.md "Things Claude gets wrong").
4. Make the incident a permanent regression test: `/sdlc:evals add` for the incident class (prompt that would have produced the bug + deterministic check); reference the case name in the entry's Guard line.
5. If follow-up work is larger than one PR (architecture, cross-service pattern), `/sdlc:intent --from-incident "<title>"` so it starts at Plan; small fixes go straight to a PR through the review gate.
6. If the incident was handled in a chat channel (Claude Tag / Slack), note the channel link in the entry — the channel is the audit trail (request, diagnosis, human authorization, fix): anyone in the channel can guide and action the response and test hypotheses in real time, and the channel history adds to the auditability — and confirm the metric is back at baseline (via MCP or `/sdlc:monitor --metric <name>`) in the same thread.
7. Commit: `postmortem: <title>`.

## Output
The LESSONS.md entry, the CLAUDE.md line (if any), the eval case created, the intent opened (if any), and the commit.

## Governance
Written by the team that owned the incident; the entry names an owner. Nothing here bypasses review: fixes are PRs, larger work is an intent. Dismissed follow-ups are recorded with a reason.

## Measurement
Append `postmortem.write` with `detail: {title, guard_case, claude_md_lesson: true|false, intent_slug}`. `/sdlc:metrics` uses LESSONS.md dates and eval case commit dates for "incident → permanent eval" time and repeat-incident counts.
