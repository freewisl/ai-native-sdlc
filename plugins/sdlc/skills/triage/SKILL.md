---
name: triage
description: Works the triage queue of automation-written intents (monitor breaches, scan findings, on-call notes) — shows anomaly and evidence per item and records the service owner's decision, fix now / schedule / dismiss with a reason, then hands fix-now items to /sdlc:plan or /sdlc:spec. Use when the user says "triage the queue", "what did the monitor find", "handle the pending intents", "dismiss this finding", "트리아지", "대기 인텐트 처리", "모니터 결과 처리".
argument-hint: "[<file-or-slug>] [fix|schedule <date>|dismiss \"<reason>\"]"
disable-model-invocation: true
allowed-tools: Bash(ls *), Bash(cat *), Bash(git *)
---

# /sdlc:triage — people triage and review; they no longer have to start the work

## Queue
!`d=$(python3 -c 'import json;c=json.load(open(".sdlc/config.json")) if __import__("os").path.exists(".sdlc/config.json") else {};m=c.get("monitor",{});print(m.get("triage_dir", c.get("paths",{}).get("intent","intent")+"/triage"))' 2>/dev/null || echo intent/triage); for f in "$d"/*.md; do [ -f "$f" ] || continue; printf '%s | %s | %s\n' "$f" "$(grep -m1 '^status:' "$f" | sed 's/status: *//')" "$(grep -m1 '^title:' "$f" | sed 's/title: *//')"; done 2>/dev/null || echo "(queue empty or not initialized)"`

## When to use
Whenever the queue is non-empty (the SessionStart context shows the count), after `/sdlc:monitor` or `/sdlc:scan` wrote findings, or when the on-call engineer starts a shift.

Prerequisites: Something wrote to the queue — /sdlc:monitor (band breach) or /sdlc:scan (finding) — and a service owner is available to decide.
Infrastructure: The triage queue directory (monitor.triage_dir) and the intent.md format.

## Inputs
- `$ARGUMENTS`: optional item (file or slug) and decision. Without arguments, walk the queue oldest first.
- Each item is an intent.md with `source: monitor|scan|channel`, frontmatter `monitor: {metric, tier, rule, value, mean, sigma}` when it came from a breach, and the Stage-1 sections (Problem = anomaly and evidence).

## Steps
1. For each item (or the one named): show title, source, tier/rule, the **Problem** evidence and **Open questions**; if the diagnosis is thin, offer to run the `sdlc-diagnoser` agent for a deeper read-only pass before deciding.
2. Ask for the decision — only the service owner (or on-call engineer, or the product owner for product-facing findings) decides:
   - **fix now** → set `status: approved`, `approved_by`, `approved: <date>`; move the file to `<intent>/<slug>.md` (keep the slug); if the fix fits one PR, hand off to `/sdlc:plan <slug>` (the intent doubles as the spec — the plan records that override); if it is wider, hand off to `/sdlc:spec <slug>`.
   - **schedule <date>** → `status: scheduled`, `due: <date>`, keep it in the queue.
   - **dismiss "<reason>"** → `status: rejected`, `reason: "<reason>"`; ask whether the band should be tuned and, if so, add a `_tuning` note to `.sdlc/bands.json` (dismissals tune the bands and reduce noise).
3. Commit each decision: `triage(<slug>): fix now|schedule|dismiss`.
4. When a fix later ships, remind the owner to add an eval for the incident (`/sdlc:evals add`) and a `.sdlc/LESSONS.md` entry (`/sdlc:postmortem`).

## Output
Per item: the decision, the frontmatter change, the commit, and the next command.

## Governance
A human triages and approves; Claude never decides fix/schedule/dismiss on its own. Decisions are recorded in the artifact frontmatter and the commit (author, timestamp), so the queue history is the audit record. Resulting changes go through the normal PR review gate.

## Measurement
Append `triage` events to `.sdlc/logs/events.jsonl` with `decision: fix|schedule|dismiss` and `detail: {slug, source, tier}`. `/sdlc:metrics` reports the queue by status and the share of findings that became fixes.
