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
- **Dropped:** a session may set `dropped` (reason in Departures) when the item turns out unnecessary. The engine then records it the way `drop` does — `status: dropped` with the reason on the default branch (team: a PR a person merges) — and closes the item's PR, so other clones leave the item alone too. When the record fails, the local state still says dropped and the run's output says how to record it.

The repository is the source of truth. A slug is done when its PR merged, or when the doc says `done` on the default branch. The engine merges only `done` docs, so a doc that reaches the default branch as `built` was merged by a person (a needs-human PR they accepted) and counts as done too. The loop also notices a person merging or closing a needs-human PR on its own, so the item leaves the "needs you" list without a `retry`.

A design session that sets `planned` but leaves Plan or Proof empty has not designed anything: the engine puts the doc back to `ready` and counts a failure. A question asked in a build session parks the item as `planned`, so the answer continues the build, not the design.

## Engine: `scripts/sdlc.py` + `scripts/sdlclib/`

The CLI subcommands are:
- `init`: adopt and migrate
- `enqueue [--new] <files>` — while a loop runs in the same checkout (no remote), docs go to `.sdlc/state/inbox/` and the loop commits them between items
- `run [--detach] [--items] [--max-items] [--max-minutes] [--dry-run]`
- `status [--json] [--no-fetch]` — fetches the remote first (10 s cap, failures ignored) so a person's merge shows at once; the go and run preflights call it too
- `stop [--after-item]` — now (the session gets SIGINT and is recorded, so it resumes) or after the current item (pause file)
- `resume` — clear the pause, start detached
- `accept <slug>…` — a `proposed` doc becomes `ready` on the default branch, the same way `enqueue` commits (solo: directly or a queue PR merged at once; team: the PR waits; a running loop takes it from the inbox)
- `answer <slug> <text>`, `retry <slug>`, `drop <slug> [reason]`, `rate <slug> good|fix|reject [note]`
  (`drop` records `status: dropped`, with the reason under Departures, on the default branch the way `accept` does, and closes the item's PR; the repository stays the source of truth)
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
  - A model-family limit switches the session to another configured model of a different family (see "Models per phase") and continues.
  - A session or weekly limit sleeps until the reset time it reports (or retries every 30 minutes), up to `limit_wait_hours`.
- **Cost:** `total_cost_usd` already includes earlier spend on a resume, so the engine records the delta per session id.
- **Cold resume:** if more than 55 minutes have passed since the stop (prompt cache gone), the engine starts a fresh "continue" session instead of `--resume`. Our logs show a cold resume cost $7–14 for 6–22 turns, against $0.62 for a warm one.
- **No progress:** if the branch head and diff hash at a stop equal the previous stop, it counts as a failure and the next session starts fresh.
- **Caps continue, they do not cut the work** (user, 2026-10-13: "비용 상한을 두면 작업중에 멈추는거아니야?", "시간상한도"). A session that hits its turn, time or budget cap is resumed at once with `--resume`, up to `loop.max_continuations` (3) times per phase. It fails only on no progress or when the phase is still unfinished after the last continuation. That last case stays a failure (user, 2026-10-14): with the default caps a phase has had four windows, up to 8 hours and 2,000 turns, and the item is still retried up to `max_failures_per_slug` times. The budget cap is off by default (`budget_usd_per_session` 0). Measured live on claude 2.1.296 (haiku, 2026-10-13): `--max-turns` and `--max-budget-usd` count per run, so a resume with the same flags gets the full caps again. A resume that started above the budget still ran and finished. The result's `total_cost_usd` and `modelUsage` are cumulative across resumes, which is why the engine records the delta. So the budget is per window: a phase can spend (1 + `max_continuations`) times it.
- **A stop is not a failure:** a SIGINT'd claude still writes a `success` result with empty text and `terminal_reason: aborted_streaming` (seen live in P7). The engine reads `terminal_reason` (`aborted*`) and exit code 130 as interrupted, records the stop and resumes it later.

**Models per phase** (user, 2026-10-12: "단계마다 모델별 세팅")
- Design and self-check run on the strongest model; build and fix run on a cheaper one. Config:
  `loop.models` = `{design: opus, build: sonnet, fix: sonnet, self_check: opus}` and
  `loop.efforts` = `{design: high, build: high, fix: medium, self_check: high}`.
- **Escalation:** a build for an item that has failed before runs on `loop.escalate_model` (opus; "" turns it off).
- The old keys still work. A string `loop.model` applies to design, build and self-check, `loop.fix_model` to fix, and a string `loop.effort` to every phase. Migration from 0.x carries them only when they differ from the 0.x defaults (opus / sonnet / high).
- A model-family limit switches the session to another configured model of a different family, else sonnet, else opus.
- **Subagents** (frontmatter `model`):
  - reviewer: opus (the quality gate)
  - verifier, simplifier: sonnet
  - researcher: haiku
  - new `sdlc-builder`: sonnet. In an interactive `/sdlc:go` the main session designs, reviews and ships, and the builder implements the plan. In the loop the build session itself runs on sonnet and does not delegate further.

**Gate (shell, no tokens)**
Every check below runs before any merge, by both `go` and `run`:
1. In the loop's checkout on `sdlc/<slug>` with the default branch merged in uncommitted (aborted afterwards — installed dependencies and caches are the session's), run the build, test and lint commands. A conflict is fixable: a fix session merges and resolves.
2. The review tally line in the work doc's `## Review` says `important=0`.
3. Test integrity. With `test_changes: none` (the default) no existing test file changes at all — an in-place weakening (`assert x == 1` → `assert True`) keeps every count, so counting is not enough. With `test_changes: allowed` no existing test file is deleted, no test cases are removed, and no skip/only/xfail/@Disabled is added; the merge (PR body, or the local squash commit) says that existing tests were allowed to change.
4. The Acceptance criteria and `test_changes` are unchanged since the design commit. Any change goes to a person, even one explained in Departures; the Departures line tells that person why. (A build or fix session cannot unlock the tests for itself.)
5. No new dependency in the manifests. A new dependency sends the PR to a human (`gate.new_dependencies: human|allow`).
6. No changed file matches `risk.human` globs, and no guard file changed: `.sdlc/config.json`, the frozen-paths file, `.claude/settings.json`, `.claude/settings.local.json`, the gate's checks directory. Either sends the PR to a human — a session must not loosen its own guardrails and merge that unattended. One exception (P7, 2026-10-13): a `.sdlc/config.json` change that only fills `commands.<name>` entries that were missing or empty at the merge base. A new app's skeleton item records its build, test and lint commands that way. It adds checks and removes none, and that item's own gate already runs the filled commands. Changing or removing a command that had a value still goes to a person. The loop re-reads the config after each merge, so the next items are gated with the filled commands.
7. `checks/*` executables pass.

