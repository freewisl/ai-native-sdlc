---
name: status
description: Shows where sdlc work stands — whether the loop is running and on which item, the queue by phase, what needs the user with the exact reply that resolves each, cost, recent merges and merges worth a look — and applies the user's replies: answers a parked question, retries or drops an item, rates a merge. Use when the user types /sdlc:status or asks "where are we", "how far along", "what's the status", "what needs me", "progress", "어디까지 됐어", "진행 상황", "뭐 해야 돼", "상태 보여줘", or replies to something the loop asked or reported: "다시 해봐", "그건 빼", "좋아", "고쳐야 해", "별로".
argument-hint: "[<slug>]"
allowed-tools: Read, Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(git show *)
---

# /sdlc:status — where things stand, and what needs you

Arguments: $ARGUMENTS

`sdlc.py` below means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py"`. Talk to the user in their language.

## Live data
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" status 2>&1 || true`

## Summarize it
If the live data above is missing or an error, run `sdlc.py status` yourself (`--json` adds per-item detail). "no .sdlc/config.json" → tell the user to run `/sdlc:init`; "config: version 1" → `/sdlc:init` migrates it. Otherwise, in the user's language and in this order, a few short lines, no raw dump:
1. **Loop:** running or not. Running: the current item, its phase and for how long, with the session's model and effort (`sonnet · medium`); phase `checks` means it waits for the PR's GitHub checks, "waiting" means it polls for people, "usage limit — wakes …" means a limit has to reset first. Not running: why it stopped, and whether it is paused. A "start refused …" part means a later start did not happen: say why, and that the last stop shown is still the previous run's.
2. **Queue:** counts in plain words — ready = waits for design, planned = waits for build, built = at the gate, design in review and awaiting merge = a person, question, needs a person, blocked, done. `next:` is what the loop takes next.
3. **Needs you:** one line per item — what happened and the exact reply that resolves it (table below). Nothing there: say nothing needs them.
4. **Cost:** the line says `loop cost` — what the loop spent: today, 7 days, median per merged item. Sessions the user ran by hand (`/sdlc:go`) are not in it; say so when they ask about their total.
5. **Recent merges**, and "worth a look": auto-merged changes nobody rated, with their PR or commit — ask for a verdict.
6. One closing line with the next step: the loop is stopped while items can run → "돌려"; the queue is empty → give me a goal.

With a slug in the arguments, show that item alone: status, phase, what it waits for, failures and last error, cost, PR, and from its work doc (`git show sdlc/<slug>:<work dir>/<slug>.md`, else `<default>:<work dir>/<slug>.md`; the work dir is `paths.work`, default `work`) the Goal, the Review tally, Departures and Questions.

## What resolves each "needs you"
| Mark · kind | What happened | What resolves it |
|---|---|---|
| `?` question | a session parked a question in the doc's `## Questions` | Show the whole question and its recommended answer (the status text is cut: read `## Questions` from the work doc as above). The user's answer → `sdlc.py answer <slug> "<their words>"` |
| `!` human | the gate sent it to a person; the reasons name it — new dependency, risk path, a guard file (`.sdlc/config.json`, `.claude/settings*.json`, the checks folder), an existing test changed while the design said `test_changes: none`, a test deleted, weakened or skipped, acceptance criteria or `test_changes` changed after the design, merge conflict, a merge that did not happen, the PR's checks failed, were still running or could not be read when the wait ended, a fix session could not fix it (`SDLC_FIX_BLOCKED`) | Accept as is: the user merges the PR (link shown); without a remote there is no PR — the line says `accept as is: git merge sdlc/<slug>`, to run on the default branch (the next loop run removes the merged branch) — nothing more to do: the item counts as done as soon as the merge is on the default branch (`sdlc.py status` fetches first, so a merge made on GitHub shows up at once; offline it shows the last fetch); a PR they close is noticed by the running loop on its next pass, not by status. Fixed it themselves or changed a gate rule: "다시 해봐". Not wanted: "그건 빼" |
| `✓` approve-design | team: the draft design PR waits for approval | someone other than its author approves it, or adds the label `sdlc:approved`; the loop notices within about 10 minutes |
| `⇢` merge | team: the gate passed, the PR is ready | a person merges it |
| `✗` blocked | failed three times; the last error is shown | "다시 해봐" gives it another round; "그건 빼" |
| `+` proposed | an idea waiting to be accepted (a problem that was already there, or a follow-up of a rejected change) | accept: `sdlc.py accept <slug>` — also while a loop runs here (the doc waits in the inbox and the loop commits it between items); team: a queue PR a person merges. "그건 빼" dismisses it |
| `…` dependency | waits for an item that waits for the user, was dropped, or has no doc | resolve that item |

## Replies you apply
Apply a reply only when it clearly answers something shown here or in the conversation; a "좋아" meant for something else is not a rating. Find the slug in the status above or in the conversation; when two items fit, ask which.
- An answer to a parked question → `sdlc.py answer <slug> "<their words, as they said them>"`. If the loop is not running afterwards, offer to continue (`sdlc.py resume`) and do it on their yes.
- "다시 해봐" / "retry" → `sdlc.py retry <slug>`: failures and waits are cleared and the loop takes the item again. If nothing changed, the same gate reasons come back — say so.
- "그건 빼" / "drop" → `sdlc.py drop <slug> "<their reason>"`: the drop is recorded on the default branch, so every clone sees it — in a team repository through a queue PR a person merges, while a loop runs here it waits in the inbox — and the item's own PR is closed. Relay in one line where it landed (its last line says). An item a loop session dropped on its own is recorded the same way; if the loop's output says the drop was `NOT recorded`, it reached only this checkout: the same command records it.
- "받아" / "그거 해" / "accept it", about a `+` proposed item → `sdlc.py accept <slug>` (several at once: `sdlc.py accept a b`); relay in one line where it landed — committed on the default branch, in the inbox until the running loop takes it, or (team) a queue PR a person has to merge. If the loop is not running afterwards, offer to start it (`sdlc.py resume`) and do it on their yes.
- A verdict on a merge from "worth a look": "좋아" → `sdlc.py rate <slug> good`. "고쳐야 해, <what>" → `sdlc.py rate <slug> fix "<what>"`: a follow-up work item is queued; when the loop is not running, offer to start it (`sdlc.py resume`) and do it on their yes. "별로, <why>" → `sdlc.py rate <slug> reject "<why>"`: a `proposed` follow-up waits for their acceptance. For fix or reject without a reason, ask what should change — without a note nothing is queued.

Confirm what you did in one line. Everything else here is read-only: do not merge, approve or close PRs for the user, and do not edit work docs yourself — accepting a `proposed` item is `sdlc.py accept`.
