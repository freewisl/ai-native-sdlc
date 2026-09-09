---
name: lesson
description: Records a correction in CLAUDE.md's "Things Claude gets wrong" section the second time the same mistake appears — one deduplicated line, keeps CLAUDE.md under a page, optionally adds a matching eval, and commits. Use when the user says "add this to CLAUDE.md", "Claude keeps doing X, remember not to", "record a lesson", "that's the second time — put it in the mistakes list", "CLAUDE.md에 추가해", "이거 또 틀렸어, 기억시켜", "교훈 기록", "같은 실수 반복 방지".
argument-hint: "\"<one-line correction>\" [--eval] [--section <heading>]"
---

# /sdlc:lesson — when Claude makes a mistake twice, the correction goes into CLAUDE.md

## When to use
The working rule from the playbook: a mistake seen twice becomes a line in CLAUDE.md. Called by the user,
by `/sdlc:review` when a finding repeats, and by `/sdlc:postmortem` when an incident yields a rule.
Prerequisite: CLAUDE.md exists (`/sdlc:init` creates it).

## Inputs
- `$ARGUMENTS`: the correction as one line, imperative and specific ("Do not bump dependency versions;
  the platform team owns them"); `--eval` also creates an eval case; `--section` targets another CLAUDE.md
  heading (default "Things Claude gets wrong").
- `CLAUDE.md` at the repo root (between `<!-- sdlc:begin mistakes -->` … `<!-- sdlc:end mistakes -->` when
  the markers exist, else under the `## Things Claude gets wrong` heading; create the heading if absent).

## Steps
1. **Shape the line.** A rule, not a story: what to do or not do, where it applies, and the reason or the
   place to look, in ≤ 160 characters. Reject narrative ("last week Claude…") and ask for the rule. If the
   line names a path or command, check it exists.
2. **Deduplicate.** Read the existing lines in the section. Normalize (lowercase, strip punctuation, keep
   words ≥ 4 letters) and compare: if an existing line shares the same key terms (path, command, rule
   object) or says the same thing, do not add a second line — sharpen the existing one instead and say so.
3. **Insert.** Replace the `- (none yet)` stub if present; otherwise append `- <line>` at the end of the
   section. Keep the existing order (newest last).
4. **Keep it under a page.** Count `wc -l CLAUDE.md`; above 120 lines warn and propose the two or three
   stalest lines to remove or merge (a rule the linter/hook now enforces, a path that no longer exists).
   Do not remove anything without the user's agreement — CLAUDE.md changes are reviewed like code.
5. **Eval (optional).** With `--eval`, or when the lesson came from a review finding or an incident, invoke
   `/sdlc:evals add <kebab-name> --from review|incident` (Skill tool) so the rule is regression-tested,
   not just written down.
6. **Commit** `docs(claude-md): lesson — <line, truncated to 60 chars>` (when `auto_commit`; else print the
   commands). Since review reads CLAUDE.md, the mistake is caught from the next PR onwards.

## Output
The line added (or the existing line sharpened), the section as it now reads, the line count with the
page warning if any, the eval case name when created, and the commit hash.

## Governance
- CLAUDE.md is version controlled; code owners approve changes in PR review. The lesson is one line of
  team memory, not a place for task notes or session logs.
- Never weaken a verification rule ("skip flaky test X") through this skill — that is a fix in code or an
  explicit team decision, not a lesson.
- A rule that must hold without exception needs a hook or a review pass behind it; say so when the
  lesson is safety- or policy-relevant (`/sdlc:policy`, frozen paths, test guard).

## Measurement
```bash
mkdir -p .sdlc/logs && printf '{"ts":"%s","event":"lesson.add","session_id":"","decision":"allow","reason":"lesson recorded","detail":{"line":%s,"source":"%s","eval":%s}}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(printf '%s' "<line>" | jq -Rs .)" "<user|review|postmortem>" "<true|false>" >> .sdlc/logs/events.jsonl
```
Feeds Stage 3's CLAUDE.md indicators in `/sdlc:metrics`: lessons added over time (git history of
CLAUDE.md) and how often Claude repeats a mistake CLAUDE.md should have caught (`review.run` events with
`repeat_finding: true` after the lesson date).
