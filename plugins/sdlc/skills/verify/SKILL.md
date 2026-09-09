---
name: verify
description: Runs the feedback loop that makes verification part of "done" (Stage 4 Test) — states the quantitative target, runs the project's build/test/lint commands and pastes their output, drives the failing-test-first protocol for bug fixes, the screenshot-vs-mock loop for UI work, and finishes with an independent fresh-context check by the sdlc-verifier agent. Use when the user says "verify the change", "run the checks", "make sure it works before we call it done", "reproduce the bug as a failing test first", "fix this bug without touching the tests", "compare against the mock", "검증해줘", "테스트 돌려서 확인", "버그 재현 테스트 먼저", "화면 비교해줘".
argument-hint: "[--bugfix] [--ui <mock-path>] [--no-agent]"
---

# /sdlc:verify — give Claude a feedback loop, then protect it

## What changes
- **Traditional:** The signal that code works arrives late. CI minutes later, a tester days later, production weeks later. With an agent producing the code, a late signal means a person has to check all of its output, and that person becomes the bottleneck.
- **AI-native:** The session is given a way to check its own work before a person sees it. Run the tests, run the build, take the screenshot. Claude iterates until the check passes, so what reaches the engineer has already passed it.


## When to use
Before reporting any task complete; at the end of `/sdlc:plan`; for every bug fix (`--bugfix`); for UI
changes with a mock (`--ui`). Prerequisites: none — a test suite and a build that run with one command each.

Prerequisites: None.
Infrastructure: A test suite and a build that run locally with one command each. For UI work, a way for Claude to see the result — a browser tool or a screenshot utility wired in via MCP.

## Inputs
- `$ARGUMENTS`: `--bugfix`, `--ui <mock-path>`, `--no-agent` (skip the final subagent pass).
- Commands: `.sdlc/config.json` → `commands.build/test/lint/run`; fall back to the "Verifying your work"
  and "Commands" sections of `CLAUDE.md`. If none exist, ask for the one command that exits non-zero on
  failure and offer to write it into both places.
- The active plan (`.sdlc/state/active-plan` → `plan/<slug>.md`, section **Proof**) for the target.

## Steps
1. **State the target first**, quantified, so the result can be judged without asking: from the plan's
   Proof section when present ("test_status.py covers the four claim states"), otherwise the defaults:
   build finishes without errors, all tests green, lint zero warnings, and any endpoint/behavior the task
   named ("returns 200 with the new field").
2. **Run the loop.** Execute build, then test, then lint (skip a stage whose command is empty and say so).
   For each: the exact command, its exit code, and the last ~30 lines of output pasted verbatim. On failure,
   read the error, fix the **code**, and rerun — never skip, delete or weaken a failing test, never
   silence the linter. Stop and report after three unsuccessful rounds on the same failure rather than
   thrashing.
3. **`--bugfix` — the failing test comes first** (the test that existed before the fix and could not be
   rewritten is the proof the bug is gone):
   1. `mkdir -p .sdlc/state && touch .sdlc/state/allow-test-edit` — opens the test-file guard for this step only.
   2. Write the test that reproduces the bug; run it; confirm it fails **for the expected reason** (paste
      the assertion message — a compile error or a wrong import is not a reproduction).
   3. `git add <test files> && git commit -m "test(<slug>): failing test reproducing <bug>"`.
   4. `rm -f .sdlc/state/allow-test-edit` — from here the hook blocks test edits.
   5. Fix the code only; rerun the test suite; commit the fix. If the fix genuinely requires a test
      change, stop and explain — the human decides (`test_changes: allowed` in the plan, or the flag file).
4. **`--ui <mock>` — close the loop with a visual check.** Read the mock. Start the app
   (`commands.run`, in the background) and open the page with the available browser or screenshot tool
   (Chrome MCP, Playwright MCP, or a project screenshot script). Compare screenshot to mock: layout,
   spacing, copy, states. Adjust, re-screenshot, compare again — two or three rounds is normal; stop when
   the differences are intentional or below the mock's precision. Save screenshots under
   `.sdlc/state/ui/<slug>-round<n>.png` and list what changed each round.
5. **Independent check.** Unless `--no-agent`: launch the `sdlc-verifier` agent (Agent tool; the plugin's
   `sdlc-verifier`) with: the commands, the plan path, the changed files (`git diff --name-only HEAD~1`
   or the branch base) and the target from step 1. Include its report verbatim. Its verdict does not
   override step 2's evidence, but a mismatch it finds must be resolved or explicitly accepted by the user.
6. **Report done** only when the loop is green and the verifier found no unaccepted mismatch — with the
   pasted outputs, not a summary of them.

## Output
Target · command/exit/output for build, test, lint · bugfix protocol trail (test commit hash) or UI
rounds · the `sdlc-verifier` report · verdict: PASS / FAIL (what is still red).

## Governance
- The evidence is the literal toolchain output pasted into the session (and the PR check run) — not
  Claude's assurance. The code owner reviewing the PR approves; this skill approves nothing.
- The loop is protected: the agent fixing code must not weaken the check on that code. The test-path
  guard blocks test edits unless the flag file, the plan's `test_changes: allowed`, or
  `SDLC_ALLOW_TEST_EDIT=1` says otherwise; do not ask the user for the env var as a shortcut.
- When `verify.required_before_stop` is on, the Stop hook refuses "done" after edits until a verify
  command has succeeded — this skill is how to satisfy it.

## Measurement
Append one `verify.run` event after step 2 (and again after a bugfix fix run):
```bash
mkdir -p .sdlc/logs && printf '{"ts":"%s","event":"verify.run","session_id":"","decision":"%s","reason":"%s","detail":{"build":%s,"test":%s,"lint":%s,"bugfix":%s,"ui":%s}}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<pass|fail>" "<all green|<stage> failed>" "<exit|null>" "<exit|null>" "<exit|null>" "<true|false>" "<true|false>" >> .sdlc/logs/events.jsonl
```
When `--bugfix` creates and later removes `.sdlc/state/allow-test-edit`, also append `verify.test_unlock` (`detail: {slug, files}`) — the audit record that the test lock was opened on purpose.
`decision: pass` = every stage exit 0 (`fail` otherwise — the same vocabulary the post-bash hook uses, so metrics count both). The `post-bash` hook also records verify command results in the
session state. Feeds Stage 4 in `/sdlc:metrics`: first-pass success rate of agent-written changes and
verify pass/fail trend; the incident → eval time comes from `/sdlc:evals`.
