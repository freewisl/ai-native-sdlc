# Changelog

All notable changes to the `sdlc` plugin are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-10-17

Release 1.2 comes from six audits of 1.1.2: the merge gate and git plumbing, the loop, init, the hooks, the skills against the code, and the transcripts of real loop runs on two private web apps. Every finding was reproduced with a script before it was fixed. The two that mattered most:
- A real loop build session never called the reviewer agent, wrote `validated=yes` itself, and its change merged unattended.
- A stale local branch could be force-pushed over commits a person had added to the item's pull request.

New key: `check.public_site` (default `true`). Every other change keeps the existing settings.

### Fixed — the loop
- **The review must really run.** The engine reads each build and fix session's stream for an `Agent`/`Task` call to `sdlc-reviewer`. A build without one is a fixable gate failure, `the review did not run in a fresh context (no sdlc-reviewer subagent call in the build session) — run references/review.md steps 2–4 and record the tally`, so a fix session runs the review. Items built before 1.2 are not held to it. The go skill now says plainly that a loop build session is top level and launches the reviewer itself.
- **Nothing on the item branch is overwritten.**
  - Before it works on an item, the loop fast-forwards to origin's `sdlc/<slug>`, so a person's fix on a needs-human PR or a reviewer's commit on a team design PR comes in first.
  - When both sides have commits the other lacks, the item goes to a person: `sdlc/<slug> on origin has commits this checkout does not have, and the other way round — merge them by hand, then retry`.
  - Every push is leased on the exact commit the loop last saw on origin (none, for a new branch). A lost lease goes to a person as `remote branch changed: …`.
  - A merge passes `--match-head-commit` with the pushed commit and is confirmed by PR number. Pull requests from forks are ignored, and an open PR wins over an old merged one with the same head name.
- **Separate clones no longer take the same item.** On GitHub the loop holds an item with the ref `refs/sdlc/claims/<slug>` on origin. The push that creates it is leased on "no such ref", so one clone wins. The claim names only the git user, the checkout's folder and the time. It is renewed at each session and every five minutes while the loop waits for checks, released when the item is left, and taken over after `2 × loop.session_max_minutes` without renewal. A held item is skipped (`SKIP    <slug> — held by <user> in <folder> since <time>`). When origin cannot be asked, the loop warns once and goes on without a claim. Worktrees of one clone already used claim files; these are now created with `O_EXCL`, so of eight processes racing for one item exactly one wins.
- **Old branches are brought up to date.**
  - Before a design or build session, a branch behind the default branch gets the default branch merged in. On a conflict the merge is undone, and the session's prompt names the conflicting files and says to merge first.
  - A `ready` item whose branch was cut before its work doc existed (0.x, or an earlier item of the same name) keeps that branch as `sdlc-old/<slug>` and starts again from the default branch. The design session is told where the earlier work is. The first push replaces origin's branch, leased on its old commit, and the log says so (`FRESH`, `REPLACE`).
- **Run-end reasons are true.** They are computed over the `--items` selection when there is one:
  - `queue empty`
  - `selected items finished`, with ` · N other item(s) not selected` when the queue has more
  - `nothing runnable — ` followed by `N wait for you (…)`, `N wait on other items (…)`, `N failed this run (…) — the next run retries them` and `N held by another loop (…)`, joined by `; `

  An `--items` name that is no work doc refuses the start: `no work doc named x`.
- **A network blip no longer stops the loop.** A failed fetch is `WARN    fetch failed: …; going on with what is here`, said once per run. The loop stops only when the default branch has really diverged from origin's.
- **The self-check cleans up after itself.** Only new `work/*.md` files survive. Every other change the session made is undone: edits to existing work docs or `.sdlc/config.json`, staged files, and new files and directories (names read NUL-separated). An edited work doc used to stay behind and stop the next pass with "the working tree is not clean". A self-check that did not finish (usage limit, cap, error) is not counted, keeps its range for the next check, and sends a usage limit through the same handling as an item's session. `loop.self_check_max_items: 0` now means no `ready` findings; it used to mean three.
- **Worktrees.** An item whose branch is checked out in another worktree is skipped for this pass (`SKIP    <slug> — checked out in <path>`) instead of stopping the run.
- **Smaller loop fixes.**
  - A PR-checks fix round is counted when its fix session returns, so a stop or a service error does not use one.
  - A resumed fix session is told when its brief changed.
  - Committing the work in progress no longer looks like progress to the no-progress check.
  - No fix session starts after a stop was requested.
  - A `resetsAt` in milliseconds is read correctly.
  - An answer from `gh` in an unexpected shape fails that item with the traceback in the run log; it no longer ends the run.
  - `sdlc.py stop` survives a pid it may not signal.
  - A pid file now records the process start time, so a reused pid is not taken for the loop.
  - A timeout kills the whole process group, so no child keeps running.

