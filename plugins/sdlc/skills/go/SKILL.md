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
- **Interactive**: anything else; the arguments, or the user's own words, are the request. Read `${CLAUDE_PLUGIN_ROOT}/skills/go/references/interactive.md` and follow it.

Read the reference a step names when you reach it, not before: `${CLAUDE_PLUGIN_ROOT}/skills/go/references/` holds `work-doc.md` (the format), `design.md`, `build.md`, `review.md`, `fix.md`, `self-check.md`, `questions.md` and `interactive.md`.
Agents (Task `subagent_type`): `sdlc:sdlc-builder`, `sdlc:sdlc-reviewer`, `sdlc:sdlc-verifier`, `sdlc:sdlc-simplifier`, `sdlc:sdlc-researcher`.

## Rules that always hold
- The record of a change is `work/<slug>.md` (dir: `paths.work`). Its branch is `sdlc/<slug>`; commits read `sdlc(<slug>): design|build|review|fix|question …`.
- Acceptance criteria are frozen once the doc is `planned`; the gate sends a changed one to a person. If one is wrong, ask (`needs-input`, or the user) instead of editing it.
- Existing test files are locked unless the doc says `test_changes: allowed`; new test files are always fine. The lock applies only while you work on an sdlc item, and with `test_changes: none` the gate sends any modification, deletion or rename of an existing test to a person. Never weaken, skip or delete a test — fix the code.
- A new dependency, or a change under a `risk.human` glob, sends the PR to a person. When the Plan can justify it, that is not a question: say why in the Plan (in a build, in Departures) and go on. Questions are for frozen paths, guard files other than filling an empty `commands.<name>`, and decisions only the user can make (references/questions.md).
- Never push, open or merge a PR yourself (`sdlc.py ship` and the engine do that), never commit to `<default>`, never `--no-verify`.
- A hook that blocks you (frozen path, test lock, secret, protected branch) is a decision. Report it; do not route around it or set an override variable.
- Change tracked files with Edit or Write: the guards and the formatter run on those, and Edit fails loudly when its text is not there. Bash writes (`python3 -c`, `sed -i`, heredocs) are for scratch files only.
- Commands run in the user's shell — on macOS zsh with BSD tools. Quote globs (`--include='*.sh'`), use `set -o pipefail` instead of `${PIPESTATUS[0]}`, prefer portable flags (no `cat -A`, `grep -P`, bare `sed -i`).
- Long commands: run in the foreground with the Bash tool's longest timeout (`timeout: 600000`) when it fits. Otherwise start it in the background with output under `.sdlc/state/` — `(cmd; echo $? > .sdlc/state/job.rc) > .sdlc/state/job.log 2>&1 & echo $! > .sdlc/state/job.pid` — and wait in one Bash call (`timeout: 600000`), bounded: `pid=$(cat .sdlc/state/job.pid); n=0; while kill -0 "$pid" 2>/dev/null && [ $n -lt 100 ]; do sleep 5; n=$((n+1)); done`. Still running → repeat the call; then read the log and `job.rc`. Never compare `wc -l` output as a string, and never end the session while a background process runs.
- Subagents (Task): launch each in the foreground (`run_in_background: false`) — its report is the input of your next step. Independent ones go out in one message and run in parallel. If one still comes back "launched in the background", wait for its completion notification: do not guess its result, do not poll, and print no marker before it is in.
- The review tally is exactly `SDLC_REVIEW_TALLY important=<n> nits=<n> validated=<yes|no>` (references/review.md).

## Loop sessions
There is no user: never ask (that is `needs-input`, references/questions.md), never push, open or merge a PR, never switch branches. An item session stays on `sdlc/<slug>` (the engine wrote `.sdlc/state/active-work`); a self-check stays on `<default>`. You are a top-level session, so you can use Task: a build session builds itself (it already runs on the build model — no `sdlc-builder`) but must launch the reviewer through Task (`subagent_type: sdlc:sdlc-reviewer`) and validate each Important finding the same way, one fresh context per finding. Reading the diff yourself is never a review; the engine sends the item back to review when the session's log has no reviewer call. The engine checks what you leave behind. Text after the flags (the gate's brief, a note about a stopped session, the project's item note) is context for you. Commit as you go — after each green plan step — because a session can be stopped at any time and the next one continues from the commits. Finish with a clean tree (a self-check's new docs stay untracked) and the marker line.

| Arguments | You | The engine then checks | Marker |
|---|---|---|---|
| `--item <slug> --design` | references/design.md: fill Plan and Proof, decide `test_changes` and `difficulty`, set `planned`, commit — or `needs-input` with `## Questions`, or `dropped` | doc status is planned, built, needs-input or dropped | `SDLC_PLANNED <slug>` |
| `--item <slug> --build` | references/build.md: implement the Plan yourself, run the gate's commands, review through the reviewer agent (references/review.md), record the tally, set `built`, commit — or needs-input / dropped | doc status is built, needs-input or dropped, and the log holds the reviewer call | `SDLC_BUILT <slug>` |
| `--item <slug> --fix` | references/fix.md; the prompt carries the brief — from the gate, or from the PR's checks on GitHub | commits what you left uncommitted, reruns the gate | `SDLC_FIXED`, or `SDLC_FIX_BLOCKED <why>` (a person takes it) |
| `--continue` (added to any of these) | a previous session stopped mid-phase: read `git status`, `git log <base>..HEAD` and the doc, pick up, do not redo committed work | as above | as above |
| `--self-check <from>..<to>` (on `<default>`) | references/self-check.md: review the merged diff and write new work docs; commit nothing | keeps only new `work/*.md`; every other change, an edit to an existing doc or a new directory included, is reverted | `SDLC_SELFCHECK items=<n>` |

When a `--design` or `--build` session cannot go on without a person, or the item turns out to be unnecessary, it sets `needs-input` or `dropped` instead of guessing — references/questions.md. A `dropped` doc is the engine's cue: it records the drop on `<default>` the way `sdlc.py drop` does (team mode: through a PR a person merges) and closes the item's PR — you do neither.