Fixable failures (1, 2, 7) get a fix session on `loop.models.fix`, up to `fix_rounds`, then count as a failure. Failures for a person (3–6) send the item to "needs a person": PR comment + `sdlc:needs-human` label, item state `needs-human`, and the loop moves on. A pass leads to a merge, and the engine verifies that the merge happened. Repositories without a remote squash-merge locally (in a linked worktree: where the default branch is checked out), and the same gate applies.

**Self-check**
- After merges the engine did itself (so solo mode; in team mode a person merged and reviewed), the engine reviews the merged diff.
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
  - Test lock, during sdlc work only (an active doc applies — the same condition as design-first): new test files are always allowed; existing test files stay locked unless the doc says `test_changes: allowed`, or a person started Claude Code with `SDLC_ALLOW_TEST_EDIT=1`. Outside sdlc work the plugin does not lock tests: there is no gate behind the lock there, and the agent could switch it off in `.sdlc/config.json` anyway. The 0.x unlock file `.sdlc/state/allow-test-edit` is gone — an agent could create it itself.
  - Secret scan.
  - The approved-plan requirement now reads the active work doc.
- **guard-bash**
  - Blocks direct pushes to the default branch.
  - Blocks commands that modify or delete existing test files (`rm`, `mv`, `sed -i`, `git rm`/`checkout`/`restore`, redirection) while they are locked, and, while the active doc is `ready`/`proposed`, commands that write project files outside the work dir and `.sdlc/` (design-first).
  - Production deploy patterns need `RELEASE_APPROVAL`. This is now one tier; the staging and dev tiers and the rollback exemption are gone.
  - Secret scan on commit: the staged diff, or — when the same command line runs `git add` first, or the commit takes `-a` — everything the commit could take (`git diff HEAD` plus untracked, non-ignored files).
