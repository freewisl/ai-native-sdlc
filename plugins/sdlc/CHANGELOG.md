# Changelog

All notable changes to the `sdlc` plugin are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-14

A redesign, not an increment. 0.x was too complicated to use ("사용법이 너무 복잡해"): 19 skills became 4, the intent, spec and plan files became one work doc, the merge gate moved from the session's own report to the shell, and the loop engine was rewritten in Python.
Run `/sdlc:init` once in each repository that used 0.x (see Migration).

### Changed
- **Breaking:** four skills — `init`, `go`, `run`, `status` — replace the 19 of 0.x. Slash commands and plain requests ("X 해줘", "어디까지 됐어?", "멈춰") reach the same skills; the detailed protocols moved into `skills/go/references/`.
- **Breaking:** one work doc per change, `work/<slug>.md` (Goal, Acceptance criteria, Plan, Proof, Review, Departures, Learned, Questions), replaces the `intent/`, `spec/` and `plan/` files. Statuses: `proposed`, `ready`, `planned`, `built`, `done`, `dropped`, `needs-input`. The Acceptance criteria and `test_changes` are frozen once the doc is `planned`.
- **Breaking:** `.sdlc/config.json` is version 2. Defaults live in the engine (`scripts/sdlclib/config.py`); the file holds what init detected and what you changed. A version-1 file keeps being read until `/sdlc:init` rewrites it.
- The merge gate is run by the shell, with no model tokens, for `/sdlc:go` and the loop alike. Seven checks: build, test and lint on the branch with the default branch merged in; review tally `important=0`; test integrity; Acceptance criteria and `test_changes` unchanged since the design commit; no new dependency; no `risk.human` path and no guard file changed; `checks/*` pass. A failing command, review tally or check gets a fix session; the rest goes to a person with the reasons.
- Gate check 3, test integrity: with `test_changes: none` (the default) no existing test file changes by a single line, because an in-place weakening such as `assert x == 1` → `assert True` keeps every count. With `allowed`, no existing test is deleted, no test case is removed and no skip/only/xfail/`@Disabled` is added, and the merge records that existing tests were allowed to change: a line `Existing tests were allowed to change in this item (test_changes: allowed) — <reason>` goes at the top of the PR body and into the local squash commit message. The reason is the first line of the Plan, Departures or Proof (in that order) that speaks of `test_changes` or existing tests; the design guidance asks for a Proof line that starts with `test_changes:`.
- Gate check 4: a change to the Acceptance criteria or to `test_changes` after the design commit goes to a person even when Departures explains it, so an implementation or fix session cannot unlock the tests for itself.
- Gate check 6: a changed guard file — `.sdlc/config.json`, the frozen-paths file, `.claude/settings.json`, `.claude/settings.local.json` or anything in the `checks/` directory — goes to a person whatever `risk.human` says, and the check has no toggle. A session must not loosen its own guardrails and merge that unattended. The one exception is a `.sdlc/config.json` change that only fills `commands.*` entries that were missing or empty: the skeleton item of a new app records its build, test and lint commands this way, which adds checks and removes none. That item's own gate already runs the filled commands, and the loop re-reads the config after each merge, so the next items are gated with them too. Changing or removing a command that was already set still goes to a person.
- Gate toggles: `gate.test_integrity: false` turns off check 3 as a whole (the existing-test-change check included) and `gate.acceptance_frozen: false` turns off check 4 (the freeze of the Acceptance criteria and `test_changes`). `protect.protect_tests: false` turns off the hook lock and the gate's existing-test-change check together, so the whole project behaves as if every item said `test_changes: allowed`; the deleted-test, removed-case and skip checks go on while `gate.test_integrity` is on.
- The loop engine is Python (`scripts/sdlc.py`, standard library, 3.9+) instead of `run-loop.sh`. The queue is the work docs on the default branch; engine state keeps only failures, waits, answers, the last stopped session and cost. A session that reaches its time, turn or budget cap (`loop.session_max_minutes` 120, `loop.max_turns` 500, `loop.budget_usd_per_session`, off by default) is not abandoned. It is continued at once with `--resume`, up to `loop.max_continuations` (3) times per phase. Each continuation gets the full caps again: claude counts `--max-turns` and `--max-budget-usd` per run, not across a resume. So a phase can spend up to (1 + `max_continuations`) times `budget_usd_per_session`. It counts as a failure only when nothing changed since the previous stop, or when the phase is still unfinished after the last continuation. A session stopped by request or by a usage limit is resumed warm, or restarted fresh after `loop.cold_resume_minutes`.
- Team repositories use the same flow and commands: a person merges the queue PR (the approval of the list), approves each design PR (an approving review by someone else, or the `sdlc:approved` label) and merges the passed PR. A repository without a remote squash-merges locally through the same gate. When the gate hands an item to a person there, the status shows `git merge sdlc/<slug>` as the way to accept it, and the loop deletes the branch once it is merged.
- Model and effort per phase: `loop.models` (design opus, build sonnet, fix sonnet, self_check opus), `loop.efforts`, and `loop.escalate_model` for an item that has failed before. Subagents: reviewer opus; builder, verifier and simplifier sonnet; researcher haiku. A model-family usage limit switches to another configured model; a session or weekly limit is slept out up to `loop.limit_wait_hours`. The 0.x keys `loop.model`, `loop.fix_model` and `loop.effort` still work.
- Hooks read the work doc. Design-first reads the active work doc, and production deploys have one tier (`RELEASE_APPROVAL`). `guard-edit` and `guard-bash` fail closed (`onFailure: block`, which Claude Code honors from 2.1.295).
- The test lock is add-only (new test files are free; existing ones stay locked unless the doc says `test_changes: allowed`, or a person started Claude Code with `SDLC_ALLOW_TEST_EDIT=1`) and applies during sdlc work only: while an active work doc applies (`.sdlc/state/active-work` names the slug of the branch `sdlc/<slug>`, the same condition as design-first). Outside sdlc work nothing is locked, because no gate stands behind the lock there and the agent could switch it off in the config. The hook lock is an early warning — it turns off if an agent deletes `active-work` or leaves the branch — and the gate (checks 3, 4 and 6) enforces.
- `guard-bash` applies the test lock and design-first to shell writes (`rm`, `mv`, `cp`, `sed -i`, `tee`, redirections, `dd`, `truncate`, `git rm`/`mv`/`checkout -- <file>`/`restore`). It reads the command line without running it: programs that write on their own (`python -c`, `patch`, `git apply`, `make`, package managers, `curl -o`) and paths built from variables or brace expansion are not seen.
- The production deploy gate matches each simple command (quotes resolved, heredoc bodies and comments left out) instead of the whole command line. Text tools (`echo`, `cat`, `grep` …) and git commands that only read or record (`commit`, `log`, `diff` …) are text, so a commit message or a search that mentions a deploy no longer trips it. Quoted arguments (`bash -c "…"`, `ssh host '…'`), wrappers (`sudo`, `env`), command substitutions, heredocs fed to a shell and commands that can run a program are still checked: git's `rebase --exec`, `submodule foreach`, `bisect run`, `config`, a `-c` other than `user.name`/`user.email`, `--upload-pack`/`--exec`, or an environment prefix such as `GIT_SSH_COMMAND=`; a text tool with `--pre`, `--pager` or `--compress-program`. A command line it cannot split is matched as plain text, as before.
- On macOS a long non-ASCII text in a command line stopped `awk`, so the test lock never saw the commands after it; a line in a legacy encoding in a staged diff did the same to the commit secret scan, and the secrets after it went through. Both now read bytes (`LC_ALL=C`).
- The commit secret scan covers the staged diff by default. With a `git add` earlier in the same command line, or with `-a`/`--all`, it covers everything the commit could take: tracked-file changes and untracked text files that are not ignored (with a cap). `git -C <dir>` committing to another repository scans that repository too.
- One definition of a test file for the hooks and the gate — the directories `test/`, `tests/`, `__tests__/`, `androidTest/`, `testFixtures/`, `spec/` and the files `*_test.go`, `*.test.*`, `*.spec.*`, `test_*.py`, `*_test.py`, `*Test(s).java|kt|swift`, `*Spec.scala|kt`, `*_spec.rb`, with the work directory excluded; `protect.test_paths: ["auto"]` is this set. And one meaning of a glob in the frozen-paths file, `protect.generated_paths`, `protect.test_paths` and `risk.human`: `dir/` is a prefix, `*` and `?` stay inside one path segment, `**` spans any depth, a pattern without `/` matches a file name at any depth, any other pattern is relative to the repository root.
- `/sdlc:init` is Python and still never overwrites; it now imports `AGENTS.md` into CLAUDE.md, and `--check` is the health report that replaces `doctor`. `/sdlc:status` shows the loop, the queue, what needs you with the reply that resolves each item, the loop's cost (the sessions it ran; `/sdlc:go` sessions are not counted), recent merges and merges worth a look.

