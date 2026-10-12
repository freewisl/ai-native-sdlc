# sdlc 1.0 redesign: design and progress

Branch `redesign-1.0`. Read this first when resuming. The checklist at the bottom is the progress record.

## Why

- **The user said it plainly:** "사용법이 너무 복잡해" (2026-10-12). Usage is too complex.
- **What the usage logs of 6 repos show:**
  - 19 skills exist. The monitor, triage, postmortem, release, scan and metrics skills were never run, and doctor and evals ran only 2–7 times.
  - Approvals were self-approvals.
  - The user hand-wrote 13 runner scripts to keep the loop going.
  - The merge gate was the session's own report.
- **Where the field is converging** (research on 2026-10-12): small units in fresh sessions; plan files; mechanical verification over documents; risk-tiered human gates with sampling; lessons turned into checks.

## Decisions (user, 2026-10-12)

1. Team repositories use the same flow and the same commands. A human approves only the design and the merge.
2. Ops and enterprise features are deleted: monitor, triage, postmortem, release, policy, evals, managed settings, the CI (github) mode and the GitHub workflow templates.
3. The loop engine is rewritten in Python (stdlib only, 3.9+).
4. There is no interim 0.8.4. Work goes straight to the redesign, which ships as 1.0.0.

## User surface

| Say / type | What happens |
|---|---|
| `/sdlc:init` | Once per repository: adopt, detect commands, migrate an old layout |
| `/sdlc:go "…"` or "X 해줘" | One change in this session: work doc → build → shell gate → PR → merge (team: a human merges) |
| `/sdlc:run "goal"` or "X 만들어, 알아서 채우고 돌려" | Split the goal into work items, enqueue them, and start the background loop |
| "큐에 넣어" | Enqueue only |
| "어디까지 됐어?" / `/sdlc:status` | Progress, what needs you, cost |
| "멈춰" / "이어서 돌려" | Pause or resume the loop |

There are 4 skills: init, go, run and status. The detailed protocols live as reference files under `skills/go/`.

## Work document: `work/<slug>.md`

One file replaces the intent, spec and plan files.

```markdown
---
slug: entry-crud
title: 수입·지출 입력/수정/삭제
status: ready          # proposed | ready | planned | done | dropped | needs-input
depends_on: []
test_changes: none     # none | allowed (design decides; existing test files stay locked otherwise)
created: 2026-10-12T09:30:00Z
source: user           # user | goal:<id> | self-check
---
## Goal
## Acceptance criteria        (R1. WHEN … THE SYSTEM SHALL … — frozen once planned)
## Plan                       (numbered steps, each with how it is checked)
## Proof                      (| R | test or command | kind |)
## Departures
## Learned
## Questions                  (needs-input only: multiple choice, recommended answer first)
```

**Lifecycle**
- **Solo:** an item is enqueued as `ready`.
- **Team:** an item is enqueued as `proposed`; a human merging the enqueue PR is the approval, and the merge sets it to `ready`.
- **Design session:** fills in the Plan and Proof sections and commits the doc as `planned` on `sdlc/<slug>`. In team mode it also opens a draft PR, and a human's PR approval lets the build start.
- **Build session:** writes the code and the PR.
- **Gate:** when it passes, the PR is merged and the doc becomes `done` in the merge commit.
- **Needs input:** the item is parked and its question goes to status. It is not counted as a failure.

The repository is the source of truth. A slug is done when its PR merged, or when the doc says `done` on the default branch.

## Engine: `scripts/sdlc.py` + `scripts/sdlclib/`

The CLI subcommands are:
- `init`: adopt and migrate
- `enqueue <files>`
- `run [--detach]`
- `status [--json]`
- `gate <pr|branch>`
- `stop`
- `resume`

**Loop**
- With `--detach`, the loop runs under nohup, writes its pid and `loop-status.json`, and continues until the queue is empty.
- If only waiting items are left (dependencies or human approval in another loop), it polls again every 10 minutes. If only items waiting on a human are left, it exits and reports them.
- It also stops for: the pause file, N consecutive failures, a usage limit it cannot wait out (`loop.limit_wait_hours`), or `max_minutes` (default: unlimited).

**Per item**
1. Design session, unless the item is already planned.
2. Build session.
3. Gate.
4. Merge, or a fix session when a test fails (`fix_rounds`), or "needs human" with the reasons.

**Sessions**
- Run with `claude -p --output-format stream-json --verbose --session-id <uuid> --max-turns N --permission-mode acceptEdits --permission-prompts none --model M --effort E`.
- The whole stream goes to the log.
- **Stopping at the time cap:** SIGINT, then 30 s, SIGTERM, 10 s, SIGKILL.
- **Usage limits:** limit messages for session, weekly, Opus, Sonnet and usage/spend are parsed.
  - A model-family limit switches the session to `loop.fix_model` (or another family) and continues.
  - A session or weekly limit sleeps until the reset time it reports (or retries every 30 minutes), up to `limit_wait_hours`.
- **Cost:** `total_cost_usd` already includes earlier spend on a resume, so the engine records the delta per session id.
- **Cold resume:** if more than 55 minutes have passed since the stop (prompt cache gone), the engine starts a fresh "continue" session instead of `--resume`. Our logs show a cold resume cost $7–14 for 6–22 turns, against $0.62 for a warm one.
- **No progress:** if the branch head and diff hash at a stop equal the previous stop, it counts as a failure and the next session starts fresh.