### Fixed — the merge gate
- **The design cannot be rewritten away.** The engine records the commit where the design phase ended. Criteria and `test_changes` are held to the doc there, if that commit is still on the branch. Otherwise they are held to the first commit where the doc says `planned`, then to the doc as it was queued. A branch with none of these goes to a person. Before, a session that squashed the branch onto a commit already saying `built` freed both.
- **Fail closed.** A gate that cannot compute the diff, or a check that crashes on a manifest, hands the PR to a person (`the gate could not compute the diff: …`) instead of passing.
- **Odd paths.** Paths are read NUL-separated with literal pathspecs. A path git would quote (Korean, spaces, quotes, `[`, `*`) used to skip every file check.
- **The tree is always restored.** After the gate, however it ended (a command timeout included), the merge is aborted, tracked files go back to the branch head, and untracked files the gate made are removed.
- **More goes to a person:**
  - frozen paths (`.sdlc/frozen-paths.txt`) and `protect.generated_paths`, which the hooks guarded but the gate did not check;
  - test-runner configuration (`conftest.py`, `pytest.ini`, jest/vitest/playwright/cypress configs, `karma.conf.*`, `.mocharc*`, `phpunit.xml*`), even with `test_changes: allowed`;
  - changes to `package.json`'s `scripts.test` or to the pytest settings in `pyproject.toml`.
- **Dependencies** are read from PEP 621, Poetry and dependency groups, `requirements*.txt` (also `-e` and `name @ url`), `setup.py`, `Pipfile`, `package.json` (also `bundleDependencies`), `Cargo.toml`, `go.mod` (not `// indirect`), Gradle and version catalogs, `pubspec.yaml`, `pom.xml`, `Package.swift`, `Podfile` and `Gemfile`. A version bump of a URL dependency is not new. Python names are compared normalized.
- **Tests.**
  - Skip markers are recognized for Python, Rust, Go, Ruby, PHP, Java/Kotlin, C#, Swift and JS.
  - Rust `#[test]`, `#[tokio::test]` and Swift `func test…` count as test cases.
  - A test file renamed to another test path is compared by counts. A test renamed to a non-test path counts as deleted.
- **Work docs.** A byte-order mark before the front matter is accepted. `depends_on` written as a YAML block list is read. With two `## Review` sections, the last tally counts and the doc is reported.

### Fixed — init
- **0.x workflow lines.** A CLAUDE.md, AGENTS.md or project skill that still tells sessions to merge PRs themselves (`gh pr merge`), keep the queue in `intent/` or read `spec.md`/`plan.md` is named with file and line, as an init NOTE and a `--check` WARN. Init does not edit those lines. Meanwhile the hooks block `gh pr merge` during sdlc work.
- **New `--check` rows:**
  - `default branch`: FAIL when the branch exists neither here nor on origin, WARN when origin's default is another;
  - `dependencies`: a `depends_on` naming no doc, or a cycle;
  - `ignored files`: git ignores `.sdlc/config.json`, the work docs or REVIEW.md;
  - `pause file`;
  - `public site`.
