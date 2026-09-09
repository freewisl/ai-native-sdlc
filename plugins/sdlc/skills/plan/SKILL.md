---
name: plan
description: Runs Claude Code plan mode as the default starting point for implementation (Stage 3 Build) — reads the approved spec and intent, produces a plan naming the files that change, the order of work, the risks and the proof, interrogates it, commits the approved plan as plan/<slug>.md, sets it as the active plan for the hooks, then implements and keeps plan.md in sync. Use when the user says "plan this change", "make an implementation plan for <slug>", "start in plan mode", "implement the spec", "plan then build", "계획 세워", "구현 계획 작성", "플랜 모드로 시작", "스펙대로 구현해".
argument-hint: "<slug> [--allow-test-changes] [--no-implement]"
---

# /sdlc:plan — nothing is implemented without an accepted plan

## What changes
- **Traditional:** An engineer reads the design and starts writing code. How the change will be made, down to which files and which tests, stays in the engineer's head or at best a ticket comment. Nobody else can review it. The first thing a reviewer sees is the finished diff, and by then rework is slow.
- **AI-native:** Work starts with a written plan that Claude produces in plan mode, where it can read the codebase without changing anything. The engineer corrects the plan before code is written, and the approved version is committed as plan.md for later stages to check against.


## When to use
When a spec has `status: approved` (accepting the spec starts this play), or for any change the engineer
wants planned before code is written. Prerequisites: the spec/intent if they exist; CLAUDE.md helps.

Prerequisites: The intent artifact (intent.md or spec.md) if one exists, and the CLAUDE.md file helps. Plan mode is itself a start-anywhere play in the dependency figure.
Infrastructure: Claude Code with access to the repository.

## Inputs
- `$ARGUMENTS`: `<slug>`; `--allow-test-changes` when the change legitimately edits tests (new tests for
  new behavior); `--no-implement` to stop after committing the plan.
- `<spec>/<slug>.md`, `<intent>/<slug>.md` (paths from `.sdlc/config.json`), `CLAUDE.md`.
- Template `${CLAUDE_PLUGIN_ROOT}/templates/<lang>/plan.md` (fallback `en`), conventions in
  `${CLAUDE_PLUGIN_ROOT}/skills/intent/references/artifact-conventions.md`.

## Steps
1. **Enter plan mode.** If the session is not already in plan mode, call `EnterPlanMode`. Plan mode is the
   enforcement: nothing can be edited until the engineer accepts the plan.
2. **Read the inputs.** Spec (require `status: approved`; otherwise say so and continue only on the user's
   explicit override, recorded in the plan), intent, CLAUDE.md, and the code the spec touches. Use the
   `sdlc-researcher` agent for broad exploration so the main context stays focused.
3. **Draft the plan** with exactly the four sections of the playbook's plan.md, then the Departures section:
   - **Files that change** — every file, `(new)` or `(modified)`, grouped by component.
   - **Order of work** — numbered steps, each independently verifiable (e.g. "1. Add the status endpoint
     behind existing auth. 2. Panel against the endpoint. 3. Wire into the portal nav.").
   - **Risks** — what could break, which step is most risky, what was considered and not chosen.
   - **Proof** — the tests that prove it and any visual check, quantified ("test_status.py covers the four
     claim states; screenshot matches the approved mock").
4. **Interrogate the plan** before showing it — answer in the Risks section: What could this change break?
   Which step is the most risky? What other options were considered and not chosen, and why?
5. **Iterate** with the engineer until an engineer who has never seen the conversation could implement the
   change from the plan alone. Decide `test_changes`: `forbidden` by default; `allowed` only when tests must
   change (new tests, updated fixtures for new behavior) — say why in Proof. Bug fixes keep `forbidden` and
   use `/sdlc:verify --bugfix`.
6. **Get approval** by calling `ExitPlanMode` with the plan. On acceptance:
   - write `<plan>/<slug>.md` from the template with `status: approved`, `approved_by: <git config user.name>`,
     `links.intent/spec`, `test_changes`, `{{spec_path}}` filled; heading `# Plan: <title> (from <spec path>)`;
   - `mkdir -p .sdlc/state && printf '%s' "<plan>/<slug>.md" > .sdlc/state/active-plan` (the edit guard and the
     Stop hook read this);
   - commit `plan(<slug>): <title>` (when `auto_commit`).
   If the plan is rejected, revise and repeat from step 4. Never write the plan file before acceptance.
7. **Implement** in the planned order (skip with `--no-implement`). Follow CLAUDE.md conventions; run the
   verify commands as each step completes. Whenever implementation departs from the plan (extra file,
   reordered step, dropped item), update **Departures from plan** in `plan/<slug>.md` in the same commit as
   the code change — the `plan.enforce_sync` hook asks for this at stop time when enabled.
8. **Finish.** Set `status: implemented` in the plan frontmatter, commit, then run `/sdlc:verify` (Skill
   tool) and include its output; suggest `/sdlc:review` next. Leave `.sdlc/state/active-plan` in place until
   the review is done (the reviewer's Compliance pass reads it).

## Parallel work
When the plan's **Files that change** splits into sets that do not overlap, say so in the Output: those sets can run
as parallel sessions, each in its own worktree (`claude --worktree <task>`); tasks that share files run in one
session, one after another. Two or three sessions is a sensible starting point — add sessions only while review
keeps up.

## Output
The approved plan (path, content), the commit hash, the active-plan marker, then the implementation
summary with the Departures section and the `/sdlc:verify` result.

## Governance
- **Auto mode readiness (checklist).** Auto-accept (Claude applies each change without a per-edit prompt) becomes the
  default for routine work only when the guardrails exist — (1) a tuned CLAUDE.md, (2) skills that encode policy,
  (3) hooks that block unsafe actions, (4) a test suite Claude can run — and the task qualifies: a tight spec.md, a
  small blast radius, code the tests already cover. `/sdlc:doctor` reports `auto mode readiness`. In auto mode the
  engineer reviews artifacts after the session (diff, verify output, plan Departures) instead of watching edits;
  with worktrees this is what makes individual and team parallelism — and the autonomous Stage 6 loop — possible.
- Design review happens before code is generated: the engineer approves routine plans; anything the
  organization classes as higher risk goes to a tech lead or architect — record that person in
  `approved_by`. The plan and its revisions are in git along with who accepted them.
- The reviewer checks the eventual diff against this plan; keep it truthful rather than tidy.
- Do not edit tests when `test_changes: forbidden`; do not bypass the edit guard by asking the user to
  set `SDLC_ALLOW_TEST_EDIT` — change the plan instead and say why.

## Measurement
Append (conventions §8): `plan.approve` with `detail: {"slug": "...", "approved_by": "...", "test_changes": "..."}`
after step 6; `plan.implemented` with `detail: {"slug": "...", "departures": n}` after step 8.
Feeds Stage 3 in `/sdlc:metrics`: leading — plan approval → merged PR time and first-pass merge share;
lagging — rework cycles and whether the merged diff still matches plan.md (Departures count).