**Gate (shell, no tokens)**
Every check below runs before any merge, by both `go` and `run`:
1. In a temporary worktree at the PR head with the default branch merged in, run the build, test and lint commands. A conflict starts a short rebase session.
2. The review tally line in the PR body says `important=0`, and the findings were validated.
3. Test integrity: no existing test file is deleted, no test cases are removed, and no skip/only/xfail/@Disabled is added.
4. The Acceptance criteria are unchanged since the design commit, or the change is explained in Departures.
5. No new dependency in the manifests. A new dependency sends the PR to a human (`gate.new_dependencies: human|allow`).
6. No changed file matches `risk.human` globs. A match sends the PR to a human.
7. `checks/*` executables pass.

A pass leads to a merge, and the engine verifies that the merge happened. A failure leads to the "needs human" label and a PR comment listing the reasons. Repositories without a remote merge locally with a squash merge, and the same gate applies.

**Self-check**
- After merges, the engine reviews the merged diff.
- Only validated Important findings that touch changed lines become new items, at most 3 per pass, deduplicated against existing items.
- Pre-existing problems become `proposed` items, even in solo mode.

**Status**
The status view shows:
- whether the loop is running, and the current item, phase and elapsed time
- counts by status
- a "needs you" list: questions, PRs waiting for a human, blocked items
- cost (today, and the median per item)
- recent merges
- a review sample: a few auto-merged PRs from the last 7 days that nobody has rated yet

**Init**
- Detects the stack and commands (ported from `detect_project`) and writes `.sdlc/config.json` (version 2), the CLAUDE.md sections, REVIEW.md and `work/README.md`.
- Merges the `.claude/settings.json` permissions.
- Sets up the GitHub ruleset and auto-merge (`github-setup.sh`, kept).
- When `AGENTS.md` exists, CLAUDE.md gets `@AGENTS.md`.
- `--check` reports health. It replaces the doctor skill.

**Migration** (init on a version-1 repository)
- Every approved intent whose plan is not implemented becomes `work/<slug>.md`: the intent's Problem and Outcome become the Goal, and the spec's requirements become the Acceptance criteria when a spec exists. `depends_on` is kept.
- If the old plan is approved on a `sdlc/<slug>` branch, the item becomes `planned` and its Plan section points to that plan file.
- Done history in `intent/`, `spec/` and `plan/` is left as it is.
- `loop.*` and `commands.*` are kept, and `roles.solo` maps to `mode`.

## Hooks (bash, kept and adapted)

- **guard-edit**
  - Frozen and generated paths stay blocked.
  - Test lock: new test files are always allowed; existing test files stay locked unless the active work doc says `test_changes: allowed` or the bug-fix unlock is set.
  - Secret scan.
  - The approved-plan requirement now reads the active work doc.
- **guard-bash**
  - Blocks direct pushes to the default branch.
  - Blocks commands that modify or delete existing test files (`rm`, `mv`, `sed -i`, `git rm`/`checkout`/`restore`, redirection) while they are locked.
  - Production deploy patterns need `RELEASE_APPROVAL`. This is now one tier; the staging and dev tiers and the rollback exemption are gone.
  - Secret scan on commit.
- The hooks file uses `onFailure: block` on the guards, if `claude plugin validate` accepts it.
- post-edit, post-bash, stop-verify, session-start and session-end are kept. Their context text is updated.

## Agents

- researcher (haiku), reviewer, verifier and simplifier are kept.
- The reviewer gains a validate mode: given one finding, it tries to refute it in fresh context and keeps the finding only with a concrete failure demonstration.
- The reviewer also gains a pre-existing grade and an exclusion list.
- diagnoser is deleted.

## Deleted

- **Skills:** intent, spec, plan, verify, review, release, monitor, triage, postmortem, metrics, evals, lesson, policy, scan, doctor. Their essentials move into the `go` references or into `init --check` and `status`.
- **Scripts:** run-loop.sh, init.sh, status.sh, doctor.sh, metrics.py, monitor.py, run-evals.sh, review-gate.sh.
- **Templates:** github/*, examples/*, policies/*, evals/*, config/{bands,managed-settings,mcp.deploy.example,otel.env,settings.sandbox}.json, {en,ko}/{APPROVALS,intent-README,intent,spec,plan,LESSONS,evals-README}.md.
- **Agent:** diagnoser.
- The plugin-level trigger evals are rewritten in the current `prompt.md` + `graders/` format.

## Phases / progress

- [ ] P1 Python engine core: config, work docs, git/gh, claude runner + result parsing, gate, loop, status, enqueue, stop/resume, unit tests with stub claude/gh
- [ ] P2 init (python): detect, scaffold, migrate v1 → v2, `--check`; github-setup.sh kept
- [ ] P3 skills: init, go (+ references), run (fill + start + stop/resume), status; agents (reviewer validate mode); delete the old skills and diagnoser
- [ ] P4 hooks adapted (work doc, test lock add-only + bash bypass, one deploy tier) + bash hook tests updated
- [ ] P5 templates (work.md, CLAUDE.sections, REVIEW.md, config v2), delete old templates and scripts
- [ ] P6 README (≈150 lines, ko), CHANGELOG 1.0.0, marketplace/plugin manifests, docs images
- [ ] P7 full test run + `claude plugin validate --strict`; end-to-end trial on a scratch repo without a remote (real claude, a small app); fix what it finds
- [ ] P8 hand-off to the user: migration notes per repo (pilot, solar-next, …), merge to main, push and update only on the user's go-ahead