- **Public site.** A static host that serves the repository root would publish `CLAUDE.md`, `REVIEW.md`, `work/` and `.sdlc/`. The check reads a root `index.html` and the host's configuration: Vercel, Netlify, Cloudflare Pages, Firebase, and Jekyll on GitHub Pages. It names what would be served and stays quiet once the host's exclusion list covers it. `check.public_site: false` turns it off.
- **`--prune`** also replaces the 0.x block of `.gitignore` (log and state lines are kept) and removes the unchanged 0.x `.github/CODEOWNERS` starter.
- **Migration.**
  - Dangling `depends_on` entries are dropped with a NOTE.
  - `loop.max_items`/`max_minutes` are kept when they differ from the 0.x defaults.
  - Migration refuses while the 0.x intent, spec or plan folders have uncommitted changes.
  - Files that already had uncommitted changes are named.
- **Detection.**
  - A command the user set to `""` stays empty on a re-run.
  - uv, poetry, `.venv` and `packageManager` are detected.
  - Korean section names (`## 명령어`, `## 자주 쓰는 명령`) count as the user's own section.
  - An app one folder down (`ios/`, `android/`) or a root Xcode project gets a `--commands` suggestion.
  - Compound commands (`cd x && y`) get one permission per part.
  - A `gh` that is logged in but does not answer is a WARN, not a FAIL.
  - Deploy patterns equal to the defaults are not written out.
- **Templates.** The CLAUDE.md sections and the work-dir README name the configured work dir. The tally rule reads `validated=<yes|no>`.

### Fixed — hooks
- **Deploy gate false positives.** Commands that only read no longer count as deploys: `gh pr create --body …`, `gh api`, `curl` reads, `ls`/`find`/`sed -n` on paths, `node -e`, `pytest -k`, prose in arguments, and `git commit` with `GIT_EDITOR=`. Piping into a shell (`… | bash`) is now checked. `vercel --prod`, alone or inside `bash -c`, is now blocked.
- **Speed.** A writing command over a glob of 800 directories under the test lock took up to 43.7 s and hit the hook timeout, which blocked it. It now takes 0.2–0.5 s: one `git ls-tree` per prefix.
- **Without jq** the python fallback read the payload from an environment variable and failed open on a large one (E2BIG). It now reads stdin, and unreadable JSON is blocked.
- **Secret scan.**
  - Patterns are case-sensitive, so base64 blobs are no longer secrets.
  - It now finds unquoted values, `sk-proj-`, `sk_live_`, Slack webhooks, `npm_`, `ghs_`, encrypted private keys and `Bearer` tokens: 15 of 15 audit samples, where 1.1.2 found 2.
  - A block names the file and line and the way out.
- **Push and merge.**
  - `git push origin HEAD` and `@` on the default branch are resolved.
  - During sdlc work, `gh pr merge` and a push to the default branch are blocked whatever `protect.block_direct_push` says.
- **Test lock.** It now sees `$(…)`, `bash <<EOF`, `bash -s`, `pushd`, and directory operands of `git checkout`/`restore`. The test-runner config files above are locked like tests.
- **post-bash.** Failed commands are recorded too (`PostToolUseFailure`). A command counts as the verify command only when a simple command starts with it.
- **Paths and messages.**
  - A file outside the project is no longer matched against the project's globs or run through its formatter.
  - Commit scope with `-a` is read correctly.
  - On macOS, the session-start context adds one line about zsh and BSD tools.
- **Globs.** A leading `/` or `./` roots a pattern. `**/dir/` and `src/*/x/` work. A string is accepted where a list is expected. Python and bash agree on all 19,722 pairs of the drift corpus.

### Changed — skills and agents
- `/sdlc:go`'s interactive steps moved to `references/interactive.md`, so loop sessions no longer load them (about 1.2k tokens each).
- Sessions no longer park an item for a new dependency or a `risk.human` path the Plan can justify. The gate hands that PR to a person, who reads the Plan.
- Build sessions run what the gate runs: `gate.commands` and `checks/`.
- There is one rule for long commands: the longest foreground timeout, or a bounded background wait. This came from a 9.5-minute hang on `wc -l` padding in a real run.
- Tracked files are changed with Edit/Write, and shell commands follow zsh/BSD habits.
- Fix sessions re-review non-test code they changed beyond the failure. A command that already fails or flakes on the default branch is fixed or reported with `SDLC_FIX_BLOCKED`, not worked around. A fix that resolves another open doc drops it.
- The self-check tells the reviewer that pre-existing means present at `<from>`, not on the default branch, which already holds the merged work.
- The reviewer may run the project's build, test and lint commands.
- `/sdlc:status` lists merges with a `manual` Proof row first and names the row.
- Trigger words are narrower: "루프 돌려" and "루프 멈춰" for the run skill, and "좋아"/"별로" for status only as a reply about a merge. Two negative evals were added ("테스트 돌려줘", and "좋아" approving a design), and the negative evals run in an adopted repository.

