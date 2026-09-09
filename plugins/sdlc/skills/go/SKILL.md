---
name: go
description: Runs the whole AI-native SDLC loop for one change from a single sentence — captures the intent, writes the spec and the plan, implements, verifies, reviews and opens the pull request in one session, stopping only where a human decision is genuinely needed (policy conflict, verification that keeps failing, production deploy, merge). Use when the user says "just do it end to end", "run the loop for this", "/sdlc:go <request>", "이거 끝까지 해줘", "한 바퀴 돌려", "알아서 진행해", "귀찮으니 자동으로".
argument-hint: "<one-sentence request> [--autopilot] [--merge] [--no-pr] [--slug <slug>]"
allowed-tools: Read, Write, Edit, Glob, Grep, Task, Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(git *), Bash(gh pr *), Bash(mkdir -p .sdlc/logs*), Bash(printf *)
---

# /sdlc:go — the loop in one command

## What changes
- **Traditional (and the step-by-step skills):** each stage is a separate command and a separate "yes" — fine for a team where different people own the gates, tiring for one person who owns all of them.
- **AI-native, solo:** one request produces the whole committed artifact chain (intent → spec → plan → diff + tests → PR with review findings) and the human is asked only where judgment is genuinely theirs. The artifacts and gates are unchanged; only the ceremony is removed.

## When to use
Any bounded change in a repository that has run `/sdlc:init`. Best for work that fits one PR. For large or risky work, use the stage skills one by one — the pauses are the point there.
Prerequisites: `.sdlc/config.json` — `/sdlc:init` is user-only (`disable-model-invocation`), so if it is missing stop and ask the user to type `/sdlc:init`; never scaffold by other means; for the PR steps, `gh` authenticated. The project's own build/test/lint commands are pre-approved through `.claude/settings.json` permissions that `init` merged; `allowed-tools` above pre-approves the plugin scripts, git and gh for this turn.

## Inputs
- `$ARGUMENTS`: the request in one sentence; `--autopilot` = do not pause at the plan (write and commit plan.md, then implement; only exceptions stop the run); `--merge` = enable auto-merge when checks are green (solo repositories only); `--no-pr` = stop after the verified commit; `--slug` = reuse an existing chain (when it names an approved intent and no request text follows, the intent's title and Problem section are the request — this is how `/sdlc:run` calls this skill).
- `.sdlc/config.json`: `roles.solo`, `roles.autopilot` (default for `--autopilot`), `commands.*`, `policies`, `paths.*`, `auto_commit`.

## Steps
0. **Preflight** (no questions). `bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh" --json`: if `.sdlc/config.json` is missing, stop with one line — "run `/sdlc:init` first (it auto-detects GitHub, solo and CI auth); it has to be typed by you" — because init is reserved for explicit user invocation and cannot be called from a skill. Read `roles.solo` and `roles.autopilot`. If the repo is not git, stop: the loop needs a commit trail. Derive `slug` from the request (kebab-case, ≤ 5 words) unless `--slug`. Record `started` (ISO time) for the `go.run` event.
   **Autopilot eligibility**: autopilot applies only when (a) `--autopilot` was typed by the user, or the user invoked `/sdlc:go` directly and `roles.autopilot` is true — never when this skill was auto-invoked by the model from a phrase; (b) `roles.solo` is true; (c) doctor's `auto_mode` check is `ok` (the playbook's four guardrails: tuned CLAUDE.md, policy skills, hooks, a test suite Claude can run). If any fails, say which in one line and keep the plan pause.
   **Branch first**: `git checkout -b sdlc/<slug>` now — every commit of this run (artifacts included) lands on this branch, so nothing is written to the default branch and the push guard never has to block you.
1. **Intent** (Stage 1). Write `<intent>/<slug>.md` from the request plus what the repository itself answers (affected modules from a quick `sdlc-researcher` pass, constraints from CLAUDE.md). Ask the user at most **two** questions and only if the request is ambiguous about scope or success criteria; otherwise ask none. In solo mode set `status: approved` immediately with `approved_by: <git config user.name> (via /sdlc:go)` and `approval_basis: "user request: <the sentence>"` — the request is the product owner's approval (`/sdlc:intent` run on its own still asks once even in solo mode; `go` replaces that question with the original request) — and commit `intent(<slug>): …`. Non-solo: pause once — "approve this intent?" — because a different person owns it.
2. **Spec** (Stage 2). Apply every skill in `config.policies` as a constraint, write `<spec>/<slug>.md` with requirements, design and the **Flagged concerns** table. **Stop only if** a row is a real policy conflict or a contradicting requirement; present those rows and take the decision. Otherwise `status: approved`, commit.
3. **Plan** (Stage 3). Write the four sections (Files that change / Order of work / Risks / Proof) and answer the three questions (what could break, riskiest step, alternatives not chosen).
   - autopilot eligible (step 0): commit `plan/<slug>.md` with `status: approved`, `approved_by: <git config user.name> (autopilot)`, `approval_basis: "user request: <sentence>"`, append a `plan.approve` event with `detail.autopilot: true`, set `.sdlc/state/active-plan`, and continue — the user's request stands as the acceptance; the plan is still the audit record and the reviewer checks the diff against it.
   - otherwise: `EnterPlanMode`, present the plan, `ExitPlanMode`; this is the one pause the playbook keeps for engineers ("nothing is implemented without an accepted plan").
   Split independent file sets and mention that they could run as parallel worktrees, but run them sequentially here.
