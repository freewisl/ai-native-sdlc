# Review instructions

## Passes
Run three passes and tag each finding with its pass:
- Bugs: logic errors, broken edge cases, subtle regressions
- Security: injection risks, authentication gaps, PII in logs, secrets in the diff
- Compliance: the change meets the work doc (acceptance criteria, plan) and our design principles; CLAUDE.md conventions are followed

## What Important means here
Reserve Important for findings that would break behavior, leak data or breach a policy. Style and naming are nits.
An Important finding names a concrete failure: these inputs or this state → this wrong result. Each one is checked once
more in a fresh context that tries to refute it; only findings that survive count.

## New or pre-existing
Report only what this change introduced or made worse as findings. Problems that were already there are listed
separately as pre-existing — they become proposed work items, they do not block this change.

## Cap the nits
Report at most five nits per review; summarize the rest as a count.

## Do not report
Generated files under {{generated_paths}} and anything the gate already enforces (build, tests, lint, formatting).

## Tally
End with one line: `SDLC_REVIEW_TALLY important=<n> nits=<n> validated=yes`. It goes into the work doc's `## Review`;
the merge gate reads it, and a change with an open Important finding does not merge.

## Feedback into CLAUDE.md
When a review flags a mistake for the second time, add the correction to the "Things Claude gets wrong" section of
CLAUDE.md as part of that change. Also flag when a change has made CLAUDE.md outdated.