### Known limitations
- Item state (`.sdlc/state/items.json`) is per checkout: a loop in another worktree keeps its own failure counts and waits.
- A pid file written before 1.2 has no start time and is still checked by pid alone.

### Migration
Nothing needs migrating. Update the plugin everywhere it runs, then run `/sdlc:init --check` once per repository: it now reports 0.x workflow lines, a public site, a pause file and dangling dependencies.

## [1.1.2] - 2026-10-14

Found in the first run on a real repository: a private web app with GitHub Actions and a Vercel deployment, moved from 0.x with `/sdlc:init`, then two items through `/sdlc:run`. Both merged after their pull requests' checks passed: 11 minutes and $2.79 with the self-check.

### Fixed
- A `sdlc.py status` while the loop was starting stopped the loop with `cannot check out or fast-forward master`. Status fetched (`git fetch --prune`) at the moment the loop checked out and pulled the default branch, and git fails a command that finds another git process's lock file. Now:
  - `status` does not fetch while a loop runs in the checkout; the loop fetches for itself.
  - A git command the engine runs is tried again after 1, 2 and 4 seconds when it failed on a lock (`Unable to create '….lock': File exists`, or a ref another process moved meanwhile, `cannot lock ref …: is at … but expected …`; the same words in a commit hook's output do not count). This also covers two loops in two worktrees of one repository.
  - The reason git gave is kept in the message, for example `cannot check out or fast-forward master: fatal: Unable to create '…/.git/index.lock': File exists.` The same goes for `cannot return to sdlc/<slug> after the session`, `still on sdlc/<slug>: could not check out <default>` and, without a remote, `merge did not happen: cannot check out <default>`.
  - A fetch that runs out of time (status, init) gets SIGTERM first, so git removes its lock files, and SIGKILL only after 2 seconds.
- A work doc with the slug `readme` or `template` overwrote `work/README.md` or `work/TEMPLATE.md` on macOS and Windows, whose file systems ignore case. These slugs are refused now: `slug 'readme' names the same file as work/README.md on a case-insensitive file system (macOS, Windows) — pick another`.
  - The check applies to enqueue, the inbox, the self-check's findings and `init --check`.
  - A 0.x intent with such a slug is not migrated, and init says so in a note.
- `/sdlc:init` left a 0.x REVIEW.md as it was, and the 0.x template says two things that are wrong in 1.x:
  - its Compliance line sends the reviewer to `spec.md` and `plan.md`;
  - its Approval section says findings never block a PR, but the 1.x merge gate does not merge with an open Important finding.

  Where they are still exactly as 0.x wrote them (English or Korean), init now replaces:
  - the Compliance line with the 1.x one, which names the work doc;
  - the Approval section with the 1.x "New or pre-existing" and "Tally" sections, leaving out either one when the file has that heading already.

  The row reads `MERGED  REVIEW.md  (the 0.x compliance line now names the work doc; the 0.x approval section replaced by the review tally rule)`. Edited lines stay as they are, and `init --check` warns `names spec.md/plan.md from 0.x — the reviewer reads the work doc`.
- On GitHub, a run whose self-check queued only proposals ended with `stop=queue empty` and left the local default branch behind. The findings reach the default branch through a pull request the engine merges, and the loop counted what waits for a person before it pulled them. Now it pulls first and stops with `nothing runnable — 3 item(s) wait for you`.

### Added
- The plugin README warns that a static site deployed from the repository root also publishes the sdlc files. In the trial, the production site served `CLAUDE.md`, `REVIEW.md`, `work/*.md` and `.sdlc/config.json` to anyone.

### Migration
Nothing needs migrating. In a repository moved from 0.x with 1.0–1.1.1, run `/sdlc:init` once more to bring its REVIEW.md up to date.

## [1.1.1] - 2026-10-14

### Fixed
- The fix brief for failed PR checks now carries why the check failed. `gh run view --log-failed` prints the whole log of a failed job (gh 2.101.0 cannot match the lines to steps and marks every one `UNKNOWN STEP`), so the end of it was the job's cleanup: on a real failed run, 33 of the 42 lines a fix session got were post-job steps. The engine now cuts each job's log at `Post job cleanup.` (steps that run on failure come before it and stay), drops the job, step and time in front of every line, and marks where each job starts when a run has more than one.
- A run whose log GitHub does not give yet ("still in progress", possible for a few seconds after its last check finished) is asked again after 10 seconds, up to three times, instead of going into the brief without its log. A stop or pause of the loop during that wait stops the run before a fix session starts; the doc is `built` already, so the next run gates and ships it again.

## [1.1.0] - 2026-10-14

The loop now picks each session's effort itself, skips a self-check that would only repeat one item's own review, and on GitHub waits for the pull request's checks before it merges. It also keeps its promise about `SDLC_FIX_BLOCKED`, marks a continued window in the audit log, and no longer lets a refused start hide the last stop.
New keys, with their defaults: `loop.auto_effort` `true`, `loop.self_check_min_items` `2`, `loop.checks_wait_minutes` `30`, `loop.isolate_sessions` `false`.

### Added
- **Auto effort** (`loop.auto_effort`). The loop picks the effort of every design, build and fix session it starts, a first window and each continuation alike, by moving the phase's `loop.efforts` value along `low`, `medium`, `high`, `xhigh` and clamping the result to that range. A work doc with `difficulty: easy` is one step lower for design and build; `difficulty: hard` is one step higher for design, build and fix (a fix session works on a failure, so an easy item gets no discount there). An item that has failed before is one step higher for all three, and a fix session from the second round on for the same failure is one step higher again. The steps add up: a `high` build of a hard item that has failed before runs at `xhigh`. A phase whose effort is `""` (no `--effort` flag) or `max` is left alone, because a person chose it. The self-check runs at its base effort. `xhigh` is safe to pass: claude lowers it by itself on a model without it. `loop.auto_effort: false` gives back the fixed efforts of 1.0.
- Work doc front matter `difficulty: easy|normal|hard`, on its own line after `test_changes` (read in any case). `easy` is mechanical: the Plan names every edit and there is no logic to get wrong (config, a rename, wording, wiring, copying a pattern that exists). `hard` is concurrency, security or auth, a data migration or a stored format, money or precision, an algorithm, a contract between modules, or a requirement that needed judgment. `normal` is everything else. `/sdlc:run` guesses it when it splits a goal; the design session sets it from its Plan and revises the guess. The field is optional, not validated and not frozen: a missing field, or any other value, counts as `normal` and runs at the base effort.
- The effort is visible. The session line of the run log says why — `BUILD   a-first — session (model sonnet, effort medium — difficulty easy)` — and the reasons it can give are `difficulty easy|hard`, `failed before` and `fix round N`, in that order. The run header shows `efforts=design:high/build:high/fix:medium (auto)`. `loop-status.json` and `sdlc.py status` show the running session's model and effort (`build a-first for 3m (sonnet · medium)`), and a `loop.session` event carries `effort` and `effort_reason`.
- **PR checks** (`loop.checks_wait_minutes`). On GitHub the engine now waits, after the shell gate has passed and before it merges, for the checks on the pull request, up to the limit (status phase `checks`, log line `CHECKS  a-first — waiting for build, test`). All pass: it merges. One fails: once no check is still running (GitHub gives a run's log only when the run has finished, and the fix should see every failure at once) the doc goes back to `built` (`sdlc(<slug>): PR checks failed — back to built`), a fix session gets the failed checks' names, workflows, links and log tails (up to `loop.fix_rounds` such sessions per item), and the shell gate runs again before the next merge attempt. Still running at the limit, or not readable for the whole wait: the item goes to a person with that reason, and the loop notices when the PR is merged. A PR without any check counts as passing, after 120 seconds when the checkout has `.github/workflows/` files, at once otherwise. Only checks reported for the commit the engine pushed count, so the push after a fix is never judged by the previous commit's failure. A person who merges or closes the PR during the wait ends it. Each wait is a `loop.checks` event. `loop.checks_wait_minutes: 0` turns the wait off. Team mode is unchanged: the engine marks the PR ready and a person merges.
- A merge that GitHub refuses because the branch is behind the default branch brings the branch up to date once (the default branch merged in; a conflict hands the item to a person), publishes again, waits for the checks again and merges.
- **Session isolation** (`loop.isolate_sessions`, off by default). When `true`, loop sessions start with `--setting-sources project,local`, `--strict-mcp-config` and the sdlc plugin through `--plugin-dir` (not twice when the project's settings already enable it), so they skip the user's other plugins and MCP servers. No MCP server is loaded at all, the project's `.mcp.json` included: loop sessions only get the tools in `loop.allowed_tools` anyway, unless the project's settings allow an MCP tool — then leave it off. Measured on claude 2.1.296 (haiku, one turn): a loop session starts with about 25k tokens of prompt, and the user's other plugins and MCP servers are about 4.8k of it (about 19%). It stays off by default because user settings stop loading too: an `apiKeyHelper`, or a proxy or base URL in `env`, would be missing from every loop session. A claude without `--setting-sources` is left as it was.
- **Trimming a long CLAUDE.md** (`skills/init/references/claude-md-guide.md`). `/sdlc:init` offers it when `--check` reports the file over `claude_md.max_lines` and when an adoption ends with the file over the cap, and changes nothing without the user's yes. What only some changes need moves to `docs/claude/<topic>.md` behind one pointer line (never an `@` import, which would load it into every session again); what is stale, repeated in README.md or AGENTS.md, or enforced by a linter goes; no rule the user wrote is dropped without being listed first.

### Changed
- The self-check runs only after at least `loop.self_check_min_items` (2) items merged since the run began or since the last self-check, because one item's own review has already read its diff. A run that merged fewer logs `CHECK   skipped — 1 item merged since the last check; its own review covered it (loop.self_check_min_items 2)` once.
- `sdlc.py ship` can take up to `loop.checks_wait_minutes` longer on GitHub and exits 1 with the PR checks' brief when one fails (the doc is back to `built`: fix, commit, ship again). It exits 0 too when a person merged the PR during the wait, and 2 when they closed it. It waits with a plain sleep, so the loop's pause file does not stop it. `/sdlc:go` runs it in the background and waits for its completion notification instead of giving it the Bash tool's maximum timeout.
- The fix session's brief comes from the PR's checks as well as from the gate (`references/fix.md`): reproduce with the command the workflow runs, and never edit `.github/workflows/` to get green.
- `sdlc.py retry` also clears the item's PR-checks fix rounds (`ci_rounds` in the item state).
- `/sdlc:status` shows the running session's model and effort, the `checks` phase and a refused start, and lists the PR checks and `SDLC_FIX_BLOCKED` among the reasons an item waits for a person.

### Fixed
- `SDLC_FIX_BLOCKED <why>` was ignored. `references/fix.md` promised that the item then goes to a person with the note, but the engine ran the gate again. Now it clears the stop record, commits what is left and hands the item to a person with "the fix session could not fix it: <why>".
- `events.jsonl` did not mark a continued window; a window that hit a cap was logged as `max_turns` whether or not the engine went on. Each `loop.session` event now carries `continued`, and a new `loop.continue` event records why the engine continued after a cap or a usage limit and whether it resumed warm or started a fresh session.
- A refused start replaced "last stop". A start the engine refuses (a dirty working tree, a failed preflight) now writes the previous status back with `refused` and `refused_at` instead of a status of its own, so `status` shows `loop: not running · last stop: queue empty · start refused 5m ago: the working tree has …`.

### Migration
Nothing needs migrating. Every new key has a default, `loop.auto_effort: false` restores 1.0's fixed efforts, and `loop.checks_wait_minutes: 0` merges without waiting for the checks. Update the plugin everywhere it runs.

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