4. **Implement** (Stage 3) on the branch from step 0, following the plan; whenever the implementation departs, update "Departures from plan" in the same commit. Never edit test files unless the plan says `test_changes: allowed`; for a bug fix use the failing-test-first protocol from `/sdlc:verify --bugfix` — and because nobody is watching in autopilot, when that protocol unlocks the test lock record it: a line in the plan's Departures, a `verify.test_unlock` event, and "test files changed" in the reviewer's brief and the PR body.
5. **Verify** (Stage 4). Run `commands.build/test/lint` (or `verify.commands`); on failure fix the code and rerun, up to **3 rounds**; then launch the `sdlc-verifier` agent for a fresh-context check. **Stop only if** verification still fails after 3 rounds — show the literal output and ask how to proceed (this is a real decision, not ceremony).
6. **Review** (Stage 5). Launch `sdlc-reviewer` against REVIEW.md; fix every Important finding and re-verify; leave nits as PR comments unless trivial. If a finding repeats one already in CLAUDE.md, run `/sdlc:lesson`.
7. **Pull request** (unless `--no-pr`). Set `status: implemented` in `plan/<slug>.md` (once the PR merges, this is how `/sdlc:run` and `/sdlc:status` know the item is done), commit, `git push -u origin sdlc/<slug>`, `gh pr create` with body: intent/spec/plan links, the Proof output, the review tally line (`SDLC_REVIEW_TALLY …`). Then run the babysit loop from `/sdlc:review --pr <n>` once: address `sdlc review` workflow comments and failing checks until green.
   - `--merge` and `roles.solo`: `gh pr merge --auto --squash --delete-branch <n>`. GitHub accepts auto-merge only when `allow_auto_merge` is on **and** the base branch is protected; when it refuses (typically a private Free-plan repository without rulesets — "clean status" / "not allowed"), do not merge blind: `gh pr checks <n> --watch` until every check has completed green, then `gh pr merge --squash --delete-branch <n>`. If a check fails, fix and re-push (babysit loop) — never merge with a failing or still-running check. Otherwise **stop** here: "PR #n is green and waiting for your merge" — merging is the human gate the playbook keeps.
8. **Report** in ≤ 12 lines: slug, the four artifact paths and commits, verify output tail, review tally, PR URL, cost/turn note, and what (if anything) needs a human now. Suggest `/sdlc:release staging` when a deploy command exists.

## Stop conditions (the only places a human is asked)
| Where | Why it is a real decision |
|---|---|
| Intent ambiguity (≤ 2 questions) | Scope or success criteria cannot be inferred |
| Intent approval — non-solo repositories only | A different person (the product owner) owns it |
| Flagged concern: policy conflict or contradicting requirements | A policy owner must decide |
| Plan — unless autopilot is eligible (`--autopilot`, `roles.solo`, doctor `auto_mode` ok) | The playbook's build gate |
| Verification failing after 3 rounds | Continuing would mean weakening the check |
| Merge (unless `--merge`) and any production deploy (`RELEASE_APPROVAL`) | Separation of duties |

## Output
The committed chain (`intent/ spec/ plan/`), a verified branch, an open PR with review findings, and the short report. Nothing is left half-done: if a stop condition fires, say exactly what is needed and what already exists.

## Governance
- Autopilot removes ceremony, not controls: hooks still block frozen paths, test edits, secrets, direct pushes to the default branch and ungated production commands; the reviewer still judges the diff against plan.md; the PR still goes through branch protection.
- Never approve or merge without `--merge` given by the user; never set `RELEASE_APPROVAL`; never disable a hook to get past a block — report it instead.
- Non-solo repositories keep the intent and plan pauses even with `--autopilot`.
- A `go` the model started on its own (from a phrase, not a typed `/sdlc:go`) never uses autopilot, whatever `roles.autopilot` says.
- Branch protection note: on a solo repository the ruleset requires 0 approvals but still requires the `evals` status check; with `github-setup.sh --bypass` the admin can skip that too — then evals is advisory, and the report must say so.

## Measurement
Append `go.run` to `.sdlc/logs/events.jsonl` with `detail: {slug, autopilot, stops: [...], verify_rounds, pr, started, duration_min}` (`duration_min` = minutes from `started` to the PR or the final stop) and let the stage skills' events (intent.write, spec.*, plan.*, verify.run, review.run) carry the per-stage numbers; `/sdlc:metrics` reads `go.run` for "Request → PR (median min)" — the end-to-end leading indicator.
