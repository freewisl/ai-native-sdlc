# Review rubric and babysit protocol

## The three passes (REVIEW.md)

| Pass | Look for | Typical Important | Typical Nit |
|---|---|---|---|
| Bugs | logic errors, broken edge cases (empty, null, boundary, concurrency), subtle regressions in callers of changed code, error paths that swallow failures | wrong result for a reachable input; regression in an existing flow; unhandled failure that corrupts state | naming, comment accuracy, minor duplication |
| Security | injection (SQL/command/template/LDAP), authn/authz gaps on new routes, PII or secrets in logs and error messages, secrets in the diff, unsafe deserialization, path traversal, SSRF, weak crypto, dependency red flags | any exploitable path; any credential; PII in a log line | defensive hardening that is not a policy |
| Compliance | the change does what `spec.md` requires (each R-n covered or explicitly deferred), follows `plan.md` (files, order, Departures recorded), respects CLAUDE.md conventions and the organization's policy skills | a requirement silently dropped; a plan departure not recorded; a policy rule violated | a convention nit already covered by lint |

Tag every finding with its pass. A finding that fits two passes goes under the more severe one.

## Severity

**Important** = would break behavior, leak data, or breach a policy. Everything else is a **Nit**.
Style and naming are nits. Report at most five nits; summarize the rest as "and N more nits (formatting,
naming)". Never report generated files or anything CI already enforces (formatting, lint).

## Finding format

```
[Important][Security] src/api/claims.py:88 — status endpoint has no auth guard
  Why: any caller can read another customer's claim state (spec R2 requires existing auth only).
  Suggest: add the gateway JWT dependency used by the other /claims routes.
```

One finding per issue, deduplicated across files. Quote the line, not the whole function. Suggested
change is a sentence, not a patch, unless the fix is one line.

## Compliance checks, concretely

1. Spec: list R1..Rn; for each say covered / partially / missing / deferred (with the plan's reason).
2. Plan: every file in "Files that change" appears in the diff or is explained in Departures; every file in
   the diff appears in the plan or Departures; the Proof section's tests exist and were run (verify output).
3. Test integrity: if the plan says `test_changes: forbidden`, any change under a test path is Important.
4. CLAUDE.md: conventions broken; commands or paths CLAUDE.md mentions that this change renamed/moved
   (→ "CLAUDE.md outdated" flag).

## Repeat detection

A finding "repeats" when a previous `review.run` event or a line in "Things Claude gets wrong" describes the
same mistake class (same rule violated, not the same line). Normalize both to lowercase key phrases
(e.g. "no auth on endpoint", "bumped dependency version", "edited generated file") before comparing. On a
repeat, the review calls `/sdlc:lesson` with a one-line rule in the imperative ("Every new route needs the
gateway JWT dependency; see api/deps.py") and sets `repeat_finding: true` in the event.

## Babysit protocol (`--pr <n>`)

```bash
gh pr view "$n" --json number,title,headRefName,baseRefName,mergeable,reviewDecision,statusCheckRollup
gh pr checks "$n"                       # failing checks
gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100){nodes{id isResolved path line comments(first:10){nodes{author{login} body}}}}}}}' \
  -F o="$owner" -F r="$repo" -F n="$n"   # unresolved threads: isResolved == false
```
Loop:
1. Check out the PR branch (`gh pr checkout <n>`) in a worktree if the current tree is dirty.
2. For each unresolved thread: read the comment, make the change (or explain why not), run `/sdlc:verify`,
   commit "review: <thread summary>", push. Reply in the thread with what changed only when asked/`--post`.
3. For each failing check: `gh run view <run-id> --log-failed`; fix real failures; a flaky failure is
   re-run once (`gh run rerun <id> --failed`) and reported as flaky, not fixed by loosening a test.
4. Re-sweep. Stop when: checks green, threads resolved (by the reviewer — Claude does not resolve
   threads it answered unless the reviewer allows it), and `reviewDecision` is not CHANGES_REQUESTED.
5. Final line: "PR #n is green and waiting on code-owner approval." Never approve, never merge, never
   force-push, never edit branch protection.
6. Give up after five sweeps without progress and report what is blocking.

## Monthly tuning (tech lead)

Rate a sample of findings (useful / noise); cap nits lower if noise dominates; add "Do not report" entries
for what CI now enforces; add generated paths; check the Important definition still matches the
organization's risk classes. Commit REVIEW.md changes through PR review like code.