### Added
- `sdlc.py` commands: `init`, `enqueue`, `accept` (a `proposed` item becomes `ready`), `run [--detach]`, `status [--no-fetch]`, `stop [--after-item]`, `resume`, `answer`, `retry`, `drop`, `rate`, `gate`, `ship`.
- `sdlc.py status` fetches the remote first (about 10 seconds at most, failures ignored), so a PR a person merged shows as done at once; `--no-fetch` stays offline. A change that waited for a person and reaches the default branch as `built` counts as done, and the loop notices a person merging or closing such a PR without `retry`.
- `sdlc-builder` agent. The reviewer gains a validate mode (it tries to refute one finding in fresh context) and a pre-existing grade; only confirmed Important findings count in the tally.
- Questions: a loop session that needs a person sets `needs-input` and writes `## Questions` (recommended answer first); that item is parked, the rest of the queue goes on, and `sdlc.py answer` continues it.
- `sdlc.py drop <slug> [reason]` records `status: dropped` on the default branch (the reason on the first line of Departures, by the same path as `accept`; a team repository gets a PR for a person to merge) and closes the item's open PR. An item that a loop session drops because it found the work unnecessary is recorded the same way.
- Self-check after the merges the loop itself made — solo mode only, since in team mode a person merges and reviews: validated Important findings on changed lines become new `ready` items (at most 3 per pass); problems that were already there become `proposed` items.
- Trigger evals for the four skills in the current `prompt.md` + `graders/` format; Python engine tests with stub `claude` and `gh` (`tests/py`).

