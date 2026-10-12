---
name: go
description: One code change in this session, from request to merged pull request — writes the work doc (goal, acceptance criteria, plan, proof), builds and tests it, reviews it, runs the shell merge gate and ships it (team mode - a person approves the design and merges). Use for a change in a repository adopted with /sdlc:init, e.g. "/sdlc:go <request>", "add X", "fix Y", "implement Z", "X 해줘", "이거 고쳐줘", "기능 추가해줘", "버그 수정해줘". Also the entry point the sdlc loop runs for each work item. Not for questions or explanations, and not for several changes at once (use /sdlc:run).
argument-hint: '"<request>" | --item <slug> --design|--build|--fix [--continue] | --self-check <from>..<to>'
allowed-tools: Read, Write, Edit, Glob, Grep, Task, Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(git *), Bash(gh pr view *), Bash(gh pr list *), Bash(mkdir -p .sdlc/state*), Bash(printf *), Bash(date -u *), Bash(rm -f .sdlc/state/active-work)
---

# /sdlc:go — one change, start to finish

Arguments: $ARGUMENTS

In this skill `sdlc.py <args>` means the command `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" <args>` — the engine; it finds the repository from the current directory. Type it out in full at the start of the Bash command, not through a shell variable (zsh does not split one, and the permission rule matches the command's first words). `<default>` is `protect.default_branch` in `.sdlc/config.json` (`main` if unset); `<base>` is `origin/<default>` when that ref exists, else `<default>`.
Talk to the user in their language. Work-doc titles and prose follow `language` in `.sdlc/config.json` (en or ko); slugs, section headings, code and commit messages stay English.

## Which mode
- **Loop session**: the arguments have `--item <slug>` with `--design`, `--build` or `--fix` (maybe `--continue`), or `--self-check <from>..<to>`. The engine started you headless — nobody answers questions. See "Loop sessions".
- **Interactive**: anything else. The arguments, or the user's own words, are the request. See "Interactive".

Read the reference a step names when you reach it, not before: `${CLAUDE_PLUGIN_ROOT}/skills/go/references/` holds `work-doc.md` (the format), `design.md`, `build.md`, `review.md`, `fix.md`, `self-check.md` and `questions.md`.
Agents (Task `subagent_type`): `sdlc:sdlc-builder`, `sdlc:sdlc-reviewer`, `sdlc:sdlc-verifier`, `sdlc:sdlc-simplifier`, `sdlc:sdlc-researcher`.

## Rules that always hold
- The record of a change is `work/<slug>.md` (dir: `paths.work`). Its branch is `sdlc/<slug>`; commits read `sdlc(<slug>): design|build|review|fix …`.
- Acceptance criteria are frozen once the doc is `planned`; the gate sends a changed one to a person. If one is wrong, ask (`needs-input`, or the user) instead of editing it.
- Existing test files are locked unless the doc says `test_changes: allowed`; new test files are always fine. Never weaken, skip or delete a test — fix the code.
- A new dependency sends the PR to a person. Avoid it unless the plan says why.
- Never push, open or merge a PR yourself (`sdlc.py ship` and the engine do that), never commit to `<default>`, never `--no-verify`.
- A hook that blocks you (frozen path, test lock, secret, protected branch) is a decision. Report it; do not route around it or set an override variable.
- Loop sessions run every command in the foreground: a background process is lost when the session ends, and so is its result.
- Subagents (Task): launch each in the foreground (`run_in_background: false`) — its report is the input of your next step. Independent ones go out in one message and run in parallel. If one still comes back "launched in the background", wait for its completion notification: do not guess its result, do not poll, and print no marker before it is in.
- The review tally is exactly `SDLC_REVIEW_TALLY important=<n> nits=<n> validated=yes` (references/review.md).

## Interactive
0. **Preflight.** `sdlc.py status --json`. Exit 2 ("no .sdlc/config.json") or `legacy_config: true` → say in one line that `/sdlc:init` has to run first (adopting a repository is the user's decision — do not run it for them) and stop. `loop.running: true` → a loop owns this checkout; tell the user to `sdlc.py stop` it or work in another worktree, and stop. `git status --porcelain` not empty → ask the user to commit or stash first, and stop. Read `.sdlc/config.json`: `language`, `mode`, `paths.work`, `commands`, `verify`.
1. **Work doc.** From the request and what the repository itself answers, write `work/<slug>.md` from `${CLAUDE_PLUGIN_ROOT}/templates/<language>/work.md` (format: references/work-doc.md): a kebab-case ascii slug of at most five words that is not yet a doc, a branch or a remote branch; title; Goal; Acceptance criteria `R1. WHEN … THE SYSTEM SHALL …`; `status: ready`; `source: user`; `created` from `date -u +%Y-%m-%dT%H:%M:%SZ`, never typed from memory. Ask at most two questions, and only when scope or success is genuinely ambiguous — numbered, each with a recommended answer. Several independent changes in one request → say so and offer `/sdlc:run`.
2. **Branch.** With a remote, `git fetch -q origin`. `git switch -c sdlc/<slug> <base>` (the untracked doc comes along), then `mkdir -p .sdlc/state && printf '%s\n' <slug> > .sdlc/state/active-work`. The hooks read that file (test lock, design-first) and ignore it when the branch does not match it.
3. **Design** (references/design.md): fill Plan and Proof, decide `test_changes`, set `planned`, commit `sdlc(<slug>): design`. Team mode (`mode: team`): show the user the plan and the proof first and go on only with their OK — that is the design approval. Solo: go on.
4. **Build.** Task → `sdlc:sdlc-builder` (model sonnet) with the doc path — also for a change that looks small: the build's output stays out of this session. It implements, tests and commits `sdlc(<slug>): build`, and leaves the status `planned`. Read its report: open issues that need the user → ask; departures → check them against the Plan. Then add the checks of references/build.md (verifier, simplifier). If the agent cannot run, build in this session by references/build.md.
5. **Review** (references/review.md): reviewer pass, each Important finding validated in a fresh context, confirmed ones fixed (small → you; larger → back to the builder with the finding), tally recorded in `## Review`, `status: built`, commit `sdlc(<slug>): review`.
6. **Ship.** `sdlc.py ship <slug>` — the same shell gate and merge the loop uses (tree clean, doc `built`). It reruns build, test and lint, so give the call the Bash tool's maximum timeout, or `run_in_background` for a slower suite, and read its output when it finishes. Exit 0: merged, or in team mode handed to a person — print the PR. Exit 1: the output is the fix brief — fix it per references/fix.md, commit, ship again; at most three rounds, then stop and report with the literal output. Exit 2: a person has to decide — report the reasons and where the change lives (`gh pr list --head sdlc/<slug> --json url,state`, or the branch when there is no PR); do not work around them and do not merge it yourself. Untracked build output that blocks `ship`: delete it or add it to `.gitignore` (note it in Departures); never commit it.
7. **Wrap up** (also when you stop early). `rm -f .sdlc/state/active-work`. A shipped or handed-over change: return to `<default>` and update it (`git switch <default>`; with a remote `git pull --ff-only`; when `<default>` is checked out in another worktree, `git switch --detach <base>`). An unfinished one stays on its branch — say so. Report in the user's language: what changed (behavior, not a file list), the proof results (literal tail), the review tally and any `proposed` docs it wrote, the PR link / "merged locally" / "waits for you to merge", and what is left for the user.

**Resuming.** The user names a change already under way, or `git branch --list 'sdlc/*'` shows the obvious one: `git switch sdlc/<slug>`, write `active-work` again, read its doc and continue at the step its status shows — `ready` → 3, `planned` → 4, `built` → 6. Do not redo committed work.

## Loop sessions
There is no user: never ask (that is `needs-input`, references/questions.md), never push, open or merge a PR, never switch branches. An item session stays on `sdlc/<slug>` (the engine wrote `.sdlc/state/active-work`); a self-check stays on `<default>`. The engine checks what you leave behind. Text after the flags (the gate's brief, a note about a stopped session, the project's item note) is context for you. Commit as you go — after each green plan step — because a session can be stopped at any time and the next one continues from the commits. Finish with a clean tree (a self-check's new docs stay untracked) and the marker line.

| Arguments | You | The engine then checks | Marker |
|---|---|---|---|
| `--item <slug> --design` | references/design.md: fill Plan and Proof, decide `test_changes`, set `planned`, commit — or `needs-input` with `## Questions`, or `dropped` | doc status is planned, built, needs-input or dropped | `SDLC_PLANNED <slug>` |
| `--item <slug> --build` | references/build.md: implement the Plan yourself (this session already runs on the build model — do not delegate to `sdlc-builder`), test, run build/test/lint, review (references/review.md), record the tally, set `built`, commit — or needs-input / dropped | doc status is built, needs-input or dropped | `SDLC_BUILT <slug>` |
| `--item <slug> --fix` | references/fix.md; the prompt carries the gate's brief | a new commit on the branch | `SDLC_FIXED` |
| `--continue` (added to any of these) | a previous session stopped mid-phase: read `git status`, `git log <base>..HEAD` and the doc, pick up, do not redo committed work | as above | as above |
| `--self-check <from>..<to>` (on `<default>`) | references/self-check.md: review the merged diff and write new work docs; commit nothing, touch no other file | only new `work/*.md` survive; anything else is reverted | `SDLC_SELFCHECK items=<n>` |

When a `--design` or `--build` session cannot go on without a person, or the item turns out to be unnecessary, it sets `needs-input` or `dropped` instead of guessing — references/questions.md.
