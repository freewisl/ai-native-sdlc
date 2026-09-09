---
name: scan
description: Runs a recurring, read-only security scan of the codebase (Stage 6) — injection, authn/authz gaps, secrets, PII in logs, unsafe deserialization, path traversal, misconfiguration, dependency red flags — validates each finding with evidence and a confidence rating, routes bounded fixes through the review gate and wider ones to intent.md, and explains the hosted alternative (Claude Security). Use when the user says "scan the codebase for security issues", "run the security scan", "weekly scan", "보안 스캔", "취약점 점검", "코드베이스 스캔".
argument-hint: "[<path-or-dir>] [--since <ref>] [--report <file>]"
allowed-tools: Read, Grep, Glob, Bash(git log *), Bash(git diff *)
---

# /sdlc:scan — coverage is dated from the last run, not from the first

## What changes
- **Traditional:** Security scanning is an event with a scan launched before a release or an audit. The report goes to a tracker, and the backlog is worked down by hand until the next event. Code written in between is covered by whatever the PR review caught.
- **AI-native:** Scans run on a schedule against every connected repository, on the most capable model available, with findings validated before anyone reads them. Each finding is handled the way a breached control band is: a fix that fits in one PR goes through the review gate, and anything larger becomes an intent.md. Coverage is dated from the last run, not from the first.


## Why it matters
Security teams are sized for human output: when agents multiply code output, either the review queue builds or code ships under-reviewed — a regulated organization can accept neither, so security and policy checks have to keep pace with the agents.

## When to use
On a schedule (weekly for actively developed services — `.github/workflows/sdlc-scan.yml`), after a large merge, or before a release. Also to explain how the hosted Claude Security product replaces or complements this.

Prerequisites: The PR review gate and hooks as approval gates (Stage 5), so that findings go through review like any other change; the intent.md format from Stage 1 for findings too large for a single PR.
Infrastructure: For the hosted form (Claude Security, Claude Enterprise public beta): the Anthropic GitHub App on the target repositories (cloud github.com), Claude Code on the Web enabled, Extra Usage on with a spend limit, premium seats for the people who run scans, and the feature switched on by an admin at claude.ai/admin-settings/claude-code; billed on consumption at Mythos 5 rates, so size the spend limit to the number and size of repositories. The self-hosted form needs only claude and an API key.

## Inputs
- `$ARGUMENTS`: optional scope (directory or file), `--since <git ref>` to scan only changed files, `--report <file>` (default `.sdlc/scans/<date>.md`).
- Checklist: `${CLAUDE_PLUGIN_ROOT}/skills/scan/references/scan-checklist.md`. Policies in `config.policies` (e.g. `secure-api-review`) add their rules to the checklist.

## Steps
1. Scope the scan (whole repo by default; `--since` → `git diff --name-only <ref>`). Read `CLAUDE.md` Architecture to know where external inputs enter.
2. Work through the checklist class by class with `Grep`/`Read`; for every candidate finding **validate it** (trace the data flow, confirm no sanitizer/guard exists) before reporting; attach `file:line`, severity (high/medium/low), confidence (high/medium/low) with the evidence, and whether the fix fits in one PR.
3. Write the report to `.sdlc/scans/<date>.md`: summary table first, then findings (validated only; unvalidated suspicions go in a short "needs a human look" list), then "wider than one PR" items.
4. Route: bounded finding → offer a fix branch that goes through `/sdlc:review` and the PR gate (never merge directly); wider finding (architectural weakness, pattern repeated across services) → `/sdlc:intent --from-scan <finding-id>` so it starts at Plan. Dismissals need a reason recorded in the report (so the same finding does not return as new).
5. After a fix ships, run `/sdlc:evals add` for the vulnerability class so the configuration is tested against it from then on.
6. Hosted alternative (say this once per report) — **Claude Security** (Claude Enterprise, public beta) runs the scans on Anthropic's infrastructure on the most capable model, validates each finding and attaches a confidence rating, and lets suggested patches be applied in Claude Code on the web through the same PR gate. Checklist for the security lead:
   - prerequisites: Anthropic GitHub App installed on the target repositories (cloud github.com), Claude Code on the Web enabled, Extra Usage on with a spend limit, premium seats for the people who run scans, feature switched on by an admin at claude.ai/admin-settings/claude-code; billing is consumption-based at Mythos 5 rates — match the limit to the number and size of repositories;
   - connect the repositories and organize them into projects by repo, service or team so ownership of findings is clear from the start;
   - run a first full scan of the most critical repositories (including ones other tools already scanned) and treat it as the baseline;
   - set a schedule per project — weekly for actively developed services; scope to a directory or branch for large or mixed repositories;
   - triage with the confidence rating in hand; dismiss with a reason so the finding does not return as new;
   - export findings as CSV/Markdown or use webhooks so the existing tracker and audit systems stay the system of record (locally, the `--report` Markdown plays that role);
   - keep deterministic scanners (SAST, dependency audit) in CI — the model-driven scan covers the context-dependent issues those are not built to find.

## Output
The report path and its summary table; the routing decision per finding; the eval/intent follow-ups.

## Governance
Read-only scan. Fixes reach production only through the PR review gate and branch protection; the agent that proposed a fix cannot approve it. Every dismissal has a reason. Repositories, schedule and spend are set centrally when the hosted product is used.

## Measurement
Append `scan.run` to `.sdlc/logs/events.jsonl` with `detail: {scope, findings, high, medium, low, wider_than_pr}`. `/sdlc:metrics` reports scans/findings; the trend in findings per scan should fall as fixes and evals accumulate.
