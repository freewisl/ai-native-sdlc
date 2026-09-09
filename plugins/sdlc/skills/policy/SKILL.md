---
name: policy
description: Turns one piece of institutional knowledge (a security standard, API convention, brand or UX rule) into a version-controlled policy skill under .claude/skills/<name>/ with a trigger description, the policy owner and source of truth, an optional check script, and registers it in .sdlc/config.json policies so /sdlc:spec and /sdlc:review apply it; then tests that it triggers. Use when the user says "encode this policy as a skill", "make our API security standard a skill", "add a brand rule skill", "정책을 스킬로", "코딩 규약 스킬 만들어", "secure-api-review 예시 설치".
argument-hint: "<name> [--owner <who>] [--source <url-or-doc>] [--example secure-api-review]"
---

# /sdlc:policy — skills as institutional knowledge: explicit, version-controlled, applied broadly, updated centrally

## What changes
- **Traditional:** A policy (security standard, API convention, brand rule) is enforced inconsistently — by whoever remembers it in review.
- **AI-native:** Skills make institutional knowledge operational: explicit, version-controlled, applied broadly while the code is written, and updated centrally when the policy changes; engineers pick up the new version in their next session. A skill is advisory — a hook or review pass behind it makes the policy binding.


## When to use
When a rule is enforced inconsistently today and has a named owner and a written source of truth. Write a skill for knowledge that must be applied consistently; keep commands and project facts in CLAUDE.md instead.

Prerequisites: None required. Having a CLAUDE.md helps, because it keeps the agent's working knowledge in the repo, but a skill does not depend on it (text); the dependency figure draws CLAUDE.md → Skills — treat CLAUDE.md as helpful, not required.
Infrastructure: One policy with a named owner and a written source of truth.

## Inputs
- `$ARGUMENTS`: `<name>` (kebab-case), `--owner`, `--source`, or `--example secure-api-review` to install the playbook's example from `${CLAUDE_PLUGIN_ROOT}/templates/policies/secure-api-review/`.
- The policy owner's source of truth (document, wiki page, standard) — ask for it if not given; do not invent rules.

## Steps
1. Gather: the rules as numbered, checkable statements; when the skill must trigger (the tasks that touch the policy: "creating or modifying an external-facing endpoint", "generating an OpenAPI spec"); the owner and approval date; whether a deterministic check exists or can be written (`scripts/check-*.sh`).
2. Write `.claude/skills/<name>/SKILL.md`: frontmatter `name`, `description` in third person that states what it applies and lists the trigger situations (this is what makes Claude load it); a header comment `<!-- Policy owner: … · Source of truth: … · Last approved: … -->`; a numbered body of rules; a closing line "Run scripts/check-<name>.sh and include its output in your summary" when a check exists. Keep it under a page. `--example` copies the secure-api-review skill and its check script, then fills its three header placeholders (`{{policy_owner}}` from `--owner`, `{{policy_source}}` from `--source`, `{{date}}` = today) — ask for owner/source when not given.
3. Register: add `<name>` to `policies` in `.sdlc/config.json` (python one-liner; keep formatting). `/sdlc:spec` invokes each listed policy as a constraint and `/sdlc:review`'s Compliance pass re-checks it.
4. Test that it triggers: propose three differently-worded tasks that should load it and one that should not; ask the user to try them (or run `claude --debug` and look for the skill load); adjust the description until it loads each time.
5. Commit `policy(<name>): add skill (owner: …)`. Tell the owner that policy changes are made in this file, signed off by them in PR review, and picked up by engineers in their next session.
6. Remind: a skill is an advisory control. A policy that must always hold needs something deterministic behind it — a hook (`/sdlc:init` hooks, `frozen-paths`, secret scan) or the review pass — "the skill makes violations rare and the hook makes them close to impossible".

## Output
The skill path and content, the config change, the three trigger tests, and the commit.

## Governance
Policy owner signs off the skill and its changes (code owners on `.claude/skills/`). Skill invocations are visible in session traces (OpenTelemetry `skill.name`) and in `/sdlc:review` findings that cite the policy.

## Measurement
Append `policy.add` with `detail: {name, owner}`. `/sdlc:metrics` reports policy skills count and review findings citing a policy (should fall towards zero); time from policy approval to skill merge comes from the PR on the skill folder.