- **Test files** are one set for the hooks and the gate (`gate.py`'s patterns; the hook suite checks they match). **Existing test** means a test file that is present at `merge-base HEAD origin/<default>` (else the local default branch). Tests this branch added stay editable, which keeps test-first work possible. This is the same base the gate uses. Without git, a commit or a default branch, any test file on disk counts as existing. A 0.x repository's `spec/` history counts as tests (the RSpec `spec/` rule); it does not change in normal work, and a change that must edit it says `test_changes: allowed`.
- **Globs** mean one thing everywhere in the config — the frozen-paths file, `generated_paths`, `test_paths`, `risk.human` — the engine's `glob_match`: `dir/` is a prefix, `*` and `?` stay inside one path segment, `**` spans any depth, a pattern without `/` matches the file name at any depth, any other pattern is anchored at the repository root. The hook suite checks the hooks' matcher against the engine's. (0.x hooks let `*` cross `/`; an entry that relied on that needs `**`.)
- **The active doc** (`.sdlc/state/active-work`) applies only while the branch is `sdlc/<slug>`. Design-first: while that doc is `ready` or `proposed`, edits are allowed only in the work dir, `.sdlc/` and outside the project.
- **guard-bash parses commands on a best-effort basis.** It cannot see variable indirection, brace expansion, `find -exec`/`xargs`, `python -c`, `patch`/`git apply` or snapshot updates. The gate's test-integrity check (base...head) is the backstop.
- **Deploy gate per simple command** (D1, 2026-10-13). Whole-text matching blocked commit messages and searches that mention a deploy (it blocked this very work in our own sessions). The gate now matches each simple command with its quotes resolved. Text tools and git commands that only read or record are skipped. Shell arguments, wrappers, command substitutions, shell heredocs and git commands that can run a command are checked. Unsplittable input falls back to whole-text matching. Found on the way: macOS `awk` in a UTF-8 locale dies on a cut multibyte character. A long Korean text before `; rm tests/…` therefore bypassed the test lock, and a cp949 line in a staged diff hid the secrets after it. Both readers now run with `LC_ALL=C`.
- **The hooks warn early; the gate enforces.** An agent can switch the test lock and design-first off by deleting `.sdlc/state/active-work` or leaving the branch. Nothing it does that way survives the gate: checks 3, 4 and 6 compare the branch with its base and send such a change to a person.
- The guards use `onFailure: block` (fail-closed: a guard that crashes or times out blocks the tool call). The validator accepts any key, so this rests on the runtime schema of claude 2.1.296, which supports `continue`|`block`. Checked live on 2.1.296 (2026-10-13): a guard that exits 1 and a guard that times out both block the call ("blocking because onFailure is \"block\""), and every guard rule denied in a real session.
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

## Known limitations (P7, not fixed)

These were found in the live trial and the D1 work. None of them blocks the loop.
- **No progress needs a baseline.** The check compares a stop with the previous stop, so the first window has nothing to compare with. With a very small turn cap, a window that only reads is normal. Two such windows in a row fail the phase. The default caps make this unlikely.
- **`events.jsonl` does not mark a continued window.** A window that hit its cap is logged as `max_turns`, whether the engine continued it or not. The run log says which.
- **`num_turns` is not the cap unit.** claude reports N + 1 turns for a window capped at N.
- **Session logs are not pure JSONL.** claude may print a plain-text warning first: `Ignoring N permissions.allow entries from .claude/settings.json: this workspace has not been trusted`. In an untrusted workspace, loop sessions do not get the project's allow list. The engine's `--allowedTools` covers what they need.
- **A refused start replaces "last stop".** When `run` refuses to start (on a dirty tree, for example), `status` shows the refusal as the last stop.
- **The deploy gate does not see:**
  - a command made from a text tool's output (`$(echo …)`, or an unquoted `VAR=$(…)` in front of git)
  - a deploy split across two commands (`cd prod && make deploy`)
  - abbreviated long options and bundled short options of git (`--upl`, `-qu cmd`)
  - an alias, pager or hook already set in the repository's `.git/config`

  The gate is a guard against accidental deploys, not a sandbox.

## Phases / progress

Tests: `plugins/sdlc/tests/py` (stdlib unittest, stub `claude` and `gh` in `tests/py/stubs`), run on Python 3.9 and 3.12.

- [x] P1 Python engine core: config, work docs, git/gh, claude runner + result parsing, gate, loop, status, enqueue, stop/resume, unit tests with stub claude/gh
- [x] P2 init (python): detect, scaffold, migrate v1 → v2, `--check`; github-setup.sh kept
- [x] P3 skills: init, go (+ references), run (fill + start + stop/resume), status; agents (reviewer validate mode); delete the old skills and diagnoser
- [x] P4 hooks adapted (work doc, test lock add-only + bash bypass, one deploy tier) + bash hook tests updated
- [x] P5 templates (work.md, CLAUDE.sections, REVIEW.md, config v2), delete old templates and scripts
- [x] P6 README (ko), CHANGELOG 1.0.0, marketplace/plugin manifests, docs images
- [x] P7 full test run + `claude plugin validate --strict`; end-to-end trial on a scratch repo without a remote (real claude, a small app); fix what it finds
  - The trial (2026-10-13, claude 2.1.296, $4.00) found ten problems; all are fixed.
  - A re-run ($2.00) confirmed four of the fixes:
    - the skeleton item merged unattended, and its gate ran the commands it filled in
    - a stop resumed the same session and counted no failure
    - bytecode left by a build did not block a resume
    - a turn cap of 3 continued the same session through design and build to a merge
  - The deploy gate now matches each simple command (D1).
  - `tests/run.sh` passes:
    - hooks 1816
    - scripts 91
    - engine 475 tests on Python 3.12 and 3.9
    - `plugin validate --strict` and the marketplace
  - The limitations still open are listed under Known limitations.
- [ ] P8 hand-off to the user: migration notes per repo (pilot, solar-next, …), merge to main, push and update only on the user's go-ahead
