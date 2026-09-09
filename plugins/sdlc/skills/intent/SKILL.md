---
name: intent
description: Captures an idea, ticket, incident or scan finding as a version-controlled intent.md (Stage 1 Plan) — brainstorms like an analyst in a few questions, writes intent/<slug>.md from the organization's template, lets the originator correct it and commits it; also records the product owner's approve/reject decision. Use when the user says "write an intent", "capture this idea as intent.md", "start a new feature request", "turn this ticket into an intent", "intent from this incident", "approve the intent", "인텐트 작성", "아이디어 정리해서 intent 만들어", "티켓을 intent로", "인텐트 승인".
argument-hint: "[<title or idea>] [--from-ticket <id>] [--from-incident <ref>] [--from-scan <finding-id>] | approve <slug> | reject <slug> \"<reason>\""
---

# /sdlc:intent — capture intent in the originator's own words

## What changes
- **Traditional:** An idea passes through backlog entries, user stories, story points, and refinement meetings before anyone can act on it. Ownership transfers at each handoff, so what reaches engineering is several steps removed from what the originator meant.
- **AI-native:** The originator brainstorms with Claude and writes the result down as intent.md, a proto-spec in the originator's own terms. The artifact contains what is wanted, why, and under which constraints. Repeat processes are encoded via skills.


## When to use
At the very start of any change: a person has an idea, a ticket was filed, an incident surfaced, or a
scan found something too big for one PR. Also for the product owner's decision (`approve`/`reject`).

Prerequisites: None.
Infrastructure: Claude access for people who are not engineers (claude.ai or Cowork); an agreed intent.md template; a shared, version-controlled home for intent that the product owner watches (for a single product, an intent/ folder in the product repo).

## Inputs
- `$ARGUMENTS`: free text = the idea/title; `--from-ticket <id>` sets `source: ticket` and `record_id`;
  `--from-incident <ref>` sets `source: incident` (ref = LESSONS.md entry title or incident id);
  `--from-scan <finding-id>` sets `source: scan`; `approve <slug>` / `reject <slug> "<reason>"`.
- `.sdlc/config.json`: `language`, `paths.intent`, `source_of_truth`, `auto_commit`.
- Template: `<intent>/TEMPLATE.md` if present, else `${CLAUDE_PLUGIN_ROOT}/templates/<lang>/intent.md`
  (fallback `en`).
- Conventions for placeholders, frontmatter, commits and logging:
  `${CLAUDE_PLUGIN_ROOT}/skills/intent/references/artifact-conventions.md` — follow it exactly.

## Steps
1. **Read config and template** per the conventions file. If `--from-ticket` is given and a tracker
   connector (MCP) or `gh issue view <id>` is available, read the record first and quote its title and
   description back; otherwise ask the user to paste it.
   `--from-incident`: read the matching entry in `.sdlc/LESSONS.md`. `--from-scan`: read the finding
   from the latest `.sdlc/scans/*.md` report.
2. **Brainstorm until the idea is concrete** — ask the questions an analyst would ask, at most six,
   in one or two rounds, skipping any the user already answered:
   - Scope: what cannot be done today, and how often does it hurt?
   - Users: who is affected, and who else touches the same system?
   - Success: what does "better" look like; how would we know it worked?
   - Constraints: security, compliance, budget, timeline, "existing authentication only", "no new PII"?
   - Out of scope: what is deliberately not part of this?
   - Open questions: what could the originator not answer?
   No formal language is required from the user; keep their wording in the artifact.
3. **Write the artifact** to `<intent>/<slug>.md`: fill the template's sections (Problem, Proposed
   outcome, Affected users and systems, Constraints, Out of scope, Open questions) with the answers in
   the originator's own terms — a proto-spec, not a solution design. Frontmatter: `status: draft`,
   `source`, `record_id`, `author` = `git config user.name`. Never overwrite an existing file (see
   conventions §5). When `language` is `ko`, write the prose in Korean with the template's headings.
4. **Let the originator correct it.** Show the file and ask what Claude misunderstood; apply the
   corrections. Repeat once if needed.
5. **Commit** (`intent(<slug>): <title>`) when `auto_commit` is true and the repo is git; otherwise print
   the commands. Apply the source-of-truth linkage rule (conventions §7): write `record_id`, and write
   the commit SHA back to the legacy record when a connector exists.
6. **Hand off.** Tell the user the product owner approves by running `/sdlc:intent approve <slug>` (or by
   committing `status: approved` / merging the PR), and that approval is what starts `/sdlc:spec <slug>`.
   **Solo mode** (`roles.solo: true` in `.sdlc/config.json`): the user is the product owner — ask "approve now?" and,
   on yes, approve and continue straight into `/sdlc:spec <slug>` in the same session. (`/sdlc:go` skips this question: there the
   user's original request is the approval, recorded as `approval_basis`.)

### `approve <slug>` / `reject <slug> "<reason>"`
Only on the user's explicit instruction. Edit the frontmatter: `status: approved` (or `rejected`),
`approved_by: <git config user.name>`, `approved: <date>`; for a rejection add `reason: "<reason>"`.
Commit `intent(<slug>): approve` / `reject`. Never change a status without being told to.

## Output
The path of the written intent, its rendered content, the commit hash (or the commands to run), and the
next command (`/sdlc:intent approve <slug>` for the product owner, then `/sdlc:spec <slug>`).

## Governance
- The product owner approves; the skill never sets `approved` on its own initiative and never skips the
  correction step. The accept/reject decision is the commit (or merge/closing review) — the audit record
  with author, timestamp and revision history.
- Non-engineers can contribute the same way from claude.ai/Cowork through a repository connector; git
  knowledge is not required. Keep `intent/README.md` pointing at that route.
- Do not turn the intent into a design: no file lists, no architecture decisions; those belong to spec
  and plan.

## Measurement
Append to `.sdlc/logs/events.jsonl` (design §2.3 shape; see conventions §8):
- on write: `event: "intent.write"`, `decision: "allow"`, `detail: {"slug": "...", "source": "idea|ticket|incident|monitor|scan|channel", "record_id": "..."}`
- on approve/reject: `event: "intent.approve"` / `"intent.reject"`, `decision: "allow"|"deny"`, `detail: {"slug": "...", "approved_by": "..."}`
Feeds Stage 1 indicators in `/sdlc:metrics`: leading — time from first conversation to committed
intent (event timestamp vs commit time); lagging — survival rate (approved vs rejected) and intent edits
after the first spec commit (git log).