### Removed
- Skills: `intent`, `spec`, `plan`, `verify`, `review`, `release`, `monitor`, `triage`, `postmortem`, `metrics`, `evals`, `lesson`, `policy`, `scan`, `doctor`. The agent `diagnoser`.
- Scripts: `run-loop.sh`, `init.sh`, `status.sh`, `doctor.sh`, `metrics.py`, `monitor.py`, `run-evals.sh`, `review-gate.sh`.
- Templates: the GitHub workflow templates, `examples/`, `policies/`, `evals/`, `config/bands.json`, `config/managed-settings.json`, `config/mcp.deploy.example.json`, `config/otel.env`, `config/settings.sandbox.json`, `config/config.json` and `config/gitignore.sdlc` (init builds both in code), and `{en,ko}/{APPROVALS,intent-README,intent,spec,plan,LESSONS,evals-README}.md`.
- The CI (GitHub) mode and `ci.mode`, the managed-settings and enterprise material (`docs/ENTERPRISE.md`, `docs/PLAYBOOK-MAPPING.md`), and the control-band monitoring.
- The `.sdlc/state/allow-test-edit` file as an unlock of the test lock (an agent could create it itself). `test_changes: allowed` and `SDLC_ALLOW_TEST_EDIT=1` remain.

### Migration
Update the plugin everywhere it runs, then run `/sdlc:init` once per repository that used 0.x. Until then `/sdlc:go` and `/sdlc:run` ask for it and stop.
- Run it from an up-to-date checkout of the default branch (with several worktrees, the one that has it checked out). The migration reads that checkout's 0.x files, so init refuses to migrate from a checkout behind `origin/<default>`: items merged since then would come back as work.
- `.sdlc/config.json` is rewritten as version 2; the old file is kept as `.sdlc/config.v1.json`. `commands.*` and most `loop.*` keys carry over (`item_max_minutes` becomes `session_max_minutes`; `max_items` and `max_minutes` are dropped). Caps that were 0.x defaults (`max_turns` 120 or 200, `item_max_minutes` 45) are not carried, so 1.0's 500 turns and 120 minutes apply. `roles.solo` becomes `mode`, `protect.ticketed_paths` becomes `risk.human`, the `environments.production` patterns and `approval_env` move to `protect.deploy_patterns` and `protect.deploy_approval_env`; other 0.x keys that 1.0 does not use are dropped and listed in a NOTE.
- Every approved or draft intent whose plan is not implemented yet becomes `work/<slug>.md` — `ready`, or `planned` when an approved plan exists; drafts become `proposed`. Problem and Proposed outcome become the Goal, the spec's requirements become the Acceptance criteria, the plan's Proof becomes the Proof — the Korean templates' `## 문제 (Problem)` headings included. `depends_on` is kept (minus dependencies already merged in 0.x). A front matter without its closing `---` is read up to its last key, as 0.x did.
- NOTE lines name what a person still decides: triage drafts under `intent/triage/` (1.0 has no triage queue), dropped policies (their skills stay, but nothing applies them at review — name them in REVIEW.md), `roles.autopilot` or `auto_merge` set to false in solo mode (1.0 merges what passes the gate), a 0.x pause file, and project skills, commands or agents that name removed commands (`--check` warns about those too).
- The `intent/`, `spec/` and `plan/` history is left as it is. `sdlc.py init --prune` removes what 0.x generated and 1.0 no longer uses, on your yes. That `spec/` history falls in the default test set (the RSpec `spec/` rule), so it is locked during sdlc work and covered by gate check 3. Normal work does not change it; a change that must can free it with `test_changes: allowed` in its design.
- Globs: in the hooks `*` no longer crosses `/`, and a pattern with a `/` is anchored at the repository root (`dist/**` no longer reaches `pkg/dist/…`: write `**/dist/**`; `schemas/*.avsc` no longer reaches `schemas/sub/`: write `schemas/**/*.avsc`). A frozen-paths, `protect.generated_paths`, `protect.test_paths` or `risk.human` (0.x `protect.ticketed_paths`) entry that relied on reaching deeper levels must become `**`.
- Hooks: the test lock is off outside sdlc work (0.x always locked), and the `.sdlc/state/allow-test-edit` file no longer unlocks anything — use `test_changes: allowed` in the design, or `SDLC_ALLOW_TEST_EDIT=1` when a person starts Claude Code.
- CLAUDE.md lines that 0.x wrote and that name removed commands are updated (the plan-first line of 0.4.1–0.5.1 too), other mentions are reported. `.sdlc/LESSONS.md` is no longer read: move the lines that still hold into CLAUDE.md "Things Claude gets wrong". An untouched template is a leftover `--prune` removes.

