# Artifact conventions (shared by /sdlc:intent, /sdlc:spec, /sdlc:plan, /sdlc:triage)

Every stage ends by committing one markdown artifact the next stage reads. These rules keep the
chain consistent so `status.sh`, `metrics.py`, the hooks and the reviewer can follow it.

## 1. Read the project configuration first

```bash
root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cfg="$root/.sdlc/config.json"
lang=$(jq -r '.language // "en"' "$cfg" 2>/dev/null || echo en)
intent_dir=$(jq -r '.paths.intent // "intent"' "$cfg" 2>/dev/null || echo intent)
spec_dir=$(jq -r '.paths.spec // "spec"' "$cfg" 2>/dev/null || echo spec)
plan_dir=$(jq -r '.paths.plan // "plan"' "$cfg" 2>/dev/null || echo plan)
auto_commit=$(jq -r '.auto_commit // true' "$cfg" 2>/dev/null || echo true)
sot_mode=$(jq -r '.source_of_truth.mode // "repo"' "$cfg" 2>/dev/null || echo repo)
```

If `jq` is missing, read the same keys with `python3 -c 'import json,sys; ...'`. If `.sdlc/config.json`
is missing, say so and point at `/sdlc:init`; artifacts may still be written with the defaults above.

## 2. Pick the template

1. Project override: `<intent_dir>/TEMPLATE.md` (intent only), or a `TEMPLATE.md` in the spec/plan directory.
2. Plugin template for the configured language: `${CLAUDE_PLUGIN_ROOT}/templates/<lang>/<intent|spec|plan>.md`.
3. Fallback: `${CLAUDE_PLUGIN_ROOT}/templates/en/<name>.md`.

When `lang` is `ko`, write the prose in Korean but keep the template headings exactly as they are in
the chosen template (the scripts and the reviewer look for those headings).

## 3. Fill the placeholders

| Placeholder | Value |
|---|---|
| `{{slug}}` | kebab-case of the title: lowercase, ASCII letters/digits, words joined by `-`, max ~50 chars. Korean titles: transliterate or use a short English key; keep the Korean title in `title:` |
| `{{title}}` | the title as the originator said it |
| `{{author}}` | `git config user.name` (fallback `$USER`) |
| `{{date}}` | `date +%F` |
| `{{intent_path}}` | repo-relative path of the intent file, e.g. `intent/claims-status.md` |
| `{{spec_path}}` | repo-relative path of the spec file |
| `{{intent_commit}}` | `git log -1 --format=%h -- <intent_path>` (empty string if uncommitted) |

Do not leave a `{{...}}` token in the written file. Remove HTML comments that were only guidance once
the section has real content; keep them when the section is intentionally empty.

## 4. Frontmatter (design §2.2)

```yaml
---
type: intent | spec | plan
slug: <slug>
title: <title>
status: draft            # intent/spec: draft | approved | rejected | superseded ; plan: draft | approved | implemented
author: <name>
created: YYYY-MM-DD
source: idea | ticket | incident | monitor | scan | channel
record_id: ""            # legacy system id when source_of_truth.mode != repo, or --from-ticket
links: { intent: "", spec: "", plan: "" }
test_changes: forbidden  # plan only: forbidden | allowed
approved_by: ""          # set together with status: approved
---
```

Status transitions are made by a human decision only:
- intent / spec: `draft → approved | rejected` — the skill edits `status:` only when the user says
  "approve" / "reject" and records `approved_by: <git user.name>` and `approved: YYYY-MM-DD`.
- plan: `draft → approved` when `ExitPlanMode` is accepted; `approved → implemented` when the work is done.
- Triage intents: `draft → approved | scheduled | rejected` (see /sdlc:triage).

## 5. Never overwrite

If the target file already exists:
1. Show its frontmatter and ask whether to (a) revise it, or (b) create a new slug.
2. Revising: keep the frontmatter, append `## Revision <date>` with what changed and why, and never
   reset `status:` (an approved artifact that changes materially should go back to `draft` only if the
   user says so; record it).
3. Never delete an artifact; supersede it (`status: superseded`, link to the successor).

## 6. Commit (the commit is the audit record)

When the repo is git and `auto_commit` is `true`:
```bash
git add "<path>" && git commit -q -m "<type>(<slug>): <title>"     # type = intent | spec | plan
```
Approval commits use `<type>(<slug>): approve` (or `reject`). When `auto_commit` is `false`, print the
exact `git add`/`git commit` lines and ask the user to run them. If the directory is not a git
repository, explain that author and timestamp come from the commit and suggest `git init`.

## 7. Source of truth linkage (Stage 3 sidebar)

- `repo` (default): the markdown file is authoritative; put the ticket id in `record_id` when one exists.
- `legacy`: the ticket system is authoritative. Read the record at the start (MCP connector or `gh issue view`),
  write `record_id`, and after the commit write the commit SHA back to the record in the same session
  (MCP tool or a comment). If no connector is available, print the SHA and ask the user to paste it into the record.
- `linkage`: both; do both of the above.

## 8. Event log line

Append one line to `.sdlc/logs/events.jsonl` when `.sdlc/` exists (skip silently otherwise):
```bash
mkdir -p .sdlc/logs && printf '{"ts":"%s","event":"%s","session_id":"","decision":"%s","reason":%s,"detail":%s}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<event>" "<allow|deny|ask>" "$(printf '%s' "<reason>" | jq -Rs .)" '<detail-json>' >> .sdlc/logs/events.jsonl
```
Without `jq`, write the reason as a plain JSON string literal by hand (escape quotes). Hooks fill `session_id`; skills leave it empty.


## Additions

- `{{team}}` in the intent Author line: ask the originator for their team (or read `git config user.team`); when unknown, drop the parentheses so the line reads `Author: <name>. Status: draft.` as in the article's example.
- Source of truth per artifact: `source_of_truth.per_artifact.{intent,spec,plan}` may name a different mode per artifact kind (for example intent in Jira, plan in the repo). Resolution order: `per_artifact.<kind>` when non-empty, else `source_of_truth.mode`. Whatever the mode, every artifact carries `record_id` and every legacy record gets the commit SHA of the markdown file (the "linkage as the minimum bar" rule).
- Approval gates: the written list lives in `.sdlc/APPROVALS.md`; a skill that hits a gate (release, ticketed path) names the row it applies.
