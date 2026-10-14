---
name: run
description: Runs the background loop that works through a queue of work items without the user — turns a goal into PR-sized work items and starts the loop, only queues items, starts or resumes the loop, or stops it. Use when the user types /sdlc:run or says "build X, fill the backlog and run it", "queue this", "add these to the queue", "run the loop", "keep going in the background", "resume the loop", "stop the loop", "stop after this one", "X 만들어, 알아서 채우고 돌려", "백로그 채우고 돌려", "큐에 넣어", "돌려", "이어서 돌려", "멈춰", "이것만 끝내고 멈춰". One change done in this session is /sdlc:go, not this.
argument-hint: '["<goal>"] [--queue "<what>"] [--items a,b] [--max-items N] [--max-minutes M] [--dry-run] [stop [--after-item]]'
allowed-tools: Read, Write, Glob, Grep, Task, Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*")
---

# /sdlc:run — fill the queue, run the loop

Arguments: $ARGUMENTS

`sdlc.py` below means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py"`. Talk to the user in their language. The loop gives every work item fresh sessions — design, build, a shell gate, merge — and goes on until the queue is empty; you only fill the queue and start or stop it. If a command says there is no `.sdlc/config.json` or that the layout is version 1, tell the user to run `/sdlc:init` and stop.

| The user says | Mode |
|---|---|
| `/sdlc:run "goal"`, "X 만들어, 알아서 채우고 돌려", "백로그 채우고 돌려" | Goal |
| "큐에 넣어", `--queue "…"` | Goal, stopping after step 5: queue only |
| `/sdlc:run`, "돌려", "이어서 돌려" | Start or resume |
| "멈춰", "stop" · "이것만 끝내고 멈춰" | Stop · Stop after this item |

## Goal
1. **Look first.** Read `.sdlc/config.json` (`language`, `mode`, `paths.work` — default `work` — and `commands`), CLAUDE.md and the existing work docs (Glob the work dir; `sdlc.py status` shows the queue). On an existing codebase sweep it with the `sdlc-researcher` agent. When the goal hinges on a decision only the user can take (which provider, which platform), ask one short multiple-choice question first, recommended answer first — never invent it.
2. **Split** the goal into work items, one pull request each, typically 3–12 (a small goal is one item; more than a dozen: queue the first dozen and say the rest follows once they merge).
   - One coherent change with its own tests that a single build session can finish.
   - A new app: item 1 is the skeleton — project setup, one passing test, and the build/test/lint commands recorded in `commands.*` of `.sdlc/config.json` and in CLAUDE.md. Filling a command that is still empty is the one `.sdlc/config.json` change the gate lets through unattended; changing or removing a command that has a value goes to a person. An empty test command makes the gate verify nothing; on an existing repository without one, tell the user.
   - Independent where possible. `depends_on: [slug]` only for a real order: an item starts after its dependencies merged.
   - Acceptance criteria: numbered `R1. WHEN … THE SYSTEM SHALL …`, observable and testable, a handful per item. They freeze once the item is planned, so describe behavior, not implementation. Leave Plan and Proof to the design session.
3. **Write one doc per item** into the work dir as `<slug>.md` (slug: lowercase ascii, digits, `-`; the file name equals the slug). Format: `${CLAUDE_PLUGIN_ROOT}/skills/go/references/work-doc.md`; skeleton: `${CLAUDE_PLUGIN_ROOT}/templates/<language>/work.md`.
   ```
   ---
   slug: entry-crud
   title: 수입·지출 입력/수정/삭제
   status: ready
   depends_on: [project-skeleton]
   test_changes: none
   size: m
   created: 2026-10-12T09:30:02Z
   source: goal:a1b2c3
   ---
   ## Goal
   <who needs what, and why — plain words>

   ## Acceptance criteria
   R1. WHEN … THE SYSTEM SHALL …
   ```
   No comments after frontmatter values (they would become part of the value); the title is in `language` and quoted when it contains `: `. `created` is UTC and ascending, a second apart, in the order you want the items worked; `source` carries one short id shared by every item of this goal. `size` is your guess of the change — `s` a few lines in one or two files and nothing risky, `m` an ordinary PR, `l` many files or a migration, concurrency or security-sensitive change; the design revises it, and the loop sizes each session's effort from it.
4. **Show the list** — slug, title, depends_on — in a few lines. No approval round: the user delegated the split, and "그건 빼" works later.
5. **Queue it:** `sdlc.py enqueue --new`. "not queued: …" → fix the docs it names and run it again. The end of its output says how the items joined the queue:
   - `committed on <branch>`, `merged <PR>` or `inbox (…)` → they are on the queue.
   - `PR … the items join the queue when a person merges it`, or `could not be merged` → they are **not** on the queue yet, and a loop started now would exit with "queue empty". Do not start it: give the PR link, say who has to merge it, and that "돌려" starts the loop afterwards.
6. **Start:** `sdlc.py run --detach` (add `--max-items N` / `--max-minutes M` when the user capped it; starting twice is harmless), then `sdlc.py status` to confirm `loop: running`. A refusal — uncommitted changes in this checkout, paused, team mode without a GitHub remote, `gh` not logged in, `claude` not found — is printed verbatim: say what to fix. Do not work around it, and do not commit or stash the user's changes for them.
7. **Tell the user**, briefly:
   - Follow it with "어디까지 됐어?". Stop it with "멈춰" (the running session is recorded; "이어서 돌려" continues it) or "이것만 끝내고 멈춰". A question the loop cannot settle is parked for them while the rest goes on. It stops by itself when the queue is empty, only items waiting for them are left, failures pile up, or a usage limit resets too far away, and says why. Cost so far is in the status.
   - The loop works in this checkout and switches branches here: leave the files alone while it runs, or use another `git worktree`.
   - Solo: what passes the gate is merged by the loop; only what the gate hands to a person waits for them (a new dependency, a risk path, a guard file, an existing test changed, …) — the status lists it with the reasons. A few merges a week are offered for a quick rating.
   - Team: a person merges the queue PR (that is the approval of the list). Each item then gets a draft design PR; an approving review by someone other than its author, or the label `sdlc:approved`, lets the build start. A passed gate marks the PR ready, and a person merges it. The loop polls every 10 minutes and gives up after 12 idle hours ("이어서 돌려" restarts it).

## Start or resume
`sdlc.py status` first. Running: say so and show its loop line. `paused`, or the user says "이어서": `sdlc.py resume` (it clears the pause and starts the loop in the background; while the loop is still finishing an item it cancels a pending "stop after this item"). Otherwise `sdlc.py run --detach`; `--items a,b`, `--max-items N` and `--max-minutes M` pass through. `sdlc.py run --dry-run` prints what would run and what waits, and starts nothing. Nothing runnable: say what waits for the user (the status list) instead of starting. When the status shows why the last run stopped, say it plainly: usage limit and its reset time, N failures in a row, twelve idle hours, nothing runnable.

## Stop
"멈춰" → `sdlc.py stop`: the running session is asked to stop and recorded, so `resume` continues the same work; allow it up to a minute. "이것만 끝내고 멈춰" → `sdlc.py stop --after-item`: the current item finishes, then the loop stops. Not running: say so.

## Rules
- Never run the items yourself in this session; each one gets a fresh session from the loop.
- The loop never deploys and never touches the default branch directly (hooks and the merge step enforce it). Do not edit hooks or `loop.*`, delete the pause file by hand, or set the release approval variable to get past a stop.