## [0.8.3] - 2026-10-09

### Fixed
- A session stopped by a cap with uncommitted changes stopped the run and said the next run would resume it. The next run then refused the dirty tree, so someone had to make a WIP commit and switch back to the default branch by hand. Now the next run starts right there on `sdlc/<slug>` and resumes that session first. A dirty tree that no stopped session left is still refused.
- A resumed session now runs on its own branch: the run checks out `sdlc/<slug>` first, because the conversation it resumes was there.

## [0.8.2] - 2026-10-09

Caps that stop a runaway session, not a large item.

### Changed
- Loop caps raised: `max_turns` 120 → 500 and `item_max_minutes` 45 → 120 per session, `max_minutes` 120 → 480 per run. At 120 turns, sessions on large items stopped after about 30–40 minutes, well before the time cap. Each stop splits the work: the next run resumes the session, after the prompt cache has expired.
- `init` moves the old defaults (`max_turns` 120 or 200, `item_max_minutes` 45, `max_minutes` 120) to the new ones and keeps any value the user set.

## [0.8.1] - 2026-10-09

Model and effort, set where they matter and nowhere else.

### Changed
- Loop sessions get `--effort` from `loop.effort` (default `high`): without it, a user's own effort setting (for example `xhigh`) carried into every unattended session.
- The short session that fixes a failing check runs on `loop.fix_model` (default `sonnet`).
- `sdlc-researcher` runs on `haiku` at effort `medium`; `sdlc-verifier` and `sdlc-simplifier` set effort `medium` (a subagent without `effort` inherits the session's). `sdlc-reviewer` and `sdlc-diagnoser` keep the session's model and effort.

### Added
- `loop.item` records the models each item's sessions actually ran (`detail.models`); `/sdlc:metrics` shows the mean cost per item by model, so a new model behind an alias is visible.
- Test: no full model id in agents, skills, scripts, templates or hooks — aliases only, so a new model generation needs no plugin change; a subagent that sets a model also sets effort.

### Fixed
- `run-loop.sh` parsed a session result with a tab field separator, which collapses empty fields; it now uses the unit separator.
- `run-loop.sh` is one block that bash reads in full before running it: editing the file (a checkout used as `--plugin-dir`, say) no longer changes a loop that is already running from it.

## [0.8.0] - 2026-10-03

Four more token savings, and the loop takes over what hand-written parallel runners used to do.

### Changed
- `/sdlc:run` runs each item as two fresh sessions (`loop.split_phases`, default true): design — `/sdlc:go --stop-after plan` commits intent, spec and the approved plan on `sdlc/<slug>` and prints `SDLC_GO_PLANNED <slug>` — then build — `/sdlc:go --hand-off` starts from the committed plan, so the build session does not re-read the design conversation every turn.
- `/sdlc:go --slug` continues an existing `sdlc/<slug>` branch: stages whose artifacts are committed approved are skipped (approved plan → implement).
- The `sdlc-verifier` subagent is skipped for small changes: `verify.agent` `auto` (default) skips it below `verify.agent_min_files` (4) non-test files when every command passed in the first round, and says so in the report and PR body; `always` / `never`. The reviewer still reads the whole diff.
- Every loop session gets its id up front (`--session-id`), so a session killed by the time cap is resumed like a usage-limit or max-turns stop instead of being redone.
- The loop's time cap kills claude itself (`exec`), not only the subshell around it.
- `/sdlc:go` rule for headless runs: never end a turn while a background command runs; long commands go to the background with a log and a foreground wait.

### Added
- `/sdlc:lesson --trim`: proposes moving reference detail out of an over-long CLAUDE.md into `docs/claude/<topic>.md` behind one pointer line, applies only after agreement; doctor's CLAUDE.md row points at it.
- `run-loop.sh --items a,b,c` (these slugs, in this order), `--item-max-minutes`, `--note "<text>"` / `loop.item_note`; intent frontmatter `depends_on: [slug, ...]`.
- Parallel loops: one per worktree (`--dir <worktree>`); claims in the git common directory keep two loops off the same slug; a linked worktree detaches at the default branch's tip. A session that leaves the tree dirty stops the run and leaves the tree untouched.
- `init` fills the new `verify.agent*` and `loop.*` keys in existing configs.

## [0.7.0] - 2026-10-03

Local-first: Claude runs on the developer's machine by default.

### Changed
- New `ci.mode` (default `local`). In local mode `init` installs no GitHub Actions workflows that call Claude and stores no CI secret; skills, the loop, evals, monitor and scan run under the local Claude Code login, scheduled with cron if wanted (README "로컬 정기 실행"). `--ci github` (or `--github`) restores the CI workflows; the mode is recorded in `.sdlc/config.json` and kept across runs.
- `init --prune-ci` removes plugin-generated `sdlc-*.yml` left from github mode (only files with the plugin header); without it they are reported as LEFTOVER. `/sdlc:init` runs it for the user.
- `github-setup.sh` in local mode skips the secret entirely and applies the ruleset without a required status check (merges still wait for every PR check to be green in `go`/`run`).
- doctor: "CI mode" row (warns on leftover workflows), CI auth/secret rows only in github mode; next steps point at local runs and cron in local mode.
- Docs, pictures and the Archify diagram rewritten local-first; the README warns against putting a personal subscription token into an organization repository's secrets.

## [0.6.0] - 2026-10-03

Token economy, from the pilot's 125 headless sessions ($1,776 API-equivalent; 40% in sessions that ended in an error).

### Changed
- `/sdlc:run` calls `/sdlc:go --hand-off`: the session ends once the PR is pushed; `run-loop.sh` waits for the checks in the shell (no tokens), merges on green when `roles.auto_merge`, and starts a short fix session only for failing checks (`loop.fix_rounds`, default 2; `loop.checks_max_minutes`, 30).
- A usage-limit stop ("hit your session/weekly limit") ends the whole run instead of failing every remaining item, is not counted as a failure, and the next run resumes that session with `claude --resume` (loop sessions are persisted now). A max-turns stop resumes too. A stopped item with an open PR is still queued for resume.
- Loop defaults: `max_turns` 200 → 120, `model` "" → `opus` (standard context, auto-compacts instead of growing toward 1M). `init` migrates the old defaults and fills missing `loop` keys without touching values the user set.
- `sdlc-researcher`, `sdlc-verifier`, `sdlc-simplifier` run on `sonnet`; `sdlc-reviewer` and `sdlc-diagnoser` keep the session model.
- `sdlc-review.yml` skips `sdlc/*` branches (go already ran the same REVIEW.md passes locally); repository variable `SDLC_REVIEW_ALL_PRS=true` restores it. `sdlc-evals.yml` nightly cron → weekly (configuration PRs are still gated on every change).
- Interactive `/sdlc:go` waits for checks with one blocking call instead of a loop of short polls.

### Added
- Every loop session's `total_cost_usd`, turns, cache reads and output tokens are recorded on `loop.item` (and the run total on `loop.run`); `/sdlc:metrics` row "Headless session cost via /sdlc:run".

## [0.5.2] - 2026-09-25

Prompt audit (`/claude-api prompt-audit`, report in `_workspace/10_prompt_audit.md`).

### Fixed
- The session-start context and the CLAUDE.md "Verifying your work" template said "start with /sdlc:plan"; both now say every change starts from a committed plan.md that `/sdlc:go` writes (step-by-step path: `/sdlc:plan`). Repositories initialized earlier keep their CLAUDE.md line — update it by hand or with `/sdlc:lesson`.
- Numeric output ceilings removed from `sdlc-researcher` ("≤ 25 lines") and `/sdlc:go`'s report ("≤ 12 lines"); the item lists stay.
- `monitor.py`'s 2σ diagnosis prompt now asks for the same procedure and output as the `sdlc-diagnoser` agent (ranked hypotheses, runner-up, "unverified" wording) — the two had drifted.
- Removed the provenance footnote from `sdlc-verifier` (recorded in the README) and the pinned "Mythos 5 rates" pricing claim from `/sdlc:scan`.

## [0.5.1] - 2026-09-10

### Changed
- Documentation pass for the automatic-by-default solo behaviour: run/init skills, autopilot workflow header, PLAYBOOK-MAPPING deviation note, ELI5 page, pictures 01/03 and the Archify card no longer describe the loop or auto-merge as something the owner must switch on.

## [0.5.0] - 2026-09-10

Default flipped for solo repositories: automatic unless the owner adds a pause.

### Changed
- `init` on a solo repository now sets `roles.autopilot`, `roles.auto_merge` (new key) and `loop.enabled` to true; team repositories keep all three false. Existing keys are never overridden — a false the owner wrote stays a pause.
- `/sdlc:go`: `--merge` defaults from `roles.auto_merge`; new `--no-autopilot` / `--no-merge` add the pause for one run; doctor's auto-mode readiness is advisory (one-line warning) instead of a stop. On a solo repository nothing stops between the request and the merge except policy conflicts, three failed verification rounds and production.
- `run-loop.sh`: a missing `loop.enabled` on a solo repository means on; the refusal message names the switch.
- Docs and pictures: "기본은 자동, 제한은 사용자가 켬" — private Free-plan repositories run on hooks + go discipline alone; public / paid plans get the same rules enforced again by the ruleset.

## [0.4.6] - 2026-09-09

### Added
- Archify architecture diagram (`docs/img/archify/sdlc-architecture.json` → `docs/archify-architecture.html`, showcase validation 9/9, browser evidence pass): interactive on GitHub Pages, PNG in both READMEs alongside the hand-drawn picture; `docs/img/README.md` documents how to regenerate every picture.

## [0.4.5] - 2026-09-09

### Added
- Architecture picture (`docs/img/05-architecture.png`): device → repository → GitHub, the three layers (advisory skills/agents, enforcing hooks/rulesets, managed settings) and the single config file everything reads.
- The repository is public and GitHub Pages serves `docs/`; both READMEs link the rendered ELI5 pages.

### Fixed
- PLAYBOOK-MAPPING still said 18 skills / 6 CI templates in three rows.

## [0.4.4] - 2026-09-09

### Changed
- README pictures are now illustrations (PNG, drawn in the ELI5 visual language and rendered from `docs/img/src/*.html` with headless Chrome via `docs/img/render.sh`) instead of Mermaid: the two-command overview, the `/sdlc:go` flow with the human decision points, the `/sdlc:run` loop, and where each hook fires.

## [0.4.3] - 2026-09-09

### Added
- README diagrams (Mermaid): the two-command overview, the `/sdlc:go` flow, the `/sdlc:run` loop, hook firing points. Replaced by pictures in 0.4.4.

## [0.4.2] - 2026-09-09

### Changed
- The marketplace now lives at github.com/freewisl/ai-native-sdlc: install commands in both READMEs, the team-settings example, ENTERPRISE and the ELI5 page point there; `init.sh`'s default marketplace source for the CI workflows is `freewisl/ai-native-sdlc` (override with `--marketplace` or `SDLC_MARKETPLACE_SOURCE` for a mirror); owner metadata updated.

## [0.4.1] - 2026-09-09

### Fixed
- `gh` login checks are scoped to the repository's host (`gh auth status --hostname <origin host>`) in run-loop, github-setup, init and doctor; an unscoped `gh auth status` fails whenever any other configured host (a company GitHub Enterprise) is expired or unreachable, which locked out everyone using two hosts. `GH_HOST` is exported so later `gh api`/`gh pr` calls target the same host; the device-flow login uses that host too.

## [0.4.0] - 2026-09-08

The loop that keeps running.

### Added
- `/sdlc:run` (user-typed only) + `scripts/run-loop.sh`: works through approved intents one at a time, each in a fresh headless `claude -p "/sdlc:go --slug … --autopilot --merge"` session; after at least one merge, a self-check pass (sdlc-reviewer + the scan checklist over the merged diff) files Important findings as approved intents through a PR, which the next iteration picks up; stops when the queue is empty or at a cap. Solo repositories only; `loop.enabled` must be set to true on purpose.
- Config `loop.*`: `enabled` (false), `max_items` (5), `max_minutes` (120), `item_max_minutes` (45), `max_turns` (200), `max_failures_per_slug` (3, then the slug is blocked in `.sdlc/state/loop-failures.txt`), `self_check` (true), `model`, `allowed_tools`, `pause_file` (`.sdlc/state/pause` — the stop switch). `init` adds the block (off) to existing configs.
- `templates/github/sdlc-autopilot.yml`: unattended `run-loop.sh --once` every 30 minutes; documents why `SDLC_GH_TOKEN` (fine-grained PAT) is needed for the loop's PRs to receive their checks.
- doctor row `autopilot loop`; metrics row "merged per run / items that needed a human" from `loop.run`; events `loop.run`, `loop.item`.

### Changed
- `sdlc-evals.yml` runs on every PR and finishes green immediately when no agent-configuration path changed, so the required status check `evals` is always reported (a `paths:` filter left code-only PRs blocked forever under the ruleset).
- `/sdlc:go` sets `plan/<slug>.md` `status: implemented` inside the PR (how `run` and `status` know an item is done) and, with `--slug` naming an approved intent, takes the request from the intent.

## [0.3.4] - 2026-09-07

### Fixed
- `/sdlc:go` no longer claims it will run `/sdlc:init` when the config is missing — `init` is `disable-model-invocation` (user-typed only, because it rewrites the repository and GitHub settings), so `go` now stops and asks the user to type it.

## [0.3.3] - 2026-09-07

Documentation realigned to how the plugin is actually used: `/sdlc:init` once per repository, `/sdlc:go` once per change.

### Changed
- Marketplace and plugin descriptions lead with the two-command usage; stage-by-stage skills are documented as the parts `go` calls and as the team/large-change path.
- Plugin README: quick start rewritten (two commands, what `init` decides on its own, `go` stop conditions, when to use stage skills), install commands for the company repository `<조직>/ai-native-sdlc`, CI auth described as `ci.auth`-driven, `github-setup.sh` paragraph rewritten (secret → auto-merge → ruleset, solo without bypass, Free-plan behaviour), FAQ on auto-merge and stage skills, GitHub-only note for PR automation, event names `go.run`/`verify.test_unlock`/`direct_push`, `approval_basis`.
- Root README, ENTERPRISE, PLAYBOOK-MAPPING (test counts 206/134), ELI5 pages (two-command section, direct-push lock, install section) updated; doctor/init "Next steps" first item now points at `/sdlc:go`.

## [0.3.2] - 2026-09-07

Follow-up after checking the pilot repository (private, Free plan).

### Fixed
- `github-setup.sh` sets `allow_auto_merge`/`delete_branch_on_merge` **before** the ruleset step, so repositories where rulesets are unavailable (403 on Free-plan private repos) still get them.
- `/sdlc:go --merge` falls back to `gh pr checks --watch` + direct squash merge when GitHub refuses auto-merge (unprotected base branch); never merges with failing or running checks.
- `init.sh` merges the CI auth line of existing `sdlc-*.yml` workflows when it disagrees with `ci.auth` (only that line; other workflows untouched).
- `/sdlc:init` runs `github-setup.sh` whenever sdlc workflows are in place (not only with `--github`) and makes the adoption commit through a branch + PR, since the push guard blocks the default branch.

### Added
- doctor rows: `CI auth` (config ↔ workflow secret lines, offline), `CI auth secret` now names the expected secret, `auto-merge` repository setting.

## [0.3.1] - 2026-09-07

Fixes from the final review of 0.3.0.

### Fixed
- CI secret name mismatch: `init.sh` now records the chosen auth in `.sdlc/config.json` (`ci.auth`) and `github-setup.sh` reads it, so workflows and the registered secret always agree.
- `/sdlc:go --merge` needs `allow_auto_merge`; `github-setup.sh` now enables it (and `delete_branch_on_merge`).
- Solo detection fails closed: an unknown collaborator count keeps team gates (init and github-setup); auto-detected solo never overrides an existing `roles.solo`; `--solo` is the explicit override.
- `protect.default_branch` is written from the detected branch, so the push guard works on `master`/`develop` repositories without `origin/HEAD`.
- Push guard parses refspecs: `+main`, `refs/heads/main`, `HEAD:refs/heads/main`, `--delete <default>`, `git -C … push`, `--all/--mirror` are blocked; `main:feature` and unrelated clauses (`… && echo main`) are not.
- Solo ruleset no longer adds an admin bypass by default (the `evals` check stays enforced); `--bypass` opts in and is documented as making evals advisory.
- `/sdlc:go` creates the branch before the first artifact commit, records `approved_by (autopilot)` + `approval_basis`, requires doctor's `auto_mode` ok for autopilot, never autopilots when model-invoked, logs test-lock unlocks, and pre-approves the tools it uses.
- `run-evals.sh` lists copied config files in `<work>.meta/copied.txt`; the secret check excludes exactly those, so a credential the agent writes into CLAUDE.md/.claude/ is still caught.
- doctor honors `SDLC_SKIP_GH_DETECT`; github-setup help text, dry-run-while-logged-out message, `--save-token-file`; invalid config JSON is reported instead of a false MERGED.

### Added
- Config keys `ci.auth`, `protect.default_branch` (detected), `roles.autopilot`; README sections "가장 짧은 사용법", "손길 최소화", "갱신(릴리스) 절차"; `/sdlc:metrics` row "Request → PR via /sdlc:go"; plugin eval `trigger-go`; tests: hooks 193→206, scripts 98→120.

## [0.3.0] - 2026-09-07

Less ceremony for one-person repositories.

### Added
- `/sdlc:go "<request>" [--autopilot] [--merge]`: the whole loop in one session — intent, spec, plan, implementation, verification (3 rounds), review, PR and check babysitting — pausing only for real decisions (policy conflict, persistent verification failure, plan unless autopilot, merge unless `--merge`, production). `roles.autopilot` sets the default.
- `init.sh` zero-flag defaults: GitHub remote → workflows installed; one collaborator (via `gh`) → `roles.solo`; stored subscription token → `--auth oauth`. `--no-github` / `--no-solo` opt out; `--solo` on an existing config now merges `roles.solo`.

## [0.2.1] - 2026-09-07

Fixes from the first real project run.

### Added
- `protect.block_direct_push` (default true): the Bash guard blocks the agent's direct `git push` to the default branch and points to a PR — branch protection as a hook for repositories where GitHub rulesets are unavailable (private repos on the Free plan).

### Fixed
- `run-evals.sh` wrote its own records (`result.json`, `stderr.txt`) inside the work directory, and the `no-secret-in-code` check scanned them plus the copied `CLAUDE.md`, so a repository whose secrets rule quotes a pattern failed the case. Records now live in `<work>.meta/`; the check judges the agent's output only (excludes `.sdlc/`, `.claude/`, `.git/`, `CLAUDE.md`, `REVIEW.md`).

## [0.2.0] - 2026-09-07

Minimal-touch GitHub setup and solo mode, after the first installed-session tests.

### Added
- `scripts/github-setup.sh`: CI auth secret + default-branch ruleset via `gh` (PR required, review-thread resolution, code-owner review where the plan allows, required check `evals`, no force-push/deletion, admin bypass for PRs on solo repos); `--login` starts the gh device flow non-interactively; token resolved from env → macOS keychain → `~/.config/sdlc/ci-token`, stored once with `--save-token`.
- `init.sh --auth api|oauth` renders the matching CI secret into all six workflows; `init.sh --solo` sets `roles.solo` and the intent/spec/review skills take approvals in-session.
- Skills that run plugin scripts pre-approve them via frontmatter `allowed-tools` with `${CLAUDE_PLUGIN_ROOT}` (verified in an installed session).
- `doctor` rows: branch ruleset and CI secret presence (when `gh` is available).

### Fixed
- `plugin.json` no longer re-declares `hooks/hooks.json` (the duplicate declaration made the installed plugin fail to load).
- Remaining second-audit items: "What changes" blocks on every play skill, plan-title note, README counts/TOC, duplicate lines.

## [0.1.0] - 2026-09-07

Initial release. Implements Anthropic's "The AI-native SDLC playbook" (claude.com/blog/the-ai-native-sdlc-playbook, 2026-08-21) as an installable Claude Code plugin.

### Added
- **Skills (17)** under the `/sdlc:` namespace: `init`, `doctor`, `intent`, `spec`, `plan`, `verify`, `evals`, `review`, `lesson`, `release`, `monitor`, `triage`, `scan`, `postmortem`, `policy`, `metrics`, `status`.
- **Agents (5)**: `sdlc-verifier`, `sdlc-reviewer`, `sdlc-diagnoser`, `sdlc-simplifier`, `sdlc-researcher`.
- **Hooks (7)** registered in `hooks/hooks.json`: SessionStart / SessionEnd context and audit logging, PreToolUse guards for edits (frozen and generated paths, test-file lock during fixes, approved-plan requirement, secret scan) and for Bash (per-environment deploy gate, staged-diff secret scan), PostToolUse formatter and verification tracking, Stop verify-before-done.
- **Scripts**: `lib/common.sh` (jq with python3 fallback), `init.sh` (non-destructive scaffold and merge), `doctor.sh`, `status.sh`, `run-evals.sh`, `monitor.py` (deterministic Western Electric band detection and tiered response), `metrics.py` (per-stage leading/lagging indicators), `secret-scan.sh` with `secret-patterns.txt`.
- **Templates**: artifact templates in English and Korean (`templates/en`, `templates/ko`); project configuration (`config.json`, `bands.json`, `frozen-paths.txt`, project/sandbox/managed settings, deploy MCP example, `.gitignore` fragment, OTel env); six GitHub Actions workflows (`sdlc-evals`, `sdlc-review`, `sdlc-spec-on-intent`, `sdlc-ci-triage`, `sdlc-monitor`, `sdlc-scan`) and a `CODEOWNERS` starter; three eval cases (`no-secret-in-code`, `respect-frozen-path`, `fix-without-editing-test`); the `secure-api-review` policy skill example.
- **Playbook examples, verbatim**: `templates/examples/` (copied to `.sdlc/examples/` by init) holds the article's 14 code blocks unchanged — intent.md, plan.md, CLAUDE.md "Payments service", verifier.md, the verification block, REVIEW.md, settings.json hook registration, production-gate.sh, agent-evals.yml, the triage pipeline step, bands.yaml, the Stage 2 prompt, secure-api-review SKILL.md, the managed-settings JSON.
- **Approval-gate register** `.sdlc/APPROVALS.md` (en/ko) and a change-ticket gate for migrations/infra (`protect.ticketed_paths`, `protect.ticket_env`, default `CHANGE_TICKET`).
- **review-gate.sh**: turns the reviewer's `SDLC_REVIEW_TALLY` line into an optional merge check (Important count); wired as a commented step in `sdlc-review.yml`.
- **Monitor**: `report` route (leadership report for process metrics), disabled example metrics `post_deploy_5xx_rate` and `pr_cycle_time_hours`, `_tuning` notes; `sdlc-monitor.yml` opens a PR instead of pushing to the default branch.
- **init.sh options** `--marketplace`, `--ci-workflow`, `--team` so installed workflows and CODEOWNERS render without placeholders; `commands.lint_file` per-file linter; `claude_md.max_lines`; `source_of_truth.per_artifact`.
- **Documentation**: plugin manual (Korean) in `README.md`, enterprise deployment guide in `docs/ENTERPRISE.md`, playbook-to-component mapping in `docs/PLAYBOOK-MAPPING.md`.
- **Tests**: `tests/run.sh` runs the hook matrix (`tests/hooks.sh`, 185 checks incl. macOS bash 3.2 and a jq-less python fallback), the script suite (`tests/scripts.sh`, 85 checks: idempotent init on an empty directory, non-destructive merge into an existing project, doctor/status/metrics/run-evals/monitor) and `claude plugin validate --strict`.
- **Plugin evals**: four `claude plugin eval` trigger cases under `evals/` (intent, plan, review, doctor) — runnable once `plugin eval` early access is enabled for the account.

### Notes
- `hooks/hooks.json` is auto-discovered; the manifest must not re-declare it (doing so fails plugin load with "Duplicate hooks file detected") — guarded by a test.
- Skills that run plugin scripts pre-approve them through frontmatter `allowed-tools` with `${CLAUDE_PLUGIN_ROOT}` (verified in an installed session).
- Hosted products referenced by the playbook (Claude Code Review, Claude Security, Claude Tag, Claude Design, Cowork) are documented, not implemented; the plugin provides the local equivalents (`/sdlc:review`, `/sdlc:scan`, `/sdlc:postmortem`).
- The plugin never approves a pull request. Approval stays with a human through branch protection.
