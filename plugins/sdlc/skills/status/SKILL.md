---
name: status
description: Shows the state of the artifact chain — for every slug, whether intent.md, spec.md and plan.md exist, their status and timestamps, which plan is active for the hooks, and how much spec rework happened after planning started. Use when the user asks "where are we on <slug>", "show the sdlc status", "what is in flight", "which plan is active", "체인 상태 보여줘", "진행 상황", "활성 플랜 뭐야".
argument-hint: "[<slug>]"
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/*")
---

# /sdlc:status — the artifact chain at a glance

## Live data
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/status.sh" $ARGUMENTS 2>&1 || true`

## When to use
Before starting work (which stage is this change in?), during stand-ups, or when a hook mentions the active plan.

## Inputs
- `$ARGUMENTS`: optional `<slug>` for one chain with file paths and the list of spec commits after the first plan commit.
- Reads `.sdlc/config.json` paths, the artifact frontmatter, `.sdlc/state/active-plan`, and git history.

## Steps
1. If the live data above is missing or errored, run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/status.sh" $ARGUMENTS` and show the output.
2. Interpret it: for each slug say what the next gate is (draft intent → product owner approval → `/sdlc:spec`; approved spec → `/sdlc:plan`; approved plan → implement → `/sdlc:verify` → `/sdlc:review`).
3. Flag stale chains: `rework > 0` means spec.md changed after planning started (the Stage 2 lagging indicator); an active plan whose file has `status: implemented` means `.sdlc/state/active-plan` should be cleared or moved on.
4. If the project has no `.sdlc/config.json`, say so and point to `/sdlc:init`.

## Output
The table (slug | intent | spec | plan | active | rework) plus one line per slug naming the next action and who owns it.

## Governance
Read-only. Status changes happen only through `/sdlc:intent approve`, `/sdlc:spec`, `/sdlc:plan` and human commits.

## Measurement
No events written. The rework column feeds the Stage 2 lagging indicator in `/sdlc:metrics`.
