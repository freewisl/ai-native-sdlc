# Questions and drops

A loop session cannot ask anyone. When it cannot go on without a person it parks the item with a question — that is not a failure and it costs nothing. Interactive: do not park; ask the user in the chat (one message, numbered, recommended answer first) and carry on with what they say.

## Ask only when

- the request or a criterion is ambiguous in a way that changes what gets built;
- criteria contradict each other, or one cannot be tested as written;
- the change needs an existing test edited and the doc says `test_changes: none` — a person has to allow it;
- it needs a guard file changed (`.sdlc/config.json`, the frozen-paths file, `.claude/settings.json`, `.claude/settings.local.json`, `checks/` — filling a still-empty `commands.<name>` in the config is not held) or a frozen path (listed in the frozen-paths file or in `protect.generated_paths`);
- it has security, data or product consequences nobody wrote down.

Do not ask what the repository answers (read it), an implementation choice you can make and record in `## Departures`, or a new dependency or a `risk.human` path that the Plan can justify: say why in the Plan (in a build, in Departures) — the gate hands that PR to a person, who sees the reason.

## How

1. Commit the work so far; it stays on the branch.
2. Write `## Questions`: at most three, numbered. Each is one line with `(recommended: <option>)`, then the options as bullets, the recommended one first, and a clause on what the answer changes when that is not obvious. The first line is what the user sees in the status, so make it complete.

   ```
   ## Questions
   1. Which currency are amounts stored in? (recommended: KRW)
      - KRW — whole numbers, matches the existing `price` column
      - USD — needs a decimal type and a migration
   ```
3. Set `status: needs-input`, commit `sdlc(<slug>): question`, print `SDLC_NEEDS_INPUT <slug>` and end the session. Do not go on with other work.

## After the answer

The engine appends `**Answer** (date): …` under your questions, puts the doc back to `ready` or `planned`, and starts the next session. Read the answer and apply it; keep `## Questions` as the record. A design session that finds a Plan and an Answer already in the doc revises the Plan; a build session reads the Answer and continues.
- The answer changes an acceptance criterion: edit just that criterion and quote the answer in `## Departures` — the gate hands that PR to a person, who sees why.
- The answer allows editing existing tests: set `test_changes: allowed` and quote the answer in `## Departures`. In a design session, before `planned`, that is the design's own decision; in a build session the gate hands the PR to a person (`test_changes` is frozen with the plan), who sees the quoted answer.

## Drop instead

Only with evidence: the behavior already exists (file:line or commit), another merged item made the work unnecessary, or it contradicts the design of the codebase. Set `status: dropped`; the first line of `## Departures` is the reason (replace the template comment — the engine uses that line); commit; print `SDLC_DROPPED <slug>`. The engine then records the drop where every clone reads it, as `sdlc.py drop` does: the doc on `<default>` becomes `dropped` with your reason (team mode: through a PR a person merges) and the item's own PR is closed. That is not yours to do — do not edit `<default>` or touch a PR.
