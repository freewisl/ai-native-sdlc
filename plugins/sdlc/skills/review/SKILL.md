---
name: review
description: Runs the AI side of the PR review loop (Stage 5 Deploy) — applies REVIEW.md's three passes (bugs, security, compliance against spec.md and plan.md) through the sdlc-reviewer agent, ranks findings Important/Nit with the nit cap, feeds repeated mistakes into CLAUDE.md, and with --pr babysits a pull request (sweep unresolved comments and failing checks, fix, push) until it is green and waiting only on a code owner. Use when the user says "review this diff", "review the PR", "run the review passes", "address the review comments", "babysit PR 42 to green", "check the change against the plan", "리뷰해줘", "PR 리뷰", "리뷰 코멘트 반영해", "PR 초록불까지 봐줘".
argument-hint: "[--base <ref>] [--pr <n>] [--slug <slug>] [--post]"
---

# /sdlc:review — identical review passes for every change; humans judge intent and risk

## What changes
- **Traditional:** Review capacity was planned around human output. A PR waits for a reviewer to read all of it, review quality varies with the reviewer's load, and the author chases while the backlog grows.
- **AI-native:** All PRs get an identical set of review passes, with findings ranked by severity. Human attention moves up a level, to whether the change does what the plan intended and whether the risk is acceptable.


## When to use
Before opening a PR (local diff), on an open PR (`--pr <n>`), or to address review comments on a PR
Claude opened. Prerequisites: CLAUDE.md; REVIEW.md (created here if missing); spec/plan for the slug
when the Compliance pass should check them.

Prerequisites: An updated CLAUDE.md from Stage 3; skills if the review passes enforce written policies; defined subagents (text). Dependency figure: Evals → PR review (solid), Skills ⇢ PR review (dotted).
Infrastructure: A repo with the Claude integration installed — the managed Code Review service enabled by an admin, or claude-code-action in your own CI (model calls through Bedrock, Vertex or Foundry where needed) — and branch protection that requires a code owner's approval.

## Inputs
- `$ARGUMENTS`: `--base <ref>` (default: `origin/main`, else `main`, else `HEAD~1`), `--pr <n>` (needs
  `gh`), `--slug <slug>` (else derived from the branch name or `.sdlc/state/active-plan`), `--post` to
  publish the findings as a PR comment.
- `REVIEW.md` (path `paths.review` in `.sdlc/config.json`). If missing, create it from
  `${CLAUDE_PLUGIN_ROOT}/templates/<lang>/REVIEW.md`, substituting `{{generated_paths}}` with
  `protect.generated_paths` (or "src/gen/" when empty), and commit it — the tech lead owns its content.
- `<spec>/<slug>.md`, `<plan>/<slug>.md`, `CLAUDE.md`.
- Rubric details and the babysit protocol: `${CLAUDE_PLUGIN_ROOT}/skills/review/references/review-rubric.md`.

## Steps
1. **Collect the diff.** Local: `git diff <base>...HEAD` (plus `git diff` for uncommitted changes, flagged
   as such). PR: `gh pr view <n> --json title,body,headRefName,baseRefName,files,statusCheckRollup` and
   `gh pr diff <n>`. Save the diff to `.sdlc/state/review-<slug|pr>.diff`. Exclude the generated paths
   REVIEW.md lists.
2. **Resolve the slug** and open `spec/<slug>.md` and `plan/<slug>.md` when they exist; otherwise the
   Compliance pass checks CLAUDE.md conventions only, and the report says no spec/plan was found.
3. **Launch `sdlc-reviewer`** (Agent tool) with: the diff path, `REVIEW.md`, the spec/plan paths, CLAUDE.md,
   and the base ref. Ask for the findings table plus the JSON tally
   `{"important": n, "nits": n, "passes": {"bugs": n, "security": n, "compliance": n}}`. The agent has
   read-only git/gh access and cannot approve.
4. **Present the findings** grouped by pass, Important first, each with file:line, why it matters, and the
   suggested change; at most five nits (the rest summarized as a count), nothing CI already enforces.
   Include the tally line verbatim so a platform engineer can gate on it.
5. **Repeat detection → CLAUDE.md.** For each Important finding, search previous review events
   (`grep '"event":"review.run"' .sdlc/logs/events.jsonl`) and `CLAUDE.md` "Things Claude gets wrong" for
   the same mistake class. A second occurrence → invoke `/sdlc:lesson "<one-line correction>"` (Skill tool)
   as part of this review. Also flag any change that made CLAUDE.md outdated (renamed command, moved
   directory) and propose the edit.
6. **Fix loop (local).** If the user wants the Important findings addressed: fix, run `/sdlc:verify`,
   commit with a message that names the finding, re-run steps 1–4 on the new diff.
7. **`--pr <n>` babysit loop** (details in the rubric): sweep unresolved review threads
   (`gh api graphql` reviewThreads with `isResolved=false`) and failing checks (`gh pr checks <n>`); for
   each, fix → `/sdlc:verify` → commit → `git push`; re-sweep. Repeat until checks are green and no thread
   is unresolved, then **stop**: report "green, waiting on code-owner approval". Reply to threads only
   with what was changed (`gh pr comment` / thread reply) when `--post` or the user asks.
8. **Publish** with `--post`: `gh pr comment <n> --body-file <findings.md>` — findings only, never an
   approval.

## Output
Findings by pass (Important / Nit), the JSON tally, the CLAUDE.md lesson added (if any), the
CLAUDE.md-outdated flag, and for `--pr` the loop log (threads resolved, checks fixed, pushes) ending with
the wait-for-approval line.

## Solo mode
When `roles.solo` is true, after the findings are addressed and checks are green ask once whether to enable auto-merge (`gh pr merge --auto --squash <n>`); the human decision is that answer — the skill still never approves or merges on its own initiative.

## Governance
- Separation of duties: the agent that wrote the code cannot approve it. **Never** run
  `gh pr review --approve`, `gh pr merge`, or change branch protection; findings do not approve or block
  on their own — a code owner approves through branch protection, informed by the findings.
- The PR thread is the audit record: requests, fixes and approvals stay there; local reviews are recorded
  in the events log.
- The tech lead owns REVIEW.md and tunes it monthly (rate findings, cap nits, exclude generated paths and
  what CI enforces); this skill proposes edits, it does not rewrite the policy.
- The managed Code Review service or `claude-code-action` (`sdlc-review.yml`) runs the same REVIEW.md in
  CI; `@claude` on a review comment addresses that comment — offer the workflow if `.github/` exists.

## Measurement
Append after step 4 (and after each babysit sweep):
```bash
mkdir -p .sdlc/logs && printf '{"ts":"%s","event":"review.run","session_id":"","decision":"allow","reason":"review completed","detail":{"important":%s,"nits":%s,"passes":{"bugs":%s,"security":%s,"compliance":%s},"pr":%s,"slug":"%s","repeat_finding":%s,"policy_findings":%s}}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<n>" "<n>" "<n>" "<n>" "<n>" "<n|null>" "<slug>" "<true|false>" "<n>" >> .sdlc/logs/events.jsonl
```
Feeds Stage 5 in `/sdlc:metrics`: time to first review, findings per review, comments resolved without a
human touching the branch (babysit sweeps), and Stage 3's repeat-mistake indicator via `repeat_finding`.
