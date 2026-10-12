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
status: ready          # proposed | ready | planned | built | done | dropped | needs-input
depends_on: []
test_changes: none     # none | allowed (design decides; existing test files stay locked otherwise)
created: 2026-10-12T09:30:00Z
source: user           # user | goal:<id> | self-check
---
## Goal
## Acceptance criteria        (R1. WHEN … THE SYSTEM SHALL … — frozen once planned)
## Plan                       (numbered steps, each with how it is checked)
## Proof                      (| R | test or command | kind |)
## Review                     (findings + `SDLC_REVIEW_TALLY important=N nits=M validated=yes` — the gate reads it)
## Departures
## Learned
## Questions                  (needs-input only: multiple choice, recommended answer first)
```

**Lifecycle**
- **Enqueue** = commit the doc to the default branch as `ready`. Solo: directly (no remote) or a queue PR merged at once. Team: the queue PR waits for a person; merging it is the approval. `proposed` is for ideas a person still has to accept (self-check's pre-existing problems, rejected-review follow-ups).
- **Design session** (engine creates `sdlc/<slug>` first): fills Plan and Proof, commits the doc as `planned`. Team: the engine opens a draft PR; an approving review by someone else, `reviewDecision: APPROVED`, or the `sdlc:approved` label lets the build start.
- **Build session:** writes code and tests, verifies, reviews (reviewer + validate), records the tally in `## Review`, commits the doc as `built`. It does not push or open the PR.
- **Gate (engine):** passes → the engine commits `done` on the branch, pushes, opens/updates the PR and merges it (team: marks it ready, a person merges). The merge commit carries `done`.
- **Needs input:** a session sets `needs-input` and writes `## Questions`; the item is parked (not a failure). `sdlc.py answer` stores the answer; the engine writes it into the doc and continues.
- **Dropped:** a session may set `dropped` (reason in Departures) when the item turns out unnecessary.

The repository is the source of truth. A slug is done when its PR merged, or when the doc says `done` on the default branch.

## Engine: `scripts/sdlc.py` + `scripts/sdlclib/`

The CLI subcommands are:
- `init`: adopt and migrate
- `enqueue [--new] <files>` — while a loop runs in the same checkout (no remote), docs go to `.sdlc/state/inbox/` and the loop commits them between items
- `run [--detach] [--items] [--max-items] [--max-minutes] [--dry-run]`
- `status [--json]`
- `stop [--after-item]` — now (the session gets SIGINT and is recorded, so it resumes) or after the current item (pause file)
- `resume` — clear the pause, start detached
- `answer <slug> <text>`, `retry <slug>`, `drop <slug> [reason]`, `rate <slug> good|fix|reject [note]`
- `gate <slug>`, `ship <slug>` — the end of an interactive `/sdlc:go`: the same gate and merge the loop uses

Engine state (`.sdlc/state/items.json`) holds only what the repository cannot say: failures, waits and their reasons, pending answers, the last stopped session, cost. Claims live in the git common dir.

**Loop**
- With `--detach`, the loop runs under nohup, writes its pid and `loop-status.json`, and continues until the queue is empty.
- If only waiting items are left that resolve without anyone here (team PR approvals and merges, another loop's item), it polls every `poll_minutes` (10) for up to `idle_hours` (12). If only items waiting for you are left (questions, gate reasons, blocked), it exits and says so.
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
1. In the loop's checkout on `sdlc/<slug>` with the default branch merged in uncommitted (aborted afterwards — installed dependencies and caches are the session's), run the build, test and lint commands. A conflict is fixable: a fix session merges and resolves.
2. The review tally line in the work doc's `## Review` says `important=0`.
3. Test integrity: no existing test file is deleted, no test cases are removed, and no skip/only/xfail/@Disabled is added.
4. The Acceptance criteria are unchanged since the design commit, or the change is explained in Departures.
5. No new dependency in the manifests. A new dependency sends the PR to a human (`gate.new_dependencies: human|allow`).
6. No changed file matches `risk.human` globs. A match sends the PR to a human.
7. `checks/*` executables pass.

Fixable failures (1, 2, 7) get a fix session on `loop.fix_model`, up to `fix_rounds`, then count as a failure. Failures for a person (3–6) send the item to "needs a person": PR comment + `sdlc:needs-human` label, item state `needs-human`, and the loop moves on. A pass leads to a merge, and the engine verifies that the merge happened. Repositories without a remote squash-merge locally (in a linked worktree: where the default branch is checked out), and the same gate applies.

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

Tests: `plugins/sdlc/tests/py` (stdlib unittest, stub `claude` and `gh` in `tests/py/stubs`), run on Python 3.9 and 3.12.

- [x] P1 Python engine core: config, work docs, git/gh, claude runner + result parsing, gate, loop, status, enqueue, stop/resume, unit tests with stub claude/gh
- [ ] P2 init (python): detect, scaffold, migrate v1 → v2, `--check`; github-setup.sh kept
- [ ] P3 skills: init, go (+ references), run (fill + start + stop/resume), status; agents (reviewer validate mode); delete the old skills and diagnoser
- [ ] P4 hooks adapted (work doc, test lock add-only + bash bypass, one deploy tier) + bash hook tests updated
- [ ] P5 templates (work.md, CLAUDE.sections, REVIEW.md, config v2), delete old templates and scripts
- [ ] P6 README (≈150 lines, ko), CHANGELOG 1.0.0, marketplace/plugin manifests, docs images
- [ ] P7 full test run + `claude plugin validate --strict`; end-to-end trial on a scratch repo without a remote (real claude, a small app); fix what it finds
- [ ] P8 hand-off to the user: migration notes per repo (pilot, solar-next, …), merge to main, push and update only on the user's go-ahead
